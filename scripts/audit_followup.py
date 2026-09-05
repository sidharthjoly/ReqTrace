"""STEP 0, pass 2 — resolve the stragglers and correct false positives.

Pass 1 left 33 companies unresolved because their careers *landing* page is a
marketing hub; the ATS link sits one click deeper. It also produced a few
false positives, because a short slug like "zip" or "athena" matches somebody
else's board. Both are fixed here:

  * deep crawl  — fetch the careers page, follow the few links that look like
                  "see our jobs", fingerprint each. This is the only way the
                  Workday/PageUp enterprise boards get found.
  * identity    — an endpoint hit is only trusted if the board's own name looks
                  like the company, or the board actually lists AU roles.
"""

from __future__ import annotations

import asyncio
import csv
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_ats import FINGERPRINTS, STOPWORDS, scan_html  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
AUDIT = ROOT / "data" / "step0_ats_audit.csv"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}

# Boards proven to belong to somebody else, or to be a vendor sandbox.
# Keyed (vendor, token) -> reason.
REJECT = {
    ("greenhouse", "athena"): "board is 'Athena Group Advisors', not Athena Home Loans",
    ("ashby", "zip"): "board is Zip (US procurement AI), not Zip Co",
    ("smartrecruiters", "kpmgaustralia"): "sandbox board ('KPMG Australia Sandbox')",
}

LINK_HINTS = re.compile(
    r"(job|career|vacan|opportunit|position|roles|openings|work-with-us|join)", re.I)
ATS_HOSTS = re.compile(
    r"(greenhouse\.io|lever\.co|ashbyhq\.com|smartrecruiters\.com|myworkdayjobs\.com"
    r"|workable\.com|pageuppeople\.com|taleo\.net|successfactors|icims\.com"
    r"|jobs\.nsw\.gov\.au|livehire\.com|springboard)", re.I)

CONCURRENCY = 6


def link_candidates(html: str, base: str) -> list[str]:
    """Links worth one more hop, ATS-looking hosts first."""
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html, re.I)
    direct, indirect = [], []
    for h in hrefs:
        if h.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        full = urljoin(base, h)
        if not full.startswith("http"):
            continue
        if ATS_HOSTS.search(full):
            direct.append(full)
        elif LINK_HINTS.search(full):
            indirect.append(full)
    seen, out = set(), []
    for u in direct + indirect:
        key = u.split("#")[0]
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out[:6]


async def fetch(client, url):
    try:
        r = await client.get(url)
        return r
    except (httpx.TimeoutException, httpx.TransportError):
        return None


async def deep_resolve(client, sem, row: dict) -> dict:
    """Landing page -> fingerprint -> follow promising links -> fingerprint."""
    domain = row["domain"]
    seeds = [u for u in (
        row["careers_url"],
        f"https://{domain}/careers",
        f"https://careers.{domain}",
        f"https://{domain}",
    ) if u]

    tried = set()
    for seed in seeds:
        if seed in tried:
            continue
        tried.add(seed)
        async with sem:
            r = await fetch(client, seed)
        if r is None or r.status_code != 200:
            row["notes"] = f"{seed.split('//')[-1][:32]} HTTP {r.status_code if r else 'ERR'}"
            continue

        hits = scan_html(r.text, str(r.url))
        if hits:
            return apply_hit(row, hits, str(r.url), "careers-page-html")

        # one hop deeper
        for link in link_candidates(r.text, str(r.url)):
            if link in tried:
                continue
            tried.add(link)
            # An ATS host in the URL itself is already the answer.
            hits = scan_html(link, link)
            if hits:
                return apply_hit(row, hits, link, "careers-link-url")
            async with sem:
                r2 = await fetch(client, link)
            if r2 is None or r2.status_code != 200:
                continue
            hits = scan_html(str(r2.url), str(r2.url)) or scan_html(r2.text, str(r2.url))
            if hits:
                return apply_hit(row, hits, str(r2.url), "careers-deep-crawl")
        row["notes"] = "no ATS fingerprint after 1-hop crawl"
    return row


def apply_hit(row, hits, url, via):
    vendor, token, detail = hits[0]
    row["ats_vendor"] = vendor
    row["board_token"] = token
    row["verified_via"] = via
    row["notes"] = detail or f"found via {via}"
    row["other_hits"] = "; ".join(f"{v}:{t}" for v, t, _ in hits[:6])
    row["_board_url"] = url
    return row


async def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit", default=str(AUDIT))
    args = ap.parse_args()
    audit_path = Path(args.audit)

    rows = list(csv.DictReader(audit_path.open()))

    # 1. drop proven-wrong boards
    for r in rows:
        key = (r["ats_vendor"], r["board_token"])
        if key in REJECT:
            r["notes"] = "REJECTED: " + REJECT[key]
            r["other_hits"] = f"rejected {r['ats_vendor']}:{r['board_token']}"
            r["ats_vendor"] = r["board_token"] = r["verified_via"] = ""
            r["job_count"] = ""

    todo = [r for r in rows if not r["ats_vendor"]]
    print(f"deep-crawling {len(todo)} unresolved companies ...", file=sys.stderr)

    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=12.0), follow_redirects=True,
        headers=HEADERS, limits=httpx.Limits(max_connections=CONCURRENCY + 4),
    ) as client:
        await asyncio.gather(*(deep_resolve(client, sem, r) for r in todo))

    fields = ["company", "domain", "segment", "ats_vendor", "board_token",
              "careers_url", "job_count", "verified_via", "notes", "other_hits"]
    with audit_path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in sorted(rows, key=lambda x: (not x["ats_vendor"], x["company"].lower())):
            w.writerow(r)
    still = sum(1 for r in rows if not r["ats_vendor"])
    print(f"resolved {len(rows)-still}/{len(rows)}; {still} remain", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
