"""SmartRecruiters adapter, against recorded real responses."""

import json
from pathlib import Path

import pytest

from reqtrace.adapters.base import BoardIncomplete
from reqtrace.adapters.smartrecruiters import SmartRecruitersAdapter

SAMPLES = (Path(__file__).resolve().parent.parent / "fixtures" / "samples"
           / "smartrecruiters")


def raw(token):
    return json.loads((SAMPLES / f"{token}.json").read_text())


def test_parses_recorded_board():
    d = raw("carsales")
    d = {"totalFound": len(d["content"]), "content": d["content"]}
    snap = SmartRecruitersAdapter().parse(d, "carsales")
    assert snap.complete and snap.jobs
    j = snap.jobs[0]
    assert j.ats_vendor == "smartrecruiters" and j.external_id and j.title


def test_completeness_is_reconciled_against_total_found():
    """An unknown SmartRecruiters company answers 200 with totalFound 0 rather
    than 404, so completeness must never be judged on the status code."""
    d = raw("carsales")
    short = {"totalFound": 999, "content": d["content"]}
    assert SmartRecruitersAdapter().parse(short, "carsales").complete is False


def test_remote_and_hybrid_are_distinguished():
    d = raw("carsales")
    d = {"totalFound": len(d["content"]), "content": d["content"]}
    kinds = {j.remote_type for j in SmartRecruitersAdapter().parse(d, "carsales").jobs}
    assert kinds <= {"remote", "hybrid", "onsite", "unknown"}
    for p in d["content"]:
        loc = p.get("location") or {}
        if loc.get("hybrid"):
            mapped = {j.external_id: j for j in
                      SmartRecruitersAdapter().parse(d, "carsales").jobs}
            assert mapped[p["id"]].remote_type == "hybrid"
            break


def test_body_is_empty_without_a_detail_fetch():
    """The listing carries no description; bodies come from a second request
    that is deliberately made only for AU roles."""
    d = raw("seek")
    d = {"totalFound": len(d["content"]), "content": d["content"]}
    assert all(j.description_html == "" for j in
               SmartRecruitersAdapter().parse(d, "seek").jobs)


def test_detail_sections_become_the_body():
    d = raw("seek")
    d = {"totalFound": len(d["content"]), "content": d["content"]}
    jid = d["content"][0]["id"]
    detail = {"jobAd": {"sections": {
        "companyDescription": {"title": "Company", "text": "<p>boilerplate</p>"},
        "jobDescription": {"title": "Job Description", "text": "<p>build things</p>"},
        "qualifications": {"title": "Qualifications", "text": "<ul><li>sql</li></ul>"},
    }}}
    snap = SmartRecruitersAdapter().parse(d, "seek", {jid: detail})
    j = next(x for x in snap.jobs if x.external_id == jid)
    assert "build things" in j.description_text and "sql" in j.description_text
    # companyDescription is identical on every posting; keeping it would bloat
    # the index and pollute search.
    assert "boilerplate" not in j.description_text


def test_unexpected_envelope_fails_closed():
    with pytest.raises(BoardIncomplete):
        SmartRecruitersAdapter().parse({"oops": True}, "acme")
