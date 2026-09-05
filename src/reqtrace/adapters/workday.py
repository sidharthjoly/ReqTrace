"""Workday adapter.

The single biggest coverage win available: 12 known employers — CommBank,
Telstra, Accenture, Cochlear and Nine locally, plus Coca-Cola, J&J, Novartis,
Pfizer, Shell, Unilever and Visa globally. Enterprises buy HR suites, and
Workday is the one they buy most.

Awkward in three specific ways, all handled here:

  * **Tokens are not guessable.** A board needs tenant + `wd{N}` host + site
    path, so `board_token` encodes all three as `tenant.wdN/SitePath`
    (e.g. `cba.wd3/CommBank_Careers`). These come from careers-page crawling,
    never from slug guessing.
  * **`limit` caps at 20**, and asking for more returns nothing at all rather
    than clamping — so a 2,000-job board is 100 requests. We still page the
    whole board, because filtering server-side by location would make
    "complete" mean "complete subset" and leave closure detection at the mercy
    of a facet id. Facet ids turn out to be tenant-specific anyway: the country
    GUID that filters CommBank returns nothing for NVIDIA.
  * **The listing has no description**, and `postedOn` is a relative string
    ("Posted 30+ Days Ago"). Both come from a per-job detail request, which —
    as with SmartRecruiters — is made only for jobs that might be Australian.
    The detail response carries a real `startDate` and an authoritative
    `country`.
"""

from __future__ import annotations

import asyncio
import re
import sys
from typing import Annotated

import httpx
from pydantic import BaseModel, BeforeValidator, Field

from ..models import BoardSnapshot, Job, content_hash
from ..normalise import (
    html_to_text, is_australian, maybe_australian, parse_location, sanitise_html, to_iso2,
)
from .base import BoardIncomplete, polite_retry

TOKEN_RE = re.compile(r"^(?P<tenant>[A-Za-z0-9_-]+)\.(?P<wd>wd\d+)/(?P<site>.+)$")
PAGE = 20  # hard vendor cap; 50 or 100 returns an empty body


def _blank_if_none(v):
    return v or ""


NullableStr = Annotated[str, BeforeValidator(_blank_if_none)]


def parse_token(token: str) -> tuple[str, str, str]:
    m = TOKEN_RE.match(token)
    if not m:
        raise BoardIncomplete(
            f"workday token must look like 'tenant.wdN/SitePath', got {token!r}")
    return m["tenant"], m["wd"], m["site"]


def base_url(token: str) -> str:
    tenant, wd, site = parse_token(token)
    return f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"


class WdPosting(BaseModel):
    title: NullableStr = ""
    externalPath: NullableStr = ""
    locationsText: NullableStr = ""
    postedOn: NullableStr = ""
    bulletFields: list[str] = Field(default_factory=list)


class WdBoard(BaseModel):
    total: int = 0
    jobPostings: list[WdPosting] = Field(default_factory=list)


def external_id(raw: WdPosting) -> str:
    """Prefer the requisition id in bulletFields — it survives a title edit,
    which `externalPath` does not, and job identity must be stable."""
    for field in raw.bulletFields:
        f = (field or "").strip()
        if f and " " not in f:
            return f
    return raw.externalPath


def map_job(raw: WdPosting, token: str, raw_dict: dict, detail: dict | None = None) -> Job:
    loc = raw.locationsText
    city, country, remote = parse_location(loc)

    info = (detail or {}).get("jobPostingInfo") or {}
    # The detail response knows the country outright; the listing string is a
    # guess. "NSW/ ACT Region" is not provably Australian without it.
    if info.get("country"):
        country = to_iso2((info["country"] or {}).get("descriptor")) or country
    if not city and info.get("location"):
        city = parse_location(info["location"])[0]

    html = sanitise_html(info.get("jobDescription", "")) if info else ""
    tenant, wd, site = parse_token(token)
    return Job(
        ats_vendor="workday",
        board_token=token,
        external_id=external_id(raw),
        title=(info.get("title") or raw.title).strip(),
        description_html=html,
        description_text=html_to_text(html) if html else "",
        location_raw=loc or info.get("location", ""),
        location_city=city,
        location_country=country,
        remote_type="remote" if re.search(r"\bremote\b", loc, re.I) else remote,
        employment_type=info.get("timeType") or None,
        apply_url=info.get("externalUrl")
        or f"https://{tenant}.{wd}.myworkdayjobs.com/{site}{raw.externalPath}",
        # `postedOn` is relative text; startDate is a real date.
        posted_at=info.get("startDate") or None,
        content_hash=content_hash(raw_dict),
    )


class WorkdayAdapter:
    vendor = "workday"

    def parse(self, payload: dict, token: str,
              details: dict[str, dict] | None = None) -> BoardSnapshot:
        if not isinstance(payload, dict) or not isinstance(payload.get("jobPostings"), list):
            raise BoardIncomplete(f"workday:{token} unexpected envelope")

        board = WdBoard.model_validate(payload)
        # Some tenants CAP the reported total (Accenture reports exactly 2000
        # while still serving pages at offset 2000+). `len == total` is then
        # True on a board we only partly fetched, closure detection fires, and
        # every job past the window is falsely retired — with the window
        # shifting between runs. _all_pages probes one page past the reported
        # end and sets this flag when the vendor is under-reporting.
        complete = (len(board.jobPostings) == board.total
                    and not payload.get("_truncated", False))
        details = details or {}
        raw_by_path = {p.get("externalPath"): p for p in payload["jobPostings"]}
        jobs = [
            map_job(p, token, raw_by_path.get(p.externalPath, {}),
                    details.get(external_id(p)))
            for p in board.jobPostings
        ]
        return BoardSnapshot(ats_vendor=self.vendor, board_token=token,
                             complete=complete, jobs=jobs)

    @polite_retry
    async def _page(self, client: httpx.AsyncClient, token: str, offset: int) -> dict:
        r = await client.post(
            f"{base_url(token)}/jobs",
            json={"appliedFacets": {}, "limit": PAGE, "offset": offset, "searchText": ""},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        r.raise_for_status()
        return r.json()

    @polite_retry
    async def _detail(self, client: httpx.AsyncClient, token: str, path: str) -> dict:
        r = await client.get(f"{base_url(token)}{path}",
                             headers={"Accept": "application/json"})
        r.raise_for_status()
        return r.json()

    async def _all_pages(self, client: httpx.AsyncClient, token: str,
                         max_pages: int = 200) -> dict:
        first = await self._page(client, token, 0)
        total = first.get("total", 0)
        postings = list(first.get("jobPostings", []))
        offset = len(postings)
        pages = 1
        while offset < total and postings and pages < max_pages:
            page = await self._page(client, token, offset)
            got = page.get("jobPostings", [])
            if not got:
                break
            postings += got
            offset += len(got)
            pages += 1

        # Probe one page past the reported end. Workday CLAMPS an out-of-range
        # offset and re-serves a page rather than returning an empty list, so
        # "the probe returned rows" is not evidence of truncation — most
        # tenants hand back postings we already have. Only genuinely NEW
        # postings prove `total` was a cap.
        truncated = pages >= max_pages or total >= self.RESULT_CAP
        if total >= self.RESULT_CAP:
            print(f"  workday:{token} reports exactly {total}, Workday's public "
                  f"result cap — cannot prove the board is whole, so no closures",
                  file=sys.stderr)
        if not truncated and total and len(postings) >= total:
            try:
                probe = await self._page(client, token, total)
                have = {p.get("externalPath") for p in postings}
                truncated = any(p.get("externalPath") not in have
                                for p in probe.get("jobPostings", []))
            except Exception:
                truncated = True  # cannot prove completeness -> fail closed
            if truncated:
                print(f"  workday:{token} reports total={total} but serves more; "
                      f"treating board as incomplete (no closures)", file=sys.stderr)
        return {"total": total, "jobPostings": postings, "_truncated": truncated}

    # Bound on per-board detail requests. Boards that publish no location in
    # the listing (Accenture) make every posting a candidate, and an unbounded
    # fan-out would be an impolite number of requests to one host. Enrichment is
    # not identity, so a capped board still lists and still closes correctly —
    # it just may not classify every role's country.
    DETAIL_CAP = 800

    # Workday's public search stops reporting past 2,000 results. A board that
    # reports exactly this is indistinguishable from one that has more and is
    # being truncated — and if it IS truncated, the visible window shifts as
    # roles are posted, so jobs that fell out would be retired while still
    # open. We cannot prove it either way, so we decline to close on such a
    # board: stale rows are recoverable, a corrupted closed_at history is not.
    RESULT_CAP = 2000

    async def _descriptions(self, client: httpx.AsyncClient, token: str,
                            postings: list[dict], concurrency: int = 3) -> dict[str, dict]:
        """Open only the jobs that might be Australian — see `maybe_australian`."""
        wanted = [p for p in postings
                  if maybe_australian(p.get("locationsText") or "")]
        if len(wanted) > self.DETAIL_CAP:
            print(f"  workday:{token} {len(wanted)} postings need a detail fetch; "
                  f"capping at {self.DETAIL_CAP} (country unclassified beyond that)",
                  file=sys.stderr)
            wanted = wanted[: self.DETAIL_CAP]
        out: dict[str, dict] = {}
        sem = asyncio.Semaphore(concurrency)

        async def one(p: dict):
            async with sem:
                try:
                    out[external_id(WdPosting.model_validate(p))] = await self._detail(
                        client, token, p["externalPath"])
                except Exception:
                    pass  # a missing body must not fail the board

        await asyncio.gather(*(one(p) for p in wanted))
        return out

    async def fetch(self, client: httpx.AsyncClient, token: str) -> BoardSnapshot:
        payload = await self._all_pages(client, token)
        details = await self._descriptions(client, token, payload["jobPostings"])
        return self.parse(payload, token, details)
