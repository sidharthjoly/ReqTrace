"""Canonical job record, and the snapshot an adapter returns for one board."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

RemoteType = Literal["onsite", "hybrid", "remote", "unknown"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Job(BaseModel):
    """One requisition, normalised. Identity is (ats_vendor, board_token, external_id) —
    never title+company, which would merge genuinely distinct requisitions."""

    ats_vendor: str
    board_token: str
    external_id: str

    title: str
    description_html: str = ""
    description_text: str = ""

    location_raw: str = ""
    location_city: str | None = None
    location_country: str | None = None
    remote_type: RemoteType = "unknown"

    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_period: str | None = None

    department: str | None = None
    employment_type: str | None = None
    seniority: str | None = None
    function: str | None = None

    apply_url: str = ""
    posted_at: str | None = None   # the vendor's publish date, not ours
    content_hash: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.ats_vendor, self.board_token, self.external_id)


def token_slug(token: str) -> str:
    """Filesystem-safe form of a board token. Workday and Oracle tokens carry
    the site path (`cba.wd3/CommBank_Careers`), so the raw token cannot be used
    as a filename."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", token).strip("_")


def content_hash(raw: dict | list) -> str:
    """Hash of the raw vendor payload. Gates the expensive enrichment step so
    classification only re-runs when something actually changed."""
    blob = json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


class BoardSnapshot(BaseModel):
    """The result of one board fetch.

    `complete` is load-bearing: closure detection diffs the fetched set against
    the stored open set, so a *partial* fetch is indistinguishable from mass
    closures. Only a snapshot that fetched the whole board may retire jobs.
    """

    ats_vendor: str
    board_token: str
    fetched_at: datetime = Field(default_factory=utcnow)
    complete: bool = False
    jobs: list[Job] = Field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.complete
