"""Re-fetch complete boards for the v1 vendors and emit test-sized samples.

Two things this fixes over the audit's opportunistic dumps:

  * completeness — SmartRecruiters pages at 100, so the audit's Canva fixture was
    page 1 of 3. `job_count` must mean "jobs actually on disk" for every vendor,
    or the audit table silently compares different quantities.
  * size — a test needs five jobs, not Airwallex's 579 with full HTML bodies.
    Full dumps go to fixtures/raw/ (git-ignored); trimmed samples are committed.
"""
from __future__ import annotations

import asyncio, csv, json, sys
from pathlib import Path
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from reqtrace.models import token_slug  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
AUDIT = ROOT / "data" / "step0_ats_audit.csv"
RAW = ROOT / "fixtures" / "raw"
SAMPLES = ROOT / "fixtures" / "samples"
V1 = {"greenhouse", "lever", "ashby", "smartrecruiters", "workable"}
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"


async def fetch_board(client, vendor: str, token: str):
    """Return (payload, n_jobs). SmartRecruiters is the only paged vendor."""
    if vendor == "greenhouse":
        r = await client.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")
        r.raise_for_status(); d = r.json(); return d, len(d.get("jobs", []))
    if vendor == "lever":
        r = await client.get(f"https://api.lever.co/v0/postings/{token}?mode=json")
        r.raise_for_status(); d = r.json(); return d, len(d)
    if vendor == "ashby":
        r = await client.get(f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true")
        r.raise_for_status(); d = r.json(); return d, len(d.get("jobs", []))
    if vendor == "workable":
        r = await client.get(f"https://apply.workable.com/api/v1/widget/accounts/{token}?details=true")
        r.raise_for_status(); d = r.json(); return d, len(d.get("jobs", []))
    if vendor == "smartrecruiters":
        content, offset, total = [], 0, None
        while True:
            r = await client.get(
                f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100&offset={offset}")
            r.raise_for_status(); d = r.json()
            total = d.get("totalFound", 0)
            page = d.get("content", [])
            content += page
            offset += len(page)
            if not page or offset >= total:
                break
        return {"totalFound": total, "content": content}, len(content)
    raise ValueError(vendor)


def sample_of(vendor: str, payload, n=5):
    if vendor == "lever":
        return payload[:n]
    if vendor == "smartrecruiters":
        return {"totalFound": payload["totalFound"], "content": payload["content"][:n]}
    key = "jobs"
    out = dict(payload)
    out[key] = payload.get(key, [])[:n]
    return out


async def main():
    rows = list(csv.DictReader(AUDIT.open()))
    targets = [(r, r["ats_vendor"], r["board_token"]) for r in rows
               if r["ats_vendor"] in V1 and r["board_token"]]
    RAW.mkdir(parents=True, exist_ok=True); SAMPLES.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(6)

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0),
                                 headers={"User-Agent": UA}, follow_redirects=True) as client:
        async def one(row, vendor, token):
            async with sem:
                try:
                    payload, n = await fetch_board(client, vendor, token)
                except Exception as e:
                    print(f"  !! {vendor}:{token} {type(e).__name__}", file=sys.stderr)
                    row["job_count"] = ""; row["notes"] = f"refetch failed: {type(e).__name__}"
                    return
            (RAW / vendor).mkdir(parents=True, exist_ok=True)
            (RAW / vendor / f"{token_slug(token)}.json").write_text(json.dumps(payload, indent=1))
            (SAMPLES / vendor).mkdir(parents=True, exist_ok=True)
            (SAMPLES / vendor / f"{token_slug(token)}.json").write_text(json.dumps(sample_of(vendor, payload), indent=1))
            row["job_count"] = str(n)
            row["verified_via"] = "endpoint-200"
            row["notes"] = f"{n} open roles (complete board)"
            print(f"  {vendor:16} {token:16} {n:>4}", file=sys.stderr)

        await asyncio.gather(*(one(r, v, t) for r, v, t in targets))

    with AUDIT.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys())); w.writeheader()
        w.writerows(sorted(rows, key=lambda x: (not x["ats_vendor"], x["company"].lower())))
    print("audit CSV job_count now means 'jobs on disk' for every vendor", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
