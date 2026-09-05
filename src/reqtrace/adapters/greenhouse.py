"""Greenhouse adapter — v1's first vendor.

Chosen by the Step 0 audit: most audited employers (7), the most AU data roles
(25), and `?content=true` returns the entire board *with descriptions* in a
single request, so a fetch is either complete or it failed. Contrast
SmartRecruiters, which pages at 100 and needs an extra call per job for the
description.
"""

from __future__ import annotations

from typing import Annotated

import httpx
from pydantic import BaseModel, BeforeValidator, Field

from ..models import BoardSnapshot, Job, content_hash
from ..normalise import html_to_text, parse_location, sanitise_html
from .base import polite_retry

BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"


# --- raw vendor shapes -----------------------------------------------------

def _blank_if_none(v: str | None) -> str:
    return v or ""


NullableStr = Annotated[str, BeforeValidator(_blank_if_none)]


class GhLocation(BaseModel):
    name: NullableStr = ""


class GhOffice(BaseModel):
    name: NullableStr = ""
    location: NullableStr = ""


class GhDepartment(BaseModel):
    name: NullableStr = ""


class GhJob(BaseModel):
    id: int
    title: NullableStr = ""
    content: NullableStr = ""
    absolute_url: NullableStr = ""
    updated_at: str | None = None
    first_published: str | None = None
    requisition_id: str | None = None
    company_name: str | None = None
    location: GhLocation = Field(default_factory=GhLocation)
    offices: list[GhOffice] = Field(default_factory=list)
    departments: list[GhDepartment] = Field(default_factory=list)
    metadata: list[dict] | None = None


class GhMeta(BaseModel):
    total: int | None = None


class GhBoard(BaseModel):
    jobs: list[GhJob] = Field(default_factory=list)
    meta: GhMeta = Field(default_factory=GhMeta)


# --- mapping ---------------------------------------------------------------

def map_job(raw: GhJob, token: str, raw_dict: dict) -> Job:
    # `offices[].location` is fuller than `location.name` ("Sydney, New South
    # Wales, Australia" vs "Sydney"), so prefer it when the office agrees.
    office_loc = next((o.location for o in raw.offices if o.location), "")
    location_raw = raw.location.name.strip() or office_loc
    city, country, remote = parse_location(location_raw or office_loc)
    if city is None and office_loc:
        city, country, remote2 = parse_location(office_loc)
        remote = remote if remote != "unknown" else remote2

    html = sanitise_html(raw.content)
    return Job(
        ats_vendor="greenhouse",
        board_token=token,
        external_id=str(raw.id),
        title=raw.title.strip(),
        description_html=html,
        description_text=html_to_text(raw.content),
        location_raw=location_raw,
        location_city=city,
        location_country=country,
        remote_type=remote,
        department=(raw.departments[0].name if raw.departments else None),
        apply_url=raw.absolute_url,
        posted_at=raw.first_published or raw.updated_at,
        content_hash=content_hash(raw_dict),
    )


class GreenhouseAdapter:
    vendor = "greenhouse"

    @polite_retry
    async def _get(self, client: httpx.AsyncClient, token: str) -> dict:
        r = await client.get(BOARD_URL.format(token=token))
        r.raise_for_status()
        return r.json()

    def parse(self, payload: dict, token: str) -> BoardSnapshot:
        board = GhBoard.model_validate(payload)

        # meta.total is the vendor's own count, and it is the ONLY evidence we
        # have that this is the whole board. Fail closed: if it is missing, or
        # disagrees with what we parsed, the board is incomplete. An incomplete
        # board still upserts, but reconcile() will refuse to retire anything —
        # treating a truncated fetch as mass closures is the worst bug this
        # system could have.
        total = board.meta.total
        complete = total is not None and total == len(board.jobs)

        raw_by_id = {j["id"]: j for j in payload.get("jobs", [])}
        jobs = [map_job(j, token, raw_by_id.get(j.id, {})) for j in board.jobs]
        return BoardSnapshot(
            ats_vendor=self.vendor, board_token=token,
            complete=complete, jobs=jobs,
        )

    async def fetch(self, client: httpx.AsyncClient, token: str) -> BoardSnapshot:
        return self.parse(await self._get(client, token), token)
