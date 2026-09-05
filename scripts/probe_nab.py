"""One-off: is NAB's Australian careers site ingestible?

`data/step0_ats_audit.csv` records NAB as unresolved — the Eightfold tenant
found during step 0 is the India delivery centre (0 of 267 roles in AU), and
the row says "NAB AU hiring runs on an unidentified system". That system is
`careers.nab.com.au`, and its robots.txt declares one sitemap:

    Sitemap: https://careers.nab.com.au/sitemap.xml
    Crawl-delay: 5
    Disallow: /api/

So this reads the sanctioned sitemap with an honest user agent at the declared
crawl delay, and never touches /api/. Each job page carries a JSON-LD
JobPosting block, which is the structured data employers publish so aggregators
can index them.

Three things have to hold before an adapter is worth writing, and this settles
all three:

1. **Identity.** The index keys on the ATS's own requisition id. The JSON-LD
   carries `identifier.value` and the page body carries a NAB requisition
   number; the URL slug is title+location-derived and is *not* a safe key —
   a re-post with a changed location set would mint a new one.
2. **No duplicates.** The sitemap mixes slug URLs with UUID URLs. If the same
   requisition appears under both, "one record per role" stops being true by
   construction.
3. **Full board.** Every run diffs the whole board, so a partial feed
   manufactures false closures. The sitemap's URL set is compared against the
   server-rendered pagination at /jobs/search.

It also measures the thing that turned out to matter most: the site sits behind
an AWS WAF JS challenge (HTTP 202 + `gokuProps`, cookie domains
`clinchtalent.com` / `career-pages.com` — the platform is Clinch, now PageUp's
career-site product). The run records how many pages a polite, honestly
declared crawler actually gets served.

`--identity-check <substring>` is the duplicate test, run over the URLs where a
collision is most likely: one title family whose members differ only by
location. Two of the 79 sitemap URLs are opaque UUIDs rather than slugs, and the
UUID one that could be read is *Home Lending Executive - VIC* — a title with
four slug siblings (hybrid, Sydney, SA, WA) and no VIC slug. If the platform
ever serves one requisition under two URLs, that family is where it shows.

`--slow-retest` answers the obvious follow-up: is that challenge rate-based, so
that a slower crawl would get through? It cools off for five minutes and then
fetches three job pages ninety seconds apart, keeping the session cookies. It
is — 3 of 3 served at 90s where 0 of 8 were at the 5s robots.txt asks for. So
`--delay` sets the spacing, and a challenge is treated as backpressure: cool off
for five minutes, retry the page once, and only give up if it challenges again.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from collections import Counter
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from reqtrace.normalise import is_australian, parse_location  # noqa: E402
from reqtrace.search import ANALYST_EXCLUDE, DATA_TERMS       # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "nab_sitemap_probe.json"
SITEMAP = "https://careers.nab.com.au/sitemap.xml"
SEARCH = "https://careers.nab.com.au/jobs/search?page={n}"
SEARCH_PAGES = 3          # what the site's own pagination advertises
UA = "reqtrace/0.1 (+personal job-search index)"
CRAWL_DELAY = 5.0         # robots.txt: Crawl-delay: 5 — see --delay
BACKOFF = 300.0           # what the WAF actually needs before it serves again
GIVE_UP_AFTER = 8         # consecutive challenges — stop, don't hammer a bank
COOLOFF = 300.0           # --slow-retest: let any rate counter drain first
RETEST_SPACING = 90.0     # --slow-retest: 18x the declared crawl delay

LD = re.compile(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', re.S)
REQ_ID = re.compile(r'requisition_identifier_icon_text[^>]*>\s*([^<]+?)\s*<')
JOB_URL = re.compile(r'https://careers\.nab\.com\.au/jobs/[^"\'\s<>]+')
UUID_URL = re.compile(r'/jobs/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


def challenged(r: httpx.Response) -> bool:
    """AWS WAF answers a suspected bot with 202 and a JS challenge (or nothing
    at all) rather than a 403, so a naive crawler records an empty page as a
    successful fetch."""
    return r.status_code == 202 or not r.text.strip() or "gokuProps" in r.text


def location_of(posting: dict) -> str:
    places = posting.get("jobLocation") or []
    places = places if isinstance(places, list) else [places]
    out = []
    for p in places:
        addr = (p or {}).get("address") or {}
        bits = [addr.get("addressLocality"), addr.get("addressRegion"),
                addr.get("addressCountry")]
        joined = ", ".join(b for b in bits if b)
        if joined:
            out.append(joined)
    return " | ".join(out)


def is_data_role(title: str) -> bool:
    """Mirror of the `data_only` filter in search.py, so the count here is
    comparable to the index's own AU-data-role number."""
    t = f" {title.lower()} "
    if any(term in t for term in DATA_TERMS):
        return True
    return "analyst" in t and not any(x in t for x in ANALYST_EXCLUDE)


async def identity_check(urls: list[str], delay: float) -> list[dict]:
    """Fetch a named family of job pages and record what each one calls itself:
    the JSON-LD `identifier.value`, the requisition number printed on the page,
    and the title. Two URLs reporting one requisition would break the index's
    one-record-per-role invariant before an adapter was ever written."""
    out = []
    async with httpx.AsyncClient(timeout=45, follow_redirects=True,
                                 headers={"User-Agent": UA, "Accept": "text/html"}) as c:
        for i, u in enumerate(urls):
            if i:
                await asyncio.sleep(delay)
            r = await c.get(u)
            row: dict = {"url": u, "status": r.status_code,
                         "challenged": challenged(r)}
            if not row["challenged"]:
                for blob in LD.findall(r.text):
                    try:
                        d = json.loads(blob)
                    except json.JSONDecodeError:
                        continue
                    if d.get("@type") == "JobPosting":
                        ident = d.get("identifier") or {}
                        row["title"] = d.get("title")
                        row["ld_identifier"] = (ident.get("value")
                                                if isinstance(ident, dict) else ident)
                        row["location_raw"] = location_of(d)
                        break
                req = REQ_ID.findall(r.text)
                row["page_requisition_id"] = req[0] if req else None
            out.append(row)
            print(f"  {row['status']} {row.get('page_requisition_id')} "
                  f"{str(row.get('ld_identifier'))[:12]} {str(row.get('title'))[:44]}",
                  file=sys.stderr)
    return out


async def slow_retest(urls: list[str]) -> list[dict]:
    """Is the challenge rate-based? Cool off, then three pages 90s apart in one
    session — 18x the declared crawl delay. If these are still challenged, no
    politeness setting reaches the job pages and only executing the WAF's
    JavaScript would, which is the line this project does not cross."""
    out = []
    print(f"cooling off {COOLOFF:.0f}s", file=sys.stderr)
    await asyncio.sleep(COOLOFF)
    async with httpx.AsyncClient(timeout=45, follow_redirects=True,
                                 headers={"User-Agent": UA, "Accept": "text/html"}) as c:
        for i, u in enumerate(urls):
            if i:
                await asyncio.sleep(RETEST_SPACING)
            r = await c.get(u)
            row = {"url": u, "status": r.status_code, "bytes": len(r.content),
                   "challenged": challenged(r),
                   "cookies": sorted(c.cookies.keys())}
            out.append(row)
            print(f"  {row['status']} bytes={row['bytes']} "
                  f"challenged={row['challenged']} cookies={row['cookies']}",
                  file=sys.stderr)
    return out


def arg(flag: str, default: float) -> float:
    return float(sys.argv[sys.argv.index(flag) + 1]) if flag in sys.argv else default


async def main() -> int:
    delay = arg("--delay", CRAWL_DELAY)
    headers = {"User-Agent": UA, "Accept": "application/xml,text/xml"}
    async with httpx.AsyncClient(timeout=45, follow_redirects=True,
                                 headers=headers) as c:
        r = await c.get(SITEMAP)
        r.raise_for_status()
        sitemap_urls = re.findall(r"<loc>([^<]+)</loc>", r.text)
    job_urls = [u for u in sitemap_urls if "/jobs/" in u]
    print(f"sitemap: {len(sitemap_urls)} urls, {len(job_urls)} job urls",
          file=sys.stderr)

    if "--identity-check" in sys.argv:
        match = sys.argv[sys.argv.index("--identity-check") + 1]
        picked = [u for u in job_urls
                  if match in u or UUID_URL.search(u)]
        print(f"identity check over {len(picked)} urls at {delay:.0f}s",
              file=sys.stderr)
        rows = await identity_check(picked, delay)
        prev = json.loads(OUT.read_text()) if OUT.exists() else {}
        prev["identity_check"] = rows
        OUT.write_text(json.dumps(prev, indent=1))
        ids = [r.get("ld_identifier") for r in rows if r.get("ld_identifier")]
        reqs = [r.get("page_requisition_id") for r in rows
                if r.get("page_requisition_id")]
        print(f"\n{len(rows)} pages, {len(set(ids))} distinct identifiers, "
              f"{len(set(reqs))} distinct requisition ids", file=sys.stderr)
        return 0

    if "--slow-retest" in sys.argv:
        rows = await slow_retest(job_urls[:3])
        prev = json.loads(OUT.read_text()) if OUT.exists() else {}
        prev["slow_retest"] = rows
        OUT.write_text(json.dumps(prev, indent=1))
        served = sum(not r["challenged"] for r in rows)
        print(f"\nslow retest: {served}/{len(rows)} served at "
              f"{RETEST_SPACING:.0f}s spacing", file=sys.stderr)
        return 0

    html_headers = {"User-Agent": UA, "Accept": "text/html"}
    search_urls: set[str] = set()
    search_status: list[dict] = []
    rows, blocked, errors = [], [], []

    async with httpx.AsyncClient(timeout=45, follow_redirects=True,
                                 headers=html_headers) as c:
        # 3. does the sitemap agree with the board's own pagination?
        for n in range(1, SEARCH_PAGES + 1):
            await asyncio.sleep(delay)
            try:
                r = await c.get(SEARCH.format(n=n))
            except Exception as e:
                search_status.append({"page": n, "error": repr(e)})
                continue
            if challenged(r):
                search_status.append({"page": n, "status": r.status_code,
                                      "challenged": True})
                continue
            found = set(JOB_URL.findall(r.text)) - {SEARCH.format(n=n)}
            found = {u for u in found if "/jobs/search" not in u}
            search_urls |= found
            search_status.append({"page": n, "status": r.status_code,
                                  "challenged": False, "jobs": len(found)})

        # 1. + 2. identity, per job page
        streak = 0
        for i, u in enumerate(job_urls, 1):
            await asyncio.sleep(delay)
            try:
                r = await c.get(u)
                if challenged(r):
                    # Backpressure, not a verdict: the WAF serves again after a
                    # cool-off. Wait it out once before counting it against us.
                    print(f"  challenged at {i}/{len(job_urls)} — "
                          f"backing off {BACKOFF:.0f}s", file=sys.stderr)
                    await asyncio.sleep(BACKOFF)
                    r = await c.get(u)
            except Exception as e:
                errors.append({"url": u, "error": repr(e)})
                continue
            if challenged(r):
                blocked.append({"url": u, "status": r.status_code,
                                "bytes": len(r.content)})
                streak += 1
                if streak >= GIVE_UP_AFTER:
                    print(f"  {GIVE_UP_AFTER} challenges in a row at {i}/"
                          f"{len(job_urls)} — stopping", file=sys.stderr)
                    break
                continue
            streak = 0

            posting = None
            for blob in LD.findall(r.text):
                try:
                    d = json.loads(blob)
                except json.JSONDecodeError:
                    continue
                if d.get("@type") == "JobPosting":
                    posting = d
                    break
            if posting is None:
                errors.append({"url": u, "error": "no JobPosting JSON-LD"})
                continue

            ident = posting.get("identifier") or {}
            req_ids = REQ_ID.findall(r.text)
            raw_loc = location_of(posting)
            city, country, remote = parse_location(raw_loc)
            title = posting.get("title", "")
            rows.append({
                "url": u,
                "uuid_url": bool(UUID_URL.search(u)),
                "title": title,
                "ld_identifier": ident.get("value") if isinstance(ident, dict) else ident,
                "page_requisition_id": req_ids[0] if req_ids else None,
                "posted": posting.get("datePosted"),
                "valid_through": posting.get("validThrough"),
                "employment_type": posting.get("employmentType"),
                "location_raw": raw_loc,
                "city": city, "country": country, "remote": remote,
                "australian": is_australian(city, country, raw_loc),
                "data_role": is_data_role(title),
                "has_description": bool(posting.get("description")),
            })
            if len(rows) % 10 == 0:
                print(f"  {i}/{len(job_urls)} ({len(rows)} parsed, "
                      f"{len(blocked)} challenged)", file=sys.stderr)

    au = [r for r in rows if r["australian"]]
    data_roles = [r for r in au if r["data_role"]]
    ld_ids = [r["ld_identifier"] for r in rows if r["ld_identifier"]]
    req_ids = [r["page_requisition_id"] for r in rows if r["page_requisition_id"]]
    dup_ld = {k: v for k, v in Counter(ld_ids).items() if v > 1}
    dup_req = {k: v for k, v in Counter(req_ids).items() if v > 1}

    result = {
        "delay_seconds": delay,
        "sitemap_urls": len(sitemap_urls),
        "sitemap_job_urls": len(job_urls),
        "search_pages": search_status,
        "search_job_urls": len(search_urls),
        "in_search_not_sitemap": sorted(search_urls - set(job_urls)),
        "in_sitemap_not_search": sorted(set(job_urls) - search_urls) if search_urls else [],
        "fetched": len(rows),
        "challenged": len(blocked),
        "errors": errors,
        "ld_identifier_present": len(ld_ids),
        "page_requisition_present": len(req_ids),
        "duplicate_ld_identifiers": dup_ld,
        "duplicate_requisition_ids": dup_req,
        "australian": len(au),
        "au_data_roles": len(data_roles),
        "au_data_titles": [r["title"] for r in data_roles],
        "countries": Counter(r["country"] for r in rows).most_common(),
        "rows": rows,
        "blocked": blocked,
    }
    if OUT.exists():   # keep a --slow-retest already on record
        prior = json.loads(OUT.read_text()).get("slow_retest")
        if prior:
            result["slow_retest"] = prior
    OUT.write_text(json.dumps(result, indent=1))

    print(f"\nfetched {len(rows)}/{len(job_urls)} job pages "
          f"({len(blocked)} challenged, {len(errors)} errors)", file=sys.stderr)
    print(f"JSON-LD identifier on {len(ld_ids)}/{len(rows)}; "
          f"page requisition id on {len(req_ids)}/{len(rows)}", file=sys.stderr)
    print(f"duplicate identifiers: {dup_ld or 'none'} / {dup_req or 'none'}",
          file=sys.stderr)
    print(f"search pagination saw {len(search_urls)} job urls; "
          f"{len(result['in_search_not_sitemap'])} not in sitemap", file=sys.stderr)
    print(f"Australian: {len(au)}/{len(rows)}   AU data roles: {len(data_roles)}",
          file=sys.stderr)
    for d in data_roles[:15]:
        print(f"   {d['title'][:60]:60} {d['city']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
