"""STEP 0 — ATS audit.

Resolve each seed company to its ATS vendor + board token, verified by actually
calling the vendor's public JSON feed. Two phases:

  1. Slug probe   — guess tokens from name/domain/hints, hit the JSON endpoint.
  2. Careers grep — for anything unresolved, fetch the careers page and look for
                    ATS domains in the markup. This is the only path that finds
                    Workday/PageUp, whose tokens are not guessable.

Throwaway audit code. The real adapters get written once, against the winner.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "data" / "companies_seed.csv"
OUT = ROOT / "data" / "step0_ats_audit.csv"
FIXTURES = ROOT / "fixtures"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
CONCURRENCY = 8
TIMEOUT = httpx.Timeout(25.0, connect=12.0)


# --------------------------------------------------------------------------
# vendor endpoint definitions
# --------------------------------------------------------------------------
# Verified empirically 2026-09-04. Note the differing "unknown token" semantics:
# GH/Lever/Ashby/Workable 404, but SmartRecruiters returns 200 + totalFound=0,
# so SR must be judged on job count, never on status code.

def _gh(tok: str) -> str:
    return f"https://boards-api.greenhouse.io/v1/boards/{tok}/jobs?content=true"

def _lever(tok: str) -> str:
    return f"https://api.lever.co/v0/postings/{tok}?mode=json"

def _ashby(tok: str) -> str:
    return f"https://api.ashbyhq.com/posting-api/job-board/{tok}?includeCompensation=true"

def _sr(tok: str) -> str:
    return f"https://api.smartrecruiters.com/v1/companies/{tok}/postings?limit=100"

def _workable(tok: str) -> str:
    return f"https://apply.workable.com/api/v1/widget/accounts/{tok}?details=true"


def _count_gh(d):       return len(d.get("jobs", [])) if isinstance(d, dict) else 0
def _count_lever(d):    return len(d) if isinstance(d, list) else 0
def _count_ashby(d):    return len(d.get("jobs", [])) if isinstance(d, dict) else 0
def _count_sr(d):       return int(d.get("totalFound", 0)) if isinstance(d, dict) else 0
def _count_workable(d): return len(d.get("jobs", [])) if isinstance(d, dict) else 0


VENDORS = {
    "greenhouse":     (_gh, _count_gh),
    "lever":          (_lever, _count_lever),
    "ashby":          (_ashby, _count_ashby),
    "smartrecruiters": (_sr, _count_sr),
    "workable":       (_workable, _count_workable),
}

# ATS fingerprints for the careers-page fallback. Order matters: the more
# specific API hostnames are listed before the human-facing board URLs.
FINGERPRINTS = [
    ("greenhouse",      re.compile(r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)")),
    ("greenhouse",      re.compile(r"(?:job-)?boards\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)")),
    ("greenhouse",      re.compile(r"greenhouse\.io/embed/job_board\?for=([A-Za-z0-9_-]+)")),
    ("lever",           re.compile(r"api\.lever\.co/v0/postings/([A-Za-z0-9_-]+)")),
    ("lever",           re.compile(r"jobs\.lever\.co/([A-Za-z0-9_-]+)")),
    ("ashby",           re.compile(r"api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9_-]+)")),
    ("ashby",           re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_-]+)")),
    ("smartrecruiters", re.compile(r"api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9_-]+)")),
    ("smartrecruiters", re.compile(r"careers\.smartrecruiters\.com/([A-Za-z0-9_-]+)")),
    ("workable",        re.compile(r"apply\.workable\.com/(?:api/v\d+/accounts/)?([A-Za-z0-9_-]+)")),
    ("avature",         re.compile(r"([A-Za-z0-9_-]+)\.avature\.net|avature\.portal")),
    ("oracle-orc",      re.compile(r"([a-z0-9-]+)\.fa\.[a-z0-9]+\.oraclecloud\.com/hcmUI/CandidateExperience")),
    ("teamtailor",      re.compile(r"([A-Za-z0-9_-]+)\.teamtailor\.com|teamtailor-cdn")),
    ("pageup",          re.compile(r"([A-Za-z0-9_-]+)\.(?:nga\.)?pageuppeople\.com|/caw/en/|en_GB/apply")),
    ("phenom",          re.compile(r"([A-Za-z0-9_-]+)\.phenompeople\.com")),
    ("eightfold",       re.compile(r"([A-Za-z0-9_-]+)\.eightfold\.ai")),
    ("workday",         re.compile(r"([A-Za-z0-9_-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:wday/cxs/[A-Za-z0-9_-]+/)?([A-Za-z0-9_-]+)")),
    ("pageup",          re.compile(r"([A-Za-z0-9_-]+)\.(?:nga\.)?pageuppeople\.com")),
    ("taleo",           re.compile(r"([A-Za-z0-9_-]+)\.taleo\.net")),
    ("successfactors",  re.compile(r"([A-Za-z0-9_-]+)\.(?:successfactors|sapsf)\.com")),
    ("icims",           re.compile(r"([A-Za-z0-9_-]+)\.icims\.com")),
]

# Tokens that are path noise rather than a board identifier.
STOPWORDS = {
    "embed", "job_board", "jobs", "api", "v0", "v1", "www", "static", "assets",
    "postings", "job-board", "companies", "accounts", "widget", "boards", "search",
}


@dataclass
class Company:
    name: str
    domain: str
    careers_url: str
    hints: str
    segment: str

    def candidates(self) -> list[str]:
        """Slug guesses, most-likely first, de-duplicated."""
        out: list[str] = []
        for h in filter(None, self.hints.split("|")):
            out.append(h.strip())
        squashed = re.sub(r"[^a-z0-9]", "", self.name.lower())
        hyphen = re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-")
        root = self.domain.split(".")[0].lower()
        out += [squashed, hyphen, root]
        seen, uniq = set(), []
        for c in out:
            if c and c not in seen:
                seen.add(c)
                uniq.append(c)
        return uniq[:5]


@dataclass
class Result:
    company: Company
    vendor: str = ""
    token: str = ""
    board_url: str = ""
    job_count: int = 0
    verified_via: str = ""
    notes: str = ""
    extra: dict = field(default_factory=dict)


async def get_json(client, url, *, attempts=3):
    """GET returning (status, parsed_json_or_None). Retries 5xx/network only —
    404s are the expected outcome of a wrong slug and must not be retried."""
    delay = 1.0
    for i in range(attempts):
        try:
            r = await client.get(url)
        except (httpx.TimeoutException, httpx.TransportError):
            if i == attempts - 1:
                return None, None
            await asyncio.sleep(delay)
            delay *= 2
            continue
        if r.status_code >= 500 and i < attempts - 1:
            await asyncio.sleep(delay)
            delay *= 2
            continue
        if r.status_code != 200:
            return r.status_code, None
        try:
            return 200, r.json()
        except (json.JSONDecodeError, ValueError):
            return 200, None
    return None, None


async def probe_company(client, sem, co: Company) -> Result:
    """Phase 1: try every (vendor, slug) pair; keep the board with most jobs."""
    best = Result(company=co)
    for token in co.candidates():
        for vendor, (url_fn, count_fn) in VENDORS.items():
            async with sem:
                status, data = await get_json(client, url_fn(token))
            if status != 200 or data is None:
                continue
            n = count_fn(data)
            if n > best.job_count:
                best = Result(
                    company=co, vendor=vendor, token=token, board_url=url_fn(token),
                    job_count=n, verified_via="endpoint-200",
                    notes=f"{n} open roles",
                )
                best.extra["payload"] = data
    return best


def scan_html(html: str, page_url: str) -> list[tuple]:
    """Return (vendor, token, detail) hits found in careers-page markup."""
    hits = []
    for vendor, pat in FINGERPRINTS:
        for m in pat.finditer(html):
            if vendor == "workday":
                tenant, wd, site = m.group(1), m.group(2), m.group(3)
                hits.append((vendor, tenant, f"{tenant}.{wd}|site={site}"))
            else:
                tok = m.group(1) or vendor
                if tok.lower() in STOPWORDS or len(tok) < 2:
                    continue
                hits.append((vendor, tok, ""))
    # de-duplicate, preserving discovery order
    seen, out = set(), []
    for h in hits:
        if h[:2] not in seen:
            seen.add(h[:2])
            out.append(h)
    return out


async def careers_lookup(client, sem, res: Result) -> Result:
    """Phase 2: fetch the careers page and fingerprint the markup."""
    co = res.company
    urls = [u for u in (co.careers_url, f"https://{co.domain}/careers") if u]
    for url in urls:
        async with sem:
            try:
                r = await client.get(url)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                res.notes = f"careers page unreachable ({type(e).__name__})"
                continue
        if r.status_code != 200:
            res.notes = f"careers page HTTP {r.status_code}"
            continue
        hits = scan_html(r.text, str(r.url))
        if hits:
            vendor, token, detail = hits[0]
            res.vendor, res.token = vendor, token
            res.verified_via = "careers-page-html"
            res.board_url = str(r.url)
            res.notes = detail or "found in careers page markup"
            res.extra["all_hits"] = "; ".join(f"{v}:{t}" for v, t, _ in hits[:6])
            return res
        res.notes = res.notes or "no ATS fingerprint in careers markup"
    return res


def save_fixture(res: Result):
    if not res.vendor or "payload" not in res.extra:
        return
    d = FIXTURES / res.vendor
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{res.token}.json").write_text(
        json.dumps(res.extra["payload"], indent=1)[:12_000_000]
    )


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", default=str(SEED), help="company seed CSV")
    ap.add_argument("--out", default=str(OUT), help="audit CSV to write")
    args = ap.parse_args()
    seed_path, out_path = Path(args.seed), Path(args.out)

    rows = list(csv.DictReader(seed_path.open()))
    companies = [Company(r["name"], r["domain"], r["careers_url"], r["hints"], r["segment"])
                 for r in rows]
    sem = asyncio.Semaphore(CONCURRENCY)
    limits = httpx.Limits(max_connections=CONCURRENCY + 4)

    async with httpx.AsyncClient(
        timeout=TIMEOUT, follow_redirects=True,
        headers={"User-Agent": UA, "Accept": "application/json,text/html;q=0.9"},
        limits=limits,
    ) as client:
        print(f"phase 1: slug-probing {len(companies)} companies "
              f"across {len(VENDORS)} vendors ...", file=sys.stderr)
        results = await asyncio.gather(*(probe_company(client, sem, c) for c in companies))

        unresolved = [r for r in results if not r.vendor]
        print(f"phase 1: resolved {len(results) - len(unresolved)}, "
              f"unresolved {len(unresolved)}", file=sys.stderr)

        print(f"phase 2: careers-page fingerprint for {len(unresolved)} ...", file=sys.stderr)
        await asyncio.gather(*(careers_lookup(client, sem, r) for r in unresolved))

    for r in results:
        save_fixture(r)

    with out_path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["company", "domain", "segment", "ats_vendor", "board_token",
                    "careers_url", "job_count", "verified_via", "notes", "other_hits"])
        for r in sorted(results, key=lambda x: (not x.vendor, x.company.name.lower())):
            w.writerow([r.company.name, r.company.domain, r.company.segment,
                        r.vendor, r.token, r.company.careers_url, r.job_count or "",
                        r.verified_via, r.notes, r.extra.get("all_hits", "")])
    print(f"wrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
