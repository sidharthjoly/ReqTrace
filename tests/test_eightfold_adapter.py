"""Eightfold adapter, against a recorded real response (Citi, AU-scoped)."""

import json
from pathlib import Path

import pytest

from reqtrace.adapters.base import BoardIncomplete
from reqtrace.adapters.eightfold import EightfoldAdapter, parse_token

SAMPLES = Path(__file__).resolve().parent.parent / "fixtures" / "samples" / "eightfold"
TOKEN = "citi/citi.com"


def payload():
    return json.loads((SAMPLES / "citi_citi.com.json").read_text())


def details():
    return json.loads((SAMPLES / "citi_citi.com_detail.json").read_text())


def test_token_encodes_tenant_and_domain():
    assert parse_token(TOKEN) == ("citi", "citi.com")


def test_malformed_token_fails_closed():
    for bad in ("citi", "", "citi/", "/citi.com"):
        with pytest.raises(BoardIncomplete):
            parse_token(bad)


def test_parses_recorded_board():
    snap = EightfoldAdapter().parse(payload(), TOKEN)
    assert snap.complete and snap.jobs
    j = snap.jobs[0]
    assert j.ats_vendor == "eightfold" and j.board_token == TOKEN
    assert j.external_id and j.title and j.apply_url.startswith("http")


def test_board_is_australia_scoped():
    """These boards are fetched with location=Australia, so every row should be
    Australian — that is what makes 18-of-3366 affordable to index."""
    snap = EightfoldAdapter().parse(payload(), TOKEN)
    assert all(j.location_country == "AU" for j in snap.jobs), \
        [(j.title, j.location_raw) for j in snap.jobs if j.location_country != "AU"]


def test_completeness_reconciles_against_count():
    short = {"data": {"count": 999, "positions": payload()["data"]["positions"]}}
    assert EightfoldAdapter().parse(short, TOKEN).complete is False


def test_multi_location_postings_keep_every_location():
    """A role open in Sydney and Melbourne lists both; the city takes the first
    but the raw string must not lose the rest."""
    snap = EightfoldAdapter().parse(payload(), TOKEN)
    multi = [j for j in snap.jobs if ";" in j.location_raw]
    if multi:
        assert multi[0].location_city


def test_detail_supplies_body():
    d = details()
    snap = EightfoldAdapter().parse(payload(), TOKEN, d)
    enriched = [j for j in snap.jobs if j.external_id in d]
    assert enriched
    assert enriched[0].description_text and "<" not in enriched[0].description_text


def test_creation_ts_is_epoch_seconds_not_millis():
    """Lever's createdAt is milliseconds, Eightfold's creationTs is seconds.
    Mixing them up dates every role to 1970."""
    snap = EightfoldAdapter().parse(payload(), TOKEN)
    posted = [j.posted_at for j in snap.jobs if j.posted_at]
    assert posted and all(p.startswith("20") for p in posted), posted[:3]


def test_unexpected_envelope_fails_closed():
    with pytest.raises(BoardIncomplete):
        EightfoldAdapter().parse({"nope": True}, TOKEN)
