"""SmartRecruiters adapter — adapter #3.

Promoted into v1 by the Step 0 audit: the highest AU density of any vendor
(46% of its jobs are Australian) and it carries Canva, SEEK and carsales.

Two things make it the awkward one:

  * it pages at 100, so completeness has to be reconciled against `totalFound`
  * its listing carries **no description at all**; each one is a separate
    request. Fetching all of them would mean 412 requests for a 411-job board,
    so descriptions are fetched only for jobs we would actually store a body
    for — the Australian ones. That mirrors the storage policy in store.py and
    turns an N+1 over the whole board into an N+1 over ~3% of it.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import httpx
from pydantic import BaseModel, BeforeValidator, Field

from ..models import BoardSnapshot, Job, content_hash
from ..normalise import html_to_text, is_australian, sanitise_html, to_iso2
from .base import BoardIncomplete, polite_retry

LIST_URL = ("https://api.smartrecruiters.com/v1/companies/{token}/postings"
            "?limit={limit}&offset={offset}")
DETAIL_URL = "https://api.smartrecruiters.com/v1/companies/{token}/postings/{job_id}"
PAGE = 100


def _blank_if_none(v):
    return v or ""


NullableStr = Annotated[str, BeforeValidator(_blank_if_none)]


class SrLocation(BaseModel):
    city: NullableStr = ""
    region: NullableStr = ""
    country: NullableStr = ""
    remote: bool | None = None
    hybrid: bool | None = None
    fullLocation: NullableStr = ""


class SrLabelled(BaseModel):
    id: NullableStr = ""
    label: NullableStr = ""


class SrPosting(BaseModel):
    id: str
    name: NullableStr = ""
    refNumber: NullableStr = ""
    releasedDate: str | None = None
    location: SrLocation = Field(default_factory=SrLocation)
    department: SrLabelled = Field(default_factory=SrLabelled)
    function: SrLabelled = Field(default_factory=SrLabelled)
    typeOfEmployment: SrLabelled = Field(default_factory=SrLabelled)
    experienceLevel: SrLabelled = Field(default_factory=SrLabelled)


class SrBoard(BaseModel):
    totalFound: int = 0
    content: list[SrPosting] = Field(default_factory=list)


# SmartRecruiters splits the ad across named sections; the job body is the
# description plus qualifications. companyDescription is boilerplate repeated on
# every posting, so it is deliberately left out of the searchable text.
BODY_SECTIONS = ("jobDescription", "qualifications", "additionalInformation")


def sections_to_html(detail: dict) -> str:
    parts = []
    for key in BODY_SECTIONS:
        sec = ((detail.get("jobAd") or {}).get("sections") or {}).get(key) or {}
        text = sec.get("text") or ""
        if text.strip():
            parts.append(f"<h3>{sec.get('title') or key}</h3>{text}")
    return "".join(parts)


def apply_url(token: str, raw: SrPosting) -> str:
    return f"https://jobs.smartrecruiters.com/{token}/{raw.id}"


def map_job(raw: SrPosting, token: str, raw_dict: dict, detail: dict | None = None) -> Job:
    loc = raw.location
    # SmartRecruiters states remote and hybrid as separate booleans, so the
    # hybrid-labelled-remote distinction comes free — hybrid wins when both set.
    if loc.hybrid:
        remote = "hybrid"
    elif loc.remote:
        remote = "remote"
    elif loc.city or loc.country:
        remote = "onsite"
    else:
        remote = "unknown"

    html = sanitise_html(sections_to_html(detail)) if detail else ""
    return Job(
        ats_vendor="smartrecruiters",
        board_token=token,
        external_id=raw.id,
        title=raw.name.strip(),
        description_html=html,
        description_text=html_to_text(html) if html else "",
        location_raw=loc.fullLocation or " ".join(filter(None, [loc.city, loc.country])),
        location_city=loc.city or None,
        location_country=to_iso2(loc.country),
        remote_type=remote,
        department=raw.department.label or None,
        function=raw.function.label or None,
        employment_type=raw.typeOfEmployment.label or None,
        seniority=raw.experienceLevel.label or None,
        apply_url=apply_url(token, raw),
        posted_at=raw.releasedDate,
        content_hash=content_hash(raw_dict),
    )


class SmartRecruitersAdapter:
    vendor = "smartrecruiters"

    def parse(self, payload: dict, token: str,
              details: dict[str, dict] | None = None) -> BoardSnapshot:
        if not isinstance(payload, dict) or not isinstance(payload.get("content"), list):
            raise BoardIncomplete(f"smartrecruiters:{token} unexpected envelope")

        board = SrBoard.model_validate(payload)
        # An unknown SmartRecruiters company answers 200 with totalFound 0
        # rather than 404, so completeness must be judged on the count, never on
        # the status code.
        complete = len(board.content) == board.totalFound
        raw_by_id = {j["id"]: j for j in payload["content"]}
        details = details or {}
        jobs = [map_job(j, token, raw_by_id.get(j.id, {}), details.get(j.id))
                for j in board.content]
        return BoardSnapshot(ats_vendor=self.vendor, board_token=token,
                             complete=complete, jobs=jobs)

    @polite_retry
    async def _get(self, client: httpx.AsyncClient, url: str) -> dict:
        r = await client.get(url)
        r.raise_for_status()
        return r.json()

    async def _all_pages(self, client: httpx.AsyncClient, token: str) -> dict:
        content, offset, total = [], 0, None
        while True:
            page = await self._get(
                client, LIST_URL.format(token=token, limit=PAGE, offset=offset))
            total = page.get("totalFound", 0)
            got = page.get("content", [])
            content += got
            offset += len(got)
            if not got or offset >= total:
                break
        return {"totalFound": total, "content": content}

    async def _descriptions(self, client: httpx.AsyncClient, token: str,
                            postings: list[dict], concurrency: int = 4) -> dict[str, dict]:
        """Fetch bodies only for the AU roles — see the module docstring."""
        wanted = []
        for p in postings:
            loc = p.get("location") or {}
            raw = loc.get("fullLocation") or " ".join(
                filter(None, [loc.get("city"), loc.get("country")]))
            if is_australian(loc.get("city"), to_iso2(loc.get("country")), raw):
                wanted.append(p["id"])

        out: dict[str, dict] = {}
        sem = asyncio.Semaphore(concurrency)

        async def one(job_id: str):
            async with sem:
                try:
                    out[job_id] = await self._get(
                        client, DETAIL_URL.format(token=token, job_id=job_id))
                except Exception:
                    pass  # a missing body must not fail the board

        await asyncio.gather(*(one(i) for i in wanted))
        return out

    async def fetch(self, client: httpx.AsyncClient, token: str) -> BoardSnapshot:
        payload = await self._all_pages(client, token)
        details = await self._descriptions(client, token, payload["content"])
        return self.parse(payload, token, details)
