"""One-off: does Meta's careers sitemap contain any Australian roles?

Meta's robots.txt declares https://www.metacareers.com/jobsearch/sitemap.xml,
and every job page it lists carries a JSON-LD JobPosting block — the structured
data employers publish so aggregators can index them. So this reads the
sanctioned feed with an honest user agent; there is no spoofing involved.

The sitemap URL returns 400 to a browser-style Accept header and 200 to
`Accept: application/xml`, which is what made it look blocked at first.

A 24-page sample found zero Australian roles, which is too small to conclude
from. This sweeps all 900 to settle it before any adapter gets written.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from collections import Counter
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "meta_sitemap_probe.json"
SITEMAP = "https://www.metacareers.com/jobsearch/sitemap.xml"
UA = "reqtrace/0.1 (+personal job-search index)"
CONCURRENCY = 3          # deliberately gentle
LD = re.compile(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', re.S)


def countries_of(posting: dict) -> list[str]:
    jl = posting.get("jobLocation") or []
    jl = jl if isinstance(jl, list) else [jl]
    out = []
    for place in jl:
        if not isinstance(place, dict):
            continue
        addr = place.get("address") or {}
        out.append(addr.get("addressCountry") or place.get("name") or "")
    return [c for c in out if c]


async def main() -> int:
    async with httpx.AsyncClient(timeout=45, follow_redirects=True,
                                 headers={"User-Agent": UA,
                                          "Accept": "application/xml,text/xml"}) as c:
        r = await c.get(SITEMAP)
        r.raise_for_status()
        urls = re.findall(r"<loc>([^<]+)</loc>", r.text)
    print(f"sitemap: {len(urls)} job URLs", file=sys.stderr)

    rows, done = [], 0
    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(timeout=45, follow_redirects=True,
                                 headers={"User-Agent": UA, "Accept": "text/html"}) as c:
        async def one(u: str):
            nonlocal done
            async with sem:
                try:
                    t = (await c.get(u)).text
                except Exception:
                    t = ""
                done += 1
                if done % 100 == 0:
                    print(f"  {done}/{len(urls)} ({len(rows)} parsed)", file=sys.stderr)
            for blob in LD.findall(t):
                try:
                    d = json.loads(blob)
                except json.JSONDecodeError:
                    continue
                if d.get("@type") == "JobPosting":
                    rows.append({"url": u, "title": d.get("title", ""),
                                 "posted": d.get("datePosted"),
                                 "countries": countries_of(d)})
                    return

        await asyncio.gather(*(one(u) for u in urls))

    au = [r for r in rows
          if any(("AU" == c) or ("Australia" in c) or ("Sydney" in c) or ("Melbourne" in c)
                 for c in r["countries"])]
    OUT.write_text(json.dumps({"total_urls": len(urls), "parsed": len(rows),
                               "au": au, "rows": rows}, indent=1))
    print(f"\nparsed {len(rows)}/{len(urls)} JobPosting blocks", file=sys.stderr)
    print(f"countries: {Counter(c for r in rows for c in r['countries']).most_common(10)}",
          file=sys.stderr)
    print(f"AUSTRALIAN ROLES: {len(au)}", file=sys.stderr)
    for a in au[:15]:
        print(f"   {a['title'][:56]:56} {a['countries']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
