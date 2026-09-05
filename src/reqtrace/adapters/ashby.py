"""Ashby adapter — adapter #2.

Discovery found 106 AU boards on Ashby carrying 110 AU data roles, more than
Greenhouse. Ashby is also the richest of the three feeds: it returns a
structured postal address, an explicit workplace type, and — with
`includeCompensation=true` — machine-readable salary, which is hard problem #3
solved without regex.
"""

from __future__ import annotations

from typing import Annotated

import httpx
from pydantic import BaseModel, BeforeValidator, Field

from ..models import BoardSnapshot, Job, content_hash
from ..normalise import html_to_text, parse_location, sanitise_html, to_iso2
from .base import BoardIncomplete, polite_retry

BOARD_URL = ("https://api.ashbyhq.com/posting-api/job-board/{token}"
             "?includeCompensation=true")


def _blank_if_none(v):
    return v or ""


NullableStr = Annotated[str, BeforeValidator(_blank_if_none)]


# --- raw vendor shapes -----------------------------------------------------

class AshbyPostalAddress(BaseModel):
    addressLocality: NullableStr = ""
    addressRegion: NullableStr = ""
    addressCountry: NullableStr = ""


class AshbyAddress(BaseModel):
    postalAddress: AshbyPostalAddress | None = None


class AshbyComponent(BaseModel):
    compensationType: NullableStr = ""
    interval: NullableStr = ""
    currencyCode: str | None = None
    minValue: float | None = None
    maxValue: float | None = None


class AshbyCompensation(BaseModel):
    summaryComponents: list[AshbyComponent] = Field(default_factory=list)


class AshbyJob(BaseModel):
    id: str
    title: NullableStr = ""
    location: NullableStr = ""
    department: NullableStr = ""
    team: NullableStr = ""
    employmentType: NullableStr = ""
    descriptionHtml: NullableStr = ""
    descriptionPlain: NullableStr = ""
    jobUrl: NullableStr = ""
    applyUrl: NullableStr = ""
    publishedAt: str | None = None
    isListed: bool = True
    isRemote: bool | None = None
    workplaceType: str | None = None
    address: AshbyAddress | None = None
    compensation: AshbyCompensation | None = None


class AshbyBoard(BaseModel):
    jobs: list[AshbyJob] = Field(default_factory=list)
    apiVersion: str | None = None


# --- mapping ---------------------------------------------------------------

# `isRemote` is True for hybrid roles too — in the recorded boards it is True on
# 260 jobs while only 11 have workplaceType "Remote". Trusting it would wildly
# over-report remote work, which is the single filter that matters most here.
# workplaceType is the honest field.
WORKPLACE = {"Remote": "remote", "Hybrid": "hybrid", "OnSite": "onsite"}

INTERVALS = {"1 YEAR": "year", "1 MONTH": "month", "1 WEEK": "week",
             "1 DAY": "day", "1 HOUR": "hour"}


def _salary(comp: AshbyCompensation | None):
    """Ashby publishes structured compensation, so no regex is needed. Only the
    Salary component is used — Bonus/Equity/Commission are real but are not a
    salary band, and folding them in would corrupt the range."""
    if comp is None:
        return None, None, None, None
    for c in comp.summaryComponents:
        if c.compensationType == "Salary" and (c.minValue or c.maxValue):
            return (c.minValue, c.maxValue, c.currencyCode,
                    INTERVALS.get(c.interval.upper(), None))
    return None, None, None, None


def _location(raw: AshbyJob):
    """Prefer the structured postal address; fall back to parsing the display
    string, which is where things like 'Sydney or Melbourne' live."""
    city = country = None
    post = raw.address.postalAddress if raw.address else None
    if post:
        city = post.addressLocality.strip() or None
        country = to_iso2(post.addressCountry)
    if city is None or country is None:
        pc, pcountry, _ = parse_location(raw.location)
        city = city or pc
        country = country or pcountry
    return city, country


def map_job(raw: AshbyJob, token: str, raw_dict: dict) -> Job:
    city, country = _location(raw)
    remote = WORKPLACE.get(raw.workplaceType or "", "unknown")
    if remote == "unknown":
        remote = parse_location(raw.location)[2]

    smin, smax, cur, period = _salary(raw.compensation)
    html = sanitise_html(raw.descriptionHtml)
    return Job(
        ats_vendor="ashby",
        board_token=token,
        external_id=raw.id,
        title=raw.title.strip(),
        description_html=html,
        description_text=raw.descriptionPlain.strip() or html_to_text(raw.descriptionHtml),
        location_raw=raw.location,
        location_city=city,
        location_country=country,
        remote_type=remote,
        salary_min=smin,
        salary_max=smax,
        salary_currency=cur,
        salary_period=period,
        department=raw.department or None,
        employment_type=raw.employmentType or None,
        function=raw.team or None,
        apply_url=raw.applyUrl or raw.jobUrl,
        posted_at=raw.publishedAt,
        content_hash=content_hash(raw_dict),
    )


class AshbyAdapter:
    vendor = "ashby"

    def parse(self, payload: dict, token: str) -> BoardSnapshot:
        # Ashby's posting API is not paginated: one request returns the whole
        # board. So there is no count to reconcile against, and the envelope
        # itself is the completeness signal — if `jobs` is missing or is not a
        # list, the response shape changed and we must fail closed rather than
        # treat an unparsed board as "empty and therefore all closed".
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise BoardIncomplete(f"ashby:{token} unexpected envelope: {type(payload)}")

        board = AshbyBoard.model_validate(payload)
        raw_by_id = {j["id"]: j for j in payload["jobs"]}
        jobs = [map_job(j, token, raw_by_id.get(j.id, {}))
                for j in board.jobs if j.isListed]
        return BoardSnapshot(ats_vendor=self.vendor, board_token=token,
                             complete=True, jobs=jobs)

    @polite_retry
    async def _get(self, client: httpx.AsyncClient, token: str) -> dict:
        r = await client.get(BOARD_URL.format(token=token))
        r.raise_for_status()
        return r.json()

    async def fetch(self, client: httpx.AsyncClient, token: str) -> BoardSnapshot:
        return self.parse(await self._get(client, token), token)
