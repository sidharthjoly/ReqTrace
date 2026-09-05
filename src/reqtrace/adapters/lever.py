"""Lever adapter.

Small in board count but not in value: Palantir, Spotify, Deputy, Immutable,
Kogan, Q-CTRL and Zeller all sit here. It stayed unbuilt for a while because
discovery could not find Lever boards — Common Crawl barely indexes
`jobs.lever.co`, so a full sweep returns essentially just robots.txt.

Two things this feed does better than the others: `country` is already ISO-2,
and `workplaceType` states remote/hybrid/onsite outright.

Tokens are CASE-SENSITIVE, unlike every other vendor here:
`api.lever.co/v0/postings/Zeller` resolves and `/zeller` 404s.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

import httpx
from pydantic import BaseModel, BeforeValidator, Field

from ..models import BoardSnapshot, Job, content_hash
from ..normalise import html_to_text, parse_location, sanitise_html, to_iso2
from .base import BoardIncomplete, polite_retry

BOARD_URL = "https://api.lever.co/v0/postings/{token}?mode=json"


def _blank_if_none(v):
    return v or ""


NullableStr = Annotated[str, BeforeValidator(_blank_if_none)]

WORKPLACE = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite",
             "on-site": "onsite"}


class LeverCategories(BaseModel):
    commitment: NullableStr = ""
    location: NullableStr = ""
    team: NullableStr = ""
    department: NullableStr = ""
    allLocations: list[str] = Field(default_factory=list)


class LeverList(BaseModel):
    text: NullableStr = ""
    content: NullableStr = ""


class LeverPosting(BaseModel):
    id: str
    text: NullableStr = ""
    description: NullableStr = ""
    descriptionPlain: NullableStr = ""
    additional: NullableStr = ""
    hostedUrl: NullableStr = ""
    applyUrl: NullableStr = ""
    country: str | None = None
    workplaceType: str | None = None
    createdAt: int | None = None
    categories: LeverCategories = Field(default_factory=LeverCategories)
    lists: list[LeverList] = Field(default_factory=list)


def _body_html(raw: LeverPosting) -> str:
    """Lever splits the ad into a description, a set of titled lists
    (requirements, responsibilities) and a trailing 'additional' block. The
    lists carry the actual requirements, so dropping them would gut the text
    that search runs over."""
    parts = [raw.description]
    for section in raw.lists:
        if section.content.strip():
            parts.append(f"<h3>{section.text}</h3>{section.content}")
    if raw.additional.strip():
        parts.append(raw.additional)
    return "".join(p for p in parts if p.strip())


def _posted_at(created_ms: int | None) -> str | None:
    """createdAt is epoch milliseconds."""
    if not created_ms:
        return None
    try:
        return datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def map_job(raw: LeverPosting, token: str, raw_dict: dict) -> Job:
    loc = raw.categories.location
    city, country, remote = parse_location(loc)
    # `country` is authoritative and already ISO-2; the parsed string is only a
    # fallback for the city.
    country = to_iso2(raw.country) or country
    remote = WORKPLACE.get((raw.workplaceType or "").lower(), remote)

    html = sanitise_html(_body_html(raw))
    return Job(
        ats_vendor="lever",
        board_token=token,
        external_id=raw.id,
        title=raw.text.strip(),
        description_html=html,
        description_text=raw.descriptionPlain.strip() or html_to_text(html),
        location_raw=loc or ", ".join(raw.categories.allLocations),
        location_city=city,
        location_country=country,
        remote_type=remote,
        department=raw.categories.department or raw.categories.team or None,
        function=raw.categories.team or None,
        employment_type=raw.categories.commitment or None,
        apply_url=raw.applyUrl or raw.hostedUrl,
        posted_at=_posted_at(raw.createdAt),
        content_hash=content_hash(raw_dict),
    )


class LeverAdapter:
    vendor = "lever"

    def parse(self, payload, token: str) -> BoardSnapshot:
        # Lever returns a bare JSON array and does not paginate, so a list *is*
        # the whole board. Anything else means the shape changed, and an
        # unparsed board must never look like "empty, therefore all closed".
        if not isinstance(payload, list):
            raise BoardIncomplete(f"lever:{token} expected a list, got {type(payload)}")

        postings = [LeverPosting.model_validate(p) for p in payload]
        raw_by_id = {p["id"]: p for p in payload}
        jobs = [map_job(p, token, raw_by_id.get(p.id, {})) for p in postings]
        return BoardSnapshot(ats_vendor=self.vendor, board_token=token,
                             complete=True, jobs=jobs)

    @polite_retry
    async def _get(self, client: httpx.AsyncClient, token: str):
        r = await client.get(BOARD_URL.format(token=token))
        r.raise_for_status()
        return r.json()

    async def fetch(self, client: httpx.AsyncClient, token: str) -> BoardSnapshot:
        return self.parse(await self._get(client, token), token)
