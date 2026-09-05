"""Oracle Recruiting Cloud adapter, against a recorded real response (Westpac)."""

import json
from pathlib import Path

import pytest

from reqtrace.adapters.base import BoardIncomplete
from reqtrace.adapters.oracle import OracleAdapter, parse_token

SAMPLES = Path(__file__).resolve().parent.parent / "fixtures" / "samples" / "oracle"
TOKEN = "ebuu.fa.ap1.oraclecloud.com/CX_1"


def payload():
    return json.loads((SAMPLES / "ebuu.fa.ap1.oraclecloud.com_CX_1.json").read_text())


def details():
    return json.loads((SAMPLES / "ebuu.fa.ap1.oraclecloud.com_CX_1_detail.json").read_text())


def test_token_encodes_host_and_site():
    """A tenant can run several candidate-experience sites with different roles
    (TPG has CX_1 and CX_2), so the site is part of the board identity."""
    assert parse_token(TOKEN) == ("ebuu.fa.ap1.oraclecloud.com", "CX_1")


def test_malformed_token_fails_closed():
    for bad in ("ebuu.fa.ap1.oraclecloud.com", "", "/CX_1", "host/CX-1/extra"):
        with pytest.raises(BoardIncomplete):
            parse_token(bad)


def test_parses_recorded_board():
    snap = OracleAdapter().parse(payload(), TOKEN)
    assert snap.complete and snap.jobs
    j = snap.jobs[0]
    assert j.ats_vendor == "oracle" and j.external_id and j.title
    assert j.apply_url.startswith("https://")


def test_country_comes_from_the_iso_field():
    """PrimaryLocationCountry is already ISO-2, which beats parsing
    'Horsham, VIC, Australia' out of the display string."""
    snap = OracleAdapter().parse(payload(), TOKEN)
    assert all(j.location_country for j in snap.jobs)
    assert "AU" in {j.location_country for j in snap.jobs}


def test_posted_date_is_carried_through():
    snap = OracleAdapter().parse(payload(), TOKEN)
    posted = [j.posted_at for j in snap.jobs if j.posted_at]
    assert posted and all(p.startswith("20") for p in posted)


def test_completeness_reconciles_against_total():
    short = {"items": [{"TotalJobsCount": 999,
                        "requisitionList": payload()["items"][0]["requisitionList"]}]}
    assert OracleAdapter().parse(short, TOKEN).complete is False


def test_detail_supplies_the_body():
    d = details()
    snap = OracleAdapter().parse(payload(), TOKEN, d)
    enriched = [j for j in snap.jobs if j.external_id in d]
    assert enriched
    assert enriched[0].description_text and "<" not in enriched[0].description_text


def test_missing_requisition_list_fails_closed():
    with pytest.raises(BoardIncomplete):
        OracleAdapter().parse({"items": [{"TotalJobsCount": 5}]}, TOKEN)
    with pytest.raises(BoardIncomplete):
        OracleAdapter().parse({"items": []}, TOKEN)
