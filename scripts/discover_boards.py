"""Board discovery — hard problem #1.

There is no registry mapping companies to ATS tokens, and coverage is capped
entirely by how many tokens we can find. This harvests them at scale.

The unit of discovery is a *board*, never a job. Crawling job links would
rebuild a job board: duplicates, dead links, and no way to detect closures.
One board token, found once, hands the ingestion pipeline that employer's
entire requisition history forever.

It is also barely a crawler. Common Crawl already crawled the web; we query
their URL index for the ATS domains and validate the candidates against the
vendors' own JSON feeds. Public datasets and documented endpoints only — the
moment discovery needs proxies or bot evasion, stop.

What that cannot reach is any board Common Crawl never fetched a URL for on the
ATS's own domain: Lever (barely indexed at all — this sweep yields zero tokens),
iframed embeds whose token lives in a query string, and the two-part Workday /
Oracle / Eightfold identities. `scripts/crawl_careers.py` crawls employers'
careers pages for exactly those, and writes `crawled_<vendor>.json` in the same
format `harvest` produces. `validate` below reads the union of both.

    uv run python scripts/discover_boards.py harvest  --vendor greenhouse
    uv run python scripts/discover_boards.py validate --vendor greenhouse
    uv run python scripts/discover_boards.py report
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
import urllib.parse
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from reqtrace.adapters.workday import parse_token as parse_workday_token  # noqa: E402
from reqtrace.crawl import CASE_INSENSITIVE, plausible_token  # noqa: E402
from reqtrace.normalise import is_australian, parse_location  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DISC = ROOT / "data" / "discovery"
OUT = ROOT / "data" / "discovered_boards.csv"

CC_INDEX = "https://index.commoncrawl.org"
UA = "reqtrace/0.1 (+personal job-search index)"

# CC index URL patterns per vendor, the regex that lifts the token back out,
# and how to assemble it. The third element exists for Workday, whose identity
# is three pieces of the URL rather than one path segment.
_first = lambda m: m.group(1)  # noqa: E731

SOURCES = {
    "greenhouse": (
        ["boards.greenhouse.io/*", "job-boards.greenhouse.io/*"],
        re.compile(r"(?:job-)?boards\.greenhouse\.io/([A-Za-z0-9_-]+)"),
        _first,
    ),
    "ashby": (
        ["jobs.ashbyhq.com/*"],
        re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_-]+)"),
        _first,
    ),
    "smartrecruiters": (
        ["careers.smartrecruiters.com/*"],
        re.compile(r"careers\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
        _first,
    ),
    # Lever is here for completeness, but Common Crawl barely indexes
    # jobs.lever.co (a sweep returns essentially just robots.txt), so Lever
    # tokens have to come from elsewhere — VC portfolio pages, CT logs.
    "lever": (
        ["jobs.lever.co/*"],
        re.compile(r"jobs\.lever\.co/([A-Za-z0-9_-]+)"),
        _first,
    ),
    # Workday, the highest-yield source here and the last one added, because
    # the composite `tenant.wdN/Site` identity did not fit the one-group shape
    # the other four share. It is where large employers actually live —
    # Accenture, CommBank, Telstra — and Common Crawl indexes
    # `*.myworkdayjobs.com` heavily, unlike Lever.
    #
    # The URL carries the site path in two different places depending on
    # whether it is a browser URL (`/en-US/Site`) or an API call
    # (`/wday/cxs/tenant/Site`), so the pattern skips an optional `wday/cxs`
    # segment and an optional locale before taking the site.
    "workday": (
        ["*.myworkdayjobs.com/*"],
        re.compile(r"([A-Za-z0-9_-]+)\.(wd\d+)\.myworkdayjobs\.com/"
                   r"(?:wday/cxs/[^/]+/)?(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)"),
        lambda m: f"{m.group(1)}.{m.group(2)}/{m.group(3)}",
    ),
}

# Path noise that is not a board identifier.
SKIP = {
    "embed", "job_board", "jobs", "api", "v0", "v1", "search", "static", "assets",
    "www", "boards", "company", "apply", "en", "us", "robots", "favicon.ico",
    "sitemap.xml", "index.html", "postings", "job-board", "companies", "accounts",
}
HEXISH = re.compile(r"^[0-9a-f]{16,}$", re.I)


def plausible(token: str) -> bool:
    """Cheap junk filter. Validation is what really decides — this just avoids
    spending a request on an obvious URL fragment."""
    t = token.lower()
    return (
        t not in SKIP
        and not t.isdigit()
        and not HEXISH.match(t)
        and 1 < len(token) <= 60
    )


# --------------------------------------------------------------------------
# phase A — harvest candidates from the Common Crawl URL index
# --------------------------------------------------------------------------

async def latest_collections(client, n: int) -> list[str]:
    r = await client.get(f"{CC_INDEX}/collinfo.json")
    r.raise_for_status()
    return [c["id"] for c in r.json()[:n]]


async def page_count(client, collection: str, pattern: str) -> int:
    r = await client.get(
        f"{CC_INDEX}/{collection}-index",
        params={"url": pattern, "output": "json", "showNumPages": "true"},
    )
    if r.status_code != 200:
        return 0
    try:
        return int(r.json().get("pages", 0))
    except Exception:
        return 0


async def harvest(vendor: str, collections: int) -> set[str]:
    patterns, token_re, build = SOURCES[vendor]
    found: set[str] = set()
    async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=20.0),
                                 headers={"User-Agent": UA}, follow_redirects=True) as client:
        colls = await latest_collections(client, collections)
        print(f"collections: {', '.join(colls)}", file=sys.stderr)
        for coll in colls:
            for pat in patterns:
                pages = await page_count(client, coll, pat)
                print(f"  {coll} {pat} -> {pages} pages", file=sys.stderr)
                for p in range(pages):
                    body = None
                    for attempt in range(3):  # CC streams; IncompleteRead is common
                        try:
                            r = await client.get(
                                f"{CC_INDEX}/{coll}-index",
                                params={"url": pat, "output": "json", "page": p},
                            )
                            if r.status_code == 200:
                                body = r.text
                                break
                        except Exception as e:
                            if attempt == 2:
                                print(f"    page {p}: {type(e).__name__}", file=sys.stderr)
                        await asyncio.sleep(2 * (attempt + 1))
                    if not body:
                        continue
                    before = len(found)
                    for line in body.splitlines():
                        try:
                            url = json.loads(line)["url"]
                        except Exception:
                            continue
                        m = token_re.search(url)
                        if not m:
                            continue
                        try:
                            token = build(m)
                        except (IndexError, AttributeError):
                            continue
                        if token and plausible_token(vendor, token):
                            found.add(token)
                    print(f"    page {p}: +{len(found)-before} (total {len(found)})",
                          file=sys.stderr)
                    await asyncio.sleep(1.0)  # be a polite client
    return found


# --------------------------------------------------------------------------
# phase B — validate candidates against the vendors' own feeds
# --------------------------------------------------------------------------
# Discovery deliberately uses the *light* Greenhouse endpoint (no
# ?content=true): existence and locations are all we need here, and pulling
# full descriptions for thousands of boards would be gratuitous.

ENDPOINTS = {
    "greenhouse": lambda t: f"https://boards-api.greenhouse.io/v1/boards/{t}/jobs",
    "lever": lambda t: f"https://api.lever.co/v0/postings/{t}?mode=json",
    "ashby": lambda t: f"https://api.ashbyhq.com/posting-api/job-board/{t}",
    "smartrecruiters": lambda t: f"https://api.smartrecruiters.com/v1/companies/{t}/postings?limit=100",
    "workday": lambda t: f"{wd_base(t)}/jobs",
}

# Workday is the one vendor whose feed is a POST, and the one whose boards are
# big enough that validating them like the others would be a mistake: asking
# Accenture for every posting is 100 paged requests, and validation only needs
# to know the board resolves. One request for 20 postings answers that and
# gives a location sample to test for Australian roles, which is all `validate`
# reads. `PAGE` is the vendor's hard cap — asking for more returns nothing.
def wd_base(token: str) -> str:
    tenant, wd, site = parse_workday_token(token)
    return f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"



def extract(vendor: str, d):
    """-> (board_name, [(title, location), ...])"""
    if vendor == "greenhouse":
        js = d.get("jobs", [])
        name = js[0].get("company_name") if js else None
        return name, [(j.get("title", ""), (j.get("location") or {}).get("name", "")) for j in js]
    if vendor == "lever":
        js = d if isinstance(d, list) else []
        return None, [(j.get("text", ""), (j.get("categories") or {}).get("location") or "") for j in js]
    if vendor == "ashby":
        js = d.get("jobs", [])
        return None, [(j.get("title", ""), j.get("location") or "") for j in js]
    if vendor == "smartrecruiters":
        js = d.get("content", [])
        name = (js[0].get("company") or {}).get("name") if js else None
        loc = lambda j: " ".join(filter(None, [  # noqa: E731
            (j.get("location") or {}).get("city"), (j.get("location") or {}).get("country")]))
        return name, [(j.get("name", ""), loc(j)) for j in js]
    return None, []


async def workday_probe(client, token: str, search: str = "") -> dict | None:
    """One POST at the vendor's page cap. -> the decoded body, or None."""
    try:
        r = await client.post(ENDPOINTS["workday"](token),
                              json={"limit": 20, "offset": 0, "searchText": search})
    except Exception:  # noqa: BLE001 - a dead tenant is a settled answer
        return None
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except Exception:  # noqa: BLE001
        return None


async def workday_row(client, token: str) -> dict | None:
    """Validate one Workday board in two requests.

    Workday cannot be validated the way the other four are, and finding that
    out is what this function is. The obvious approach — pull a page of
    postings and test their locations — silently rejects exactly the boards
    worth having: Accenture's board is 2,000 jobs, an unfiltered sample of 20
    is whatever Workday sorts first, and the listing endpoint returns
    `locationsText` empty on some tenants anyway, so every job in the sample
    parses as location-unknown and the board scores zero Australian roles. It
    has 372.

    So ask the search endpoint instead of counting a sample. `searchText` is
    tenant-independent, unlike the location facet GUIDs (`adapters/workday.py`
    documents that the country GUID filtering CommBank returns nothing for
    NVIDIA), and the `total` it returns is a whole-board answer rather than a
    20-row guess.

    It is a keyword match, not a location filter, so the count is an upper
    bound — a role whose description merely mentions Australia is included.
    That is the right direction to be loose in: this decides whether a board is
    worth *fetching*, and the adapter's own location parsing is what decides
    whether a job reaches the index.
    """
    board = await workday_probe(client, token)
    if not board or not board.get("jobPostings"):
        return None
    au = await workday_probe(client, token, "Australia")
    n_au = (au or {}).get("total", 0)
    if not n_au:
        return None
    titles = [j.get("title", "") for j in (au or {}).get("jobPostings", [])]
    au_data = [t for t in titles if DATA_RE.search(t or "")]
    return {
        "ats_vendor": "workday",
        "board_token": token,
        "board_name": "",
        "n_jobs": board.get("total", 0),
        "n_au": n_au,
        "au_ratio": round(n_au / max(board.get("total", 0), 1), 3),
        # Of the AU sample, not the AU total — the same 20-row limit applies,
        # so this undercounts a big board's data roles. It orders the adoption
        # list; it is not a measurement.
        "n_au_data": len(au_data),
        "sample_au_role": (au_data[0] if au_data else (titles[0] if titles else ""))[:70],
        "board_url": f"https://{token.split('.')[0]}."
                     f"{token.split('.')[1].split('/')[0]}.myworkdayjobs.com/"
                     f"{token.split('/', 1)[1]}",
    }


DATA_RE = re.compile(
    r"data scien|machine learn|analytics|analyst|data engineer|research scien"
    r"|decision scien|quantitat|\bML\b|\bAI\b", re.I)


async def validate(vendor: str, tokens: list[str], concurrency: int = 8) -> list[dict]:
    rows: list[dict] = []
    sem = asyncio.Semaphore(concurrency)
    done = 0

    async with httpx.AsyncClient(timeout=httpx.Timeout(40.0, connect=15.0),
                                 headers={"User-Agent": UA}, follow_redirects=True) as client:
        def tick():
            """Progress, shared by both paths. It used to live only in the
            non-Workday branch's `finally`, so a Workday sweep — the slowest of
            the lot, two POSTs per token over thousands of tokens — printed
            nothing at all for a quarter of an hour and was indistinguishable
            from a hang."""
            nonlocal done
            done += 1
            if done % 250 == 0:
                print(f"    validated {done}/{len(tokens)} "
                      f"({len(rows)} live boards)", file=sys.stderr)

        async def one(token: str):
            async with sem:
                if vendor == "workday":
                    try:
                        row = await workday_row(client, token)
                    finally:
                        tick()
                    if row:
                        rows.append(row)
                    return
                try:
                    r = await client.get(ENDPOINTS[vendor](token))
                except Exception:
                    return
                finally:
                    tick()
                if r.status_code != 200:
                    return
                try:
                    d = r.json()
                except Exception:
                    return
            name, jobs = extract(vendor, d)
            if not jobs:
                return  # a board with no open roles tells us nothing about AU
            au = [(t, l) for t, l in jobs
                  if is_australian(*parse_location(l)[:2], l)]
            if not au:
                return
            au_data = [t for t, _ in au if DATA_RE.search(t or "")]
            rows.append({
                "ats_vendor": vendor,
                "board_token": token,
                "board_name": name or "",
                "n_jobs": len(jobs),
                "n_au": len(au),
                "au_ratio": round(len(au) / len(jobs), 3),  # of the sample
                "n_au_data": len(au_data),
                "sample_au_role": (au_data[0] if au_data else au[0][0])[:70],
                "board_url": ENDPOINTS[vendor](token),
            })

        await asyncio.gather(*(one(t) for t in tokens))
    return rows


# --------------------------------------------------------------------------

def cand_path(vendor: str) -> Path:
    return DISC / f"candidates_{vendor}.json"


def crawled_path(vendor: str) -> Path:
    """Candidates from `scripts/crawl_careers.py` — the focused careers-page
    crawl. Kept in a separate file from the Common Crawl harvest so neither
    source can clobber the other; validate reads the union."""
    return DISC / f"crawled_{vendor}.json"


def candidates(vendor: str) -> list[str]:
    """Every token to validate, from both discovery routes.

    Order matters only for the progress log, but dedup does not fold case here:
    Lever tokens are case-sensitive, and `report` is where case-variant
    duplicates for the other three vendors get merged.
    """
    seen, out = set(), []
    for path in (cand_path(vendor), crawled_path(vendor)):
        if not path.exists():
            continue
        found = json.loads(path.read_text())
        for t in found:
            if t not in seen:
                seen.add(t)
                out.append(t)
        print(f"  {path.name}: {len(found)} tokens", file=sys.stderr)
    return out


def rows_path(vendor: str) -> Path:
    return DISC / f"validated_{vendor}.json"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["harvest", "validate", "report"])
    ap.add_argument("--vendor", default="greenhouse", choices=list(SOURCES))
    ap.add_argument("--collections", type=int, default=1,
                    help="how many recent Common Crawl collections to sweep")
    ap.add_argument("--limit", type=int, default=0, help="cap candidates (testing)")
    args = ap.parse_args()

    DISC.mkdir(parents=True, exist_ok=True)

    if args.phase == "harvest":
        found = await harvest(args.vendor, args.collections)
        cand_path(args.vendor).write_text(json.dumps(sorted(found), indent=0))
        print(f"{args.vendor}: {len(found)} candidate tokens -> {cand_path(args.vendor)}",
              file=sys.stderr)
        return 0

    if args.phase == "validate":
        tokens = candidates(args.vendor)
        if not tokens:
            print(f"no candidates for {args.vendor} — run `harvest`, or "
                  f"`crawl_careers.py crawl` then `report`", file=sys.stderr)
            return 1
        if args.limit:
            tokens = tokens[: args.limit]
        print(f"{args.vendor}: validating {len(tokens)} candidates ...", file=sys.stderr)
        rows = await validate(args.vendor, tokens)
        rows.sort(key=lambda r: (-r["n_au_data"], -r["n_au"]))
        rows_path(args.vendor).write_text(json.dumps(rows, indent=1))
        print(f"{args.vendor}: {len(rows)} boards with AU roles -> {rows_path(args.vendor)}",
              file=sys.stderr)
        return 0

    # report — merge every validated vendor into one CSV
    allrows = []
    for v in SOURCES:
        p = rows_path(v)
        if p.exists():
            allrows += json.loads(p.read_text())

    best: dict[tuple, dict] = {}
    for r in allrows:
        tok = r["board_token"]
        key = (r["ats_vendor"],
               tok.lower() if r["ats_vendor"] in CASE_INSENSITIVE else tok)
        prior = best.get(key)
        # Prefer the variant the vendor gave a company name for, then the larger board.
        rank = (bool(r["board_name"]), r["n_jobs"])
        if prior is None or rank > (bool(prior["board_name"]), prior["n_jobs"]):
            best[key] = r
    dropped = len(allrows) - len(best)
    allrows = list(best.values())
    if dropped:
        print(f"deduped {dropped} case-variant tokens", file=sys.stderr)

    # Adoption is APPEND-ONLY. A board whose AU roles all get filled drops out
    # of this sweep's results — if that removed it from the CSV, the ingestion
    # would stop fetching it and every job it left behind would sit
    # `closed_at IS NULL` forever, which is exactly the ghost-job problem this
    # project exists to kill. Once adopted, a board is fetched forever.
    carried = 0
    if OUT.exists():
        for old in csv.DictReader(OUT.open()):
            key = (old["ats_vendor"],
                   old["board_token"].lower()
                   if old["ats_vendor"] in CASE_INSENSITIVE else old["board_token"])
            if key not in best:
                old["sample_au_role"] = old.get("sample_au_role", "")
                best[key] = old
                carried += 1
    if carried:
        print(f"carried {carried} previously-adopted boards with no AU roles this sweep",
              file=sys.stderr)

    allrows = list(best.values())
    allrows.sort(key=lambda r: (-int(r["n_au_data"]), -int(r["n_au"])))
    if not allrows:
        print("nothing validated yet", file=sys.stderr)
        return 1
    with OUT.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(allrows[0].keys()))
        w.writeheader()
        w.writerows(allrows)
    print(f"{len(allrows)} AU boards -> {OUT}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
