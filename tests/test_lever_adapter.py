"""Lever adapter, against a recorded real response (Palantir)."""

import json
from pathlib import Path

import pytest

from reqtrace.adapters.base import BoardIncomplete
from reqtrace.adapters.lever import LeverAdapter

SAMPLES = Path(__file__).resolve().parent.parent / "fixtures" / "samples" / "lever"


def load(token="palantir"):
    return LeverAdapter().parse(json.loads((SAMPLES / f"{token}.json").read_text()), token)


def test_parses_recorded_board():
    snap = load()
    assert snap.complete and snap.jobs
    j = snap.jobs[0]
    assert j.ats_vendor == "lever" and j.board_token == "palantir"
    assert j.external_id and j.title and j.apply_url.startswith("http")


def test_country_comes_from_the_vendor_field_not_the_string():
    """Lever states `country` as ISO-2 outright, which beats parsing
    'Sydney, Australia' out of the display string."""
    snap = load()
    assert all(j.location_country for j in snap.jobs)
    assert {j.location_country for j in snap.jobs} <= {
        "AU", "US", "GB", "DE", "JP", "SG", "CA", "FR", "NL", "AE", "KR", "TW", "PL"}


def test_workplace_type_is_used_directly():
    snap = load()
    assert {j.remote_type for j in snap.jobs} <= {"remote", "hybrid", "onsite", "unknown"}


def test_created_at_epoch_millis_becomes_posted_at():
    snap = load()
    posted = [j.posted_at for j in snap.jobs if j.posted_at]
    assert posted, "createdAt should map to posted_at"
    assert all(p.startswith("20") for p in posted), posted[:3]


def test_lists_are_folded_into_the_body():
    """Lever splits requirements into `lists`; dropping them would gut the text
    that search runs over."""
    raw = json.loads((SAMPLES / "palantir.json").read_text())
    with_lists = next((p for p in raw if p.get("lists")), None)
    assert with_lists, "fixture should have a posting with lists"
    job = next(j for j in load().jobs if j.external_id == with_lists["id"])
    body = job.description_text
    assert len(body) > len(with_lists.get("descriptionPlain", "")) or body


def test_unexpected_envelope_fails_closed():
    with pytest.raises(BoardIncomplete):
        LeverAdapter().parse({"not": "a list"}, "acme")
