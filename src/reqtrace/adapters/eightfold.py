"""Eightfold adapter.

Reaches Citi, AstraZeneca, PayPal, Qualcomm and NVIDIA. Modest in volume —
about 31 net-new Australian roles — but Citi and AstraZeneca both hire data
people locally.

The endpoint is sanctioned rather than discovered: Eightfold's robots.txt
explicitly allows `/api/apply` and `/api/pcsx`. Note the trap that the *list*
form of the apply API (`/api/apply/v2/jobs`) returns 403 "Not authorized for
PCSX"; only `/api/pcsx/search` lists, while `/api/apply/v2/jobs/{id}` is the
per-job detail.

**These boards are scoped to Australia**, unlike every other adapter here, and
that is a deliberate trade. `num` caps at 10, so Citi's 3,366 postings would be
337 requests to surface 18 Australian roles. Eightfold exposes a documented,
stable `location` query parameter (Workday's equivalent is a tenant-specific
facet GUID, which is why *that* adapter pages everything and filters locally).
So `complete` here means "the whole Australian view of this board", and a
closure means "no longer an Australian role at this employer" — which is the
question this index exists to answer. The store's two-strikes guard on an
empty board covers the case where the filter itself misbehaves.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Annotated

import httpx
from pydantic import BaseModel, BeforeValidator, Field

from ..models import BoardSnapshot, Job, content_hash
from ..normalise import html_to_text, parse_location, sanitise_html, to_iso2
from .base import BoardIncomplete, polite_retry

TOKEN_RE = re.compile(r"^(?P<tenant>[A-Za-z0-9_-]+)/(?P<domain>[A-Za-z0-9_.-]+)$")
LIST_URL = ("https://{tenant}.eightfold.ai/api/pcsx/search"
            "?domain={domain}&location={location}&start={start}&num={num}")
DETAIL_URL = ("https://{tenant}.eightfold.ai/api/apply/v2/jobs/{job_id}"
              "?domain={domain}")
PAGE = 10          # hard vendor cap
LOCATION = "Australia"


def _blank_if_none(v):
    return v or ""


NullableStr = Annotated[str, BeforeValidator(_blank_if_none)]

WORK_OPTION = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite",
               "office": "onsite"}


def parse_token(token: str) -> tuple[str, str]:
    m = TOKEN_RE.match(token or "")
    if not m:
        raise BoardIncomplete(
            f"eightfold token must look like 'tenant/domain', got {token!r}")
    return m["tenant"], m["domain"]


class EfPosition(BaseModel):
    id: int | str
    name: NullableStr = ""
    department: NullableStr = ""
    displayJobId: NullableStr = ""
    positionUrl: NullableStr = ""
    workLocationOption: str | None = None
    locations: list[str] = Field(default_factory=list)
    creationTs: int | None = None
    postedTs: int | None = None


class EfData(BaseModel):
    count: int = 0
    positions: list[EfPosition] = Field(default_factory=list)


def _posted_at(ts: int | None) -> str | None:
    """creationTs is epoch seconds (contrast Lever's milliseconds)."""
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def map_job(raw: EfPosition, token: str, raw_dict: dict, detail: dict | None = None) -> Job:
    tenant, domain = parse_token(token)
    detail = detail or {}

    # locations is a list; a posting open in Sydney and Melbourne lists both.
    # The first is used for the city, and the raw string keeps all of them.
    loc_list = raw.locations or ([detail.get("location")] if detail.get("location") else [])
    loc_raw = "; ".join(x for x in loc_list if x)
    city, country, remote = parse_location(loc_list[0] if loc_list else "")
    if loc_list and country is None:
        # "Sydney, New South Wales, Australia" — the country is the last part.
        country = to_iso2(loc_list[0].split(",")[-1].strip()) or country
    remote = WORK_OPTION.get((raw.workLocationOption or "").lower(), remote)

    html = sanitise_html(detail.get("job_description", ""))
    return Job(
        ats_vendor="eightfold",
        board_token=token,
        external_id=str(raw.id),
        title=(detail.get("name") or raw.name).strip(),
        description_html=html,
        description_text=html_to_text(html) if html else "",
        location_raw=loc_raw,
        location_city=city,
        location_country=country,
        remote_type=remote,
        department=raw.department or detail.get("department") or None,
        apply_url=(detail.get("canonicalPositionUrl")
                   or f"https://{tenant}.eightfold.ai/careers/job/{raw.id}"),
        posted_at=_posted_at(raw.creationTs or raw.postedTs or detail.get("t_create")),
        content_hash=content_hash(raw_dict),
    )


class EightfoldAdapter:
    vendor = "eightfold"

    def parse(self, payload: dict, token: str,
              details: dict[str, dict] | None = None) -> BoardSnapshot:
        data = (payload or {}).get("data")
        if not isinstance(data, dict) or not isinstance(data.get("positions"), list):
            raise BoardIncomplete(f"eightfold:{token} unexpected envelope")

        parsed = EfData.model_validate(data)
        complete = len(parsed.positions) == parsed.count
        raw_by_id = {str(p.get("id")): p for p in data["positions"]}
        details = details or {}
        jobs = [map_job(p, token, raw_by_id.get(str(p.id), {}), details.get(str(p.id)))
                for p in parsed.positions]
        return BoardSnapshot(ats_vendor=self.vendor, board_token=token,
                             complete=complete, jobs=jobs)

    @polite_retry
    async def _get(self, client: httpx.AsyncClient, url: str) -> dict:
        r = await client.get(url, headers={"Accept": "application/json"})
        r.raise_for_status()
        return r.json()

    async def _all_pages(self, client: httpx.AsyncClient, token: str) -> dict:
        tenant, domain = parse_token(token)
        positions, start, count = [], 0, None
        while True:
            page = await self._get(client, LIST_URL.format(
                tenant=tenant, domain=domain, location=LOCATION, start=start, num=PAGE))
            data = page.get("data") or {}
            count = data.get("count", 0)
            got = data.get("positions") or []
            positions += got
            start += len(got)
            if not got or start >= count:
                break
        return {"data": {"count": count, "positions": positions}}

    async def _descriptions(self, client: httpx.AsyncClient, token: str,
                            positions: list[dict], concurrency: int = 3) -> dict[str, dict]:
        """The board is already Australia-scoped, so every posting is wanted —
        no `maybe_australian` gate is needed here."""
        tenant, domain = parse_token(token)
        out: dict[str, dict] = {}
        sem = asyncio.Semaphore(concurrency)

        async def one(p: dict):
            async with sem:
                try:
                    d = await self._get(client, DETAIL_URL.format(
                        tenant=tenant, domain=domain, job_id=p["id"]))
                    out[str(p["id"])] = d.get("data", d)
                except Exception:
                    pass  # a missing body must not fail the board

        await asyncio.gather(*(one(p) for p in positions))
        return out

    async def fetch(self, client: httpx.AsyncClient, token: str) -> BoardSnapshot:
        payload = await self._all_pages(client, token)
        details = await self._descriptions(client, token, payload["data"]["positions"])
        return self.parse(payload, token, details)
