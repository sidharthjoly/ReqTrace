"""Workday adapter, against a recorded real response (Cochlear)."""

import json
from pathlib import Path

import pytest

from reqtrace.adapters.base import BoardIncomplete
from reqtrace.adapters.workday import (
    WorkdayAdapter, base_url, external_id, parse_token,
)

SAMPLES = Path(__file__).resolve().parent.parent / "fixtures" / "samples" / "workday"
TOKEN = "cochlear.wd3/Cochlear_Careers"


def payload():
    return json.loads((SAMPLES / "cochlear.wd3_Cochlear_Careers.json").read_text())


def details():
    return json.loads((SAMPLES / "cochlear.wd3_Cochlear_Careers_detail.json").read_text())


def test_token_encodes_tenant_host_and_site():
    """A Workday board needs three unguessable parts, so board_token carries
    all of them."""
    assert parse_token(TOKEN) == ("cochlear", "wd3", "Cochlear_Careers")
    assert base_url(TOKEN) == (
        "https://cochlear.wd3.myworkdayjobs.com/wday/cxs/cochlear/Cochlear_Careers")


def test_malformed_token_fails_closed():
    for bad in ("cochlear", "cochlear/Site", "cochlear.wd3", ""):
        with pytest.raises(BoardIncomplete):
            parse_token(bad)


def test_parses_recorded_board():
    snap = WorkdayAdapter().parse(payload(), TOKEN)
    assert snap.complete and snap.jobs
    j = snap.jobs[0]
    assert j.ats_vendor == "workday" and j.board_token == TOKEN
    assert j.external_id and j.title


def test_completeness_reconciles_against_total():
    short = dict(payload())
    short["total"] = 999
    assert WorkdayAdapter().parse(short, TOKEN).complete is False


def test_external_id_prefers_the_requisition_id():
    """externalPath contains the title slug and changes when a title is edited;
    the requisition id in bulletFields does not, and identity must be stable."""
    from reqtrace.adapters.workday import WdPosting

    p = WdPosting(title="X", externalPath="/job/Sydney/Some-Title_REQ1",
                  bulletFields=["REQ1"])
    assert external_id(p) == "REQ1"
    bare = WdPosting(title="X", externalPath="/job/Sydney/Some-Title_REQ2")
    assert external_id(bare) == "/job/Sydney/Some-Title_REQ2"


def test_detail_supplies_body_country_and_real_date():
    """The listing has no description, and postedOn is relative text
    ("Posted 30+ Days Ago"). Both come from the detail response."""
    d = details()
    snap = WorkdayAdapter().parse(payload(), TOKEN, d)
    enriched = [j for j in snap.jobs if j.external_id in d]
    assert enriched, "the detail fixture should match a posting"
    j = enriched[0]
    assert j.description_text and "<" not in j.description_text
    assert j.location_country == "AU"
    assert j.posted_at and j.posted_at.startswith("20"), j.posted_at


def test_body_is_empty_without_a_detail_fetch():
    snap = WorkdayAdapter().parse(payload(), TOKEN)
    assert all(j.description_html == "" for j in snap.jobs)


def test_unexpected_envelope_fails_closed():
    with pytest.raises(BoardIncomplete):
        WorkdayAdapter().parse({"oops": 1}, TOKEN)


def test_capped_total_is_treated_as_incomplete():
    """Accenture's tenant reports total=2000 while still serving pages at
    offset 2000+. Without this the board looks complete, closure detection
    fires, and everything past the window is falsely retired."""
    p = payload()
    p = dict(p, _truncated=True, total=len(p["jobPostings"]))
    snap = WorkdayAdapter().parse(p, TOKEN)
    assert snap.complete is False, "a truncated board must never retire jobs"


def test_untruncated_board_is_still_complete():
    p = dict(payload(), _truncated=False)
    assert WorkdayAdapter().parse(p, TOKEN).complete is True


def test_board_at_the_result_cap_never_closes():
    """Workday stops counting past 2,000. A board reporting exactly that may be
    truncated, and a truncated board whose window shifts would retire jobs that
    are still open."""
    p = dict(payload())
    p["total"] = WorkdayAdapter.RESULT_CAP
    p["jobPostings"] = p["jobPostings"] * 1  # count doesn't matter; the cap does
    p["_truncated"] = True                   # what _all_pages sets at the cap
    assert WorkdayAdapter().parse(p, TOKEN).complete is False
