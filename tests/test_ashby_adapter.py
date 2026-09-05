"""Ashby adapter, against recorded real responses."""

import json
from pathlib import Path

import pytest

from reqtrace.adapters.ashby import AshbyAdapter
from reqtrace.adapters.base import BoardIncomplete

SAMPLES = Path(__file__).resolve().parent.parent / "fixtures" / "samples" / "ashby"


def load(token):
    return AshbyAdapter().parse(json.loads((SAMPLES / f"{token}.json").read_text()), token)


def test_parses_recorded_board():
    snap = load("airtasker")
    assert snap.complete and snap.jobs
    j = snap.jobs[0]
    assert j.ats_vendor == "ashby" and j.board_token == "airtasker"
    assert j.external_id and j.title and j.apply_url.startswith("http")


def test_structured_postal_address_beats_the_display_string():
    j = load("airtasker").jobs[0]
    assert j.location_city == "Sydney"
    assert j.location_country == "AU"


def test_workplace_type_not_is_remote():
    """`isRemote` is True for hybrid roles too — in the recorded boards it is
    True on 260 jobs while only 11 are actually workplaceType 'Remote'.
    Trusting it would wildly over-report remote work."""
    raw = json.loads((SAMPLES / "mitti.json").read_text())
    hybrid = [j for j in raw["jobs"] if j.get("workplaceType") == "Hybrid"]
    assert hybrid, "fixture should contain a hybrid role"
    assert hybrid[0].get("isRemote") is True, "vendor really does report hybrid as remote"
    mapped = {j.external_id: j for j in load("mitti").jobs}
    assert mapped[hybrid[0]["id"]].remote_type == "hybrid"
    assert all(j.remote_type != "remote" for j in load("mitti").jobs)


def test_falls_back_to_the_location_string_when_workplace_type_is_absent():
    """Older postings carry no workplaceType; 'Sydney (Hybrid)' still has to
    resolve rather than defaulting to unknown."""
    # Assert on the job that actually carries the marker, not on the whole
    # fixture — re-trimming from the live board would otherwise break this.
    snap = load("airtasker")
    hybrid = [j for j in snap.jobs if "hybrid" in j.location_raw.lower()]
    assert hybrid, "fixture should contain a '(Hybrid)' location string"
    assert all(j.remote_type == "hybrid" for j in hybrid)


def test_structured_compensation_is_used_verbatim():
    """Ashby publishes machine-readable salary, so no regex is needed."""
    paid = [j for j in load("up").jobs if j.salary_min]
    assert paid, "the 'up' fixture has a job with published compensation"
    j = paid[0]
    assert j.salary_currency == "AUD" and j.salary_period == "year"
    assert j.salary_min < j.salary_max


def test_only_the_salary_component_counts():
    """Bonus/Equity/Commission are real components but are not a salary band;
    folding them in would corrupt the range."""
    for j in load("airwallex").jobs:
        if j.salary_min is not None:
            assert j.salary_min > 1000, "equity percentages must not leak into salary"


def test_unexpected_envelope_fails_closed():
    """No jobs list means the response shape changed. Returning an empty board
    would look like every job closing at once."""
    with pytest.raises(BoardIncomplete):
        AshbyAdapter().parse({"error": "nope"}, "acme")
