"""Oracle Recruiting Cloud adapter.

The best Australian source found after the original Step 0 audit: Westpac
publishes 148 roles here of which 140 are Australian — the highest AU density of
any board in the index — including *Senior Data Scientist - DDAI*, *Senior
Quantitative Analyst, AI Models* and *Data Analytics Manager - Financial Crime
Intelligence*. TPG Telecom runs two sites on the same platform.

It is also the most pleasant of the enterprise feeds to consume: it pages at 50
(Workday manages 20, Eightfold 10), states `PrimaryLocationCountry` as ISO-2
rather than a display string, and gives a real `PostedDate`.

The detail endpoint took some finding. Every documented `finder=` form returns
400; the one that works is the plain path parameter
`recruitingCEJobRequisitionDetails/{id}`, which carries `ExternalDescriptionStr`.

`board_token` is `{host}/{siteNumber}` — a tenant can run several candidate
experience sites (TPG has CX_1 and CX_2 with different roles), so the site is
part of the board's identity.
"""

from __future__ import annotations

import asyncio
import re
from typing import Annotated

import httpx
from pydantic import BaseModel, BeforeValidator, Field

from ..models import BoardSnapshot, Job, content_hash
from ..normalise import (
    html_to_text, maybe_australian, parse_location, sanitise_html, to_iso2,
)
from .base import BoardIncomplete, polite_retry

TOKEN_RE = re.compile(r"^(?P<host>[A-Za-z0-9_.-]+)/(?P<site>[A-Za-z0-9_]+)$")
PAGE = 50
LIST_PATH = ("/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
             "?onlyData=true&expand=requisitionList"
             "&finder=findReqs;siteNumber={site},limit={limit},offset={offset}")
DETAIL_PATH = ("/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
               "/{job_id}?expand=all&onlyData=true")


def _blank_if_none(v):
    return v or ""


NullableStr = Annotated[str, BeforeValidator(_blank_if_none)]

WORKPLACE = {"onsite": "onsite", "on-site": "onsite", "hybrid": "hybrid",
             "remote": "remote", "office": "onsite"}


def parse_token(token: str) -> tuple[str, str]:
    m = TOKEN_RE.match(token or "")
    if not m:
        raise BoardIncomplete(
            f"oracle token must look like 'host/CX_1', got {token!r}")
    return m["host"], m["site"]


class OracleReq(BaseModel):
    Id: str | int
    Title: NullableStr = ""
    PostedDate: str | None = None
    PrimaryLocation: NullableStr = ""
    PrimaryLocationCountry: str | None = None
    Department: NullableStr = ""
    JobFamily: NullableStr = ""
    JobFunction: NullableStr = ""
    JobSchedule: NullableStr = ""
    ContractType: NullableStr = ""
    WorkplaceType: str | None = None
    WorkplaceTypeCode: str | None = None
    ShortDescriptionStr: NullableStr = ""


def map_job(raw: OracleReq, token: str, raw_dict: dict, detail: dict | None = None) -> Job:
    host, site = parse_token(token)
    detail = detail or {}

    city, parsed_country, parsed_remote = parse_location(raw.PrimaryLocation)
    # PrimaryLocationCountry is already ISO-2 and authoritative.
    country = to_iso2(raw.PrimaryLocationCountry) or parsed_country

    wt = (raw.WorkplaceType or raw.WorkplaceTypeCode or "").lower()
    remote = WORKPLACE.get(wt, parsed_remote)

    body = detail.get("ExternalDescriptionStr") or raw.ShortDescriptionStr
    quals = detail.get("ExternalQualificationsStr") or ""
    html = sanitise_html(body + (f"<h3>Qualifications</h3>{quals}" if quals else ""))

    return Job(
        ats_vendor="oracle",
        board_token=token,
        external_id=str(raw.Id),
        title=(detail.get("Title") or raw.Title).strip(),
        description_html=html,
        description_text=html_to_text(html) if html else "",
        location_raw=raw.PrimaryLocation,
        location_city=city,
        location_country=country,
        remote_type=remote,
        department=raw.Department or detail.get("Department") or None,
        function=raw.JobFamily or raw.JobFunction or None,
        employment_type=raw.JobSchedule or raw.ContractType or None,
        apply_url=(f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}"
                   f"/job/{raw.Id}"),
        posted_at=raw.PostedDate,
        content_hash=content_hash(raw_dict),
    )


class OracleAdapter:
    vendor = "oracle"

    def parse(self, payload: dict, token: str,
              details: dict[str, dict] | None = None) -> BoardSnapshot:
        items = (payload or {}).get("items")
        if not isinstance(items, list) or not items:
            raise BoardIncomplete(f"oracle:{token} unexpected envelope")
        head = items[0]
        reqs = head.get("requisitionList")
        if not isinstance(reqs, list):
            raise BoardIncomplete(f"oracle:{token} no requisitionList")

        total = head.get("TotalJobsCount")
        complete = total is not None and len(reqs) == total

        details = details or {}
        parsed = [OracleReq.model_validate(r) for r in reqs]
        raw_by_id = {str(r.get("Id")): r for r in reqs}
        jobs = [map_job(p, token, raw_by_id.get(str(p.Id), {}), details.get(str(p.Id)))
                for p in parsed]
        return BoardSnapshot(ats_vendor=self.vendor, board_token=token,
                             complete=complete, jobs=jobs)

    @polite_retry
    async def _get(self, client: httpx.AsyncClient, url: str) -> dict:
        r = await client.get(url, headers={"Accept": "application/json"})
        r.raise_for_status()
        return r.json()

    async def _all_pages(self, client: httpx.AsyncClient, token: str) -> dict:
        host, site = parse_token(token)
        reqs, offset, total = [], 0, None
        while True:
            page = await self._get(client, f"https://{host}" + LIST_PATH.format(
                site=site, limit=PAGE, offset=offset))
            items = page.get("items") or [{}]
            head = items[0]
            total = head.get("TotalJobsCount", 0)
            got = head.get("requisitionList") or []
            reqs += got
            offset += len(got)
            if not got or offset >= total:
                break
        return {"items": [{"TotalJobsCount": total, "requisitionList": reqs}]}

    async def _descriptions(self, client: httpx.AsyncClient, token: str,
                            reqs: list[dict], concurrency: int = 4) -> dict[str, dict]:
        """Bodies only for roles that might be Australian, as elsewhere. These
        boards are ~95% AU so it saves little here, but it keeps the behaviour
        the same across vendors and bounds a non-AU-heavy tenant."""
        host, _ = parse_token(token)
        wanted = [r for r in reqs
                  if maybe_australian(r.get("PrimaryLocation") or "")
                  or (r.get("PrimaryLocationCountry") or "").upper() == "AU"]
        out: dict[str, dict] = {}
        sem = asyncio.Semaphore(concurrency)

        async def one(r: dict):
            async with sem:
                try:
                    d = await self._get(
                        client, f"https://{host}" + DETAIL_PATH.format(job_id=r["Id"]))
                    out[str(r["Id"])] = d
                except Exception:
                    pass  # a missing body must not fail the board

        await asyncio.gather(*(one(r) for r in wanted))
        return out

    async def fetch(self, client: httpx.AsyncClient, token: str) -> BoardSnapshot:
        payload = await self._all_pages(client, token)
        reqs = payload["items"][0]["requisitionList"]
        details = await self._descriptions(client, token, reqs)
        return self.parse(payload, token, details)
