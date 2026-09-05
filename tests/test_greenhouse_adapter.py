"""Adapter test against a recorded real Greenhouse response (conventions:
every adapter needs one). Fixtures under fixtures/samples/ are trimmed copies
of real boards, committed so the suite runs offline."""

import json
from pathlib import Path

import pytest

from reqtrace.adapters.greenhouse import GhBoard, map_job

SAMPLES = Path(__file__).resolve().parent.parent / "fixtures" / "samples" / "greenhouse"


def load(token):
    payload = json.loads((SAMPLES / f"{token}.json").read_text())
    board = GhBoard.model_validate(payload)
    raw = {j["id"]: j for j in payload["jobs"]}
    return [map_job(j, token, raw[j.id]) for j in board.jobs]


def test_parses_recorded_board():
    jobs = load("quantium")
    assert jobs, "fixture produced no jobs"
    j = jobs[0]
    assert j.ats_vendor == "greenhouse"
    assert j.board_token == "quantium"
    assert j.external_id and j.external_id.isdigit()
    assert j.title
    assert j.apply_url.startswith("http")


def test_identity_is_the_composite_key_not_title():
    jobs = load("quantium")
    keys = [j.key for j in jobs]
    assert len(keys) == len(set(keys)), "external ids must be unique per board"
    # Distinct requisitions may legitimately share a title; identity must not merge them.
    for j in jobs:
        assert j.key == ("greenhouse", "quantium", j.external_id)


def test_description_is_decoded_and_sanitised():
    """Greenhouse returns entity-encoded HTML (&lt;div&gt;...). If it isn't
    unescaped before sanitising, the markup survives as literal text."""
    j = next(x for x in load("quantium") if x.description_html)
    assert "&lt;" not in j.description_html
    assert "<script" not in j.description_html.lower()
    assert j.description_text
    assert "<" not in j.description_text


def test_content_hash_is_stable_and_distinct():
    a, b = load("quantium"), load("quantium")
    assert [x.content_hash for x in a] == [x.content_hash for x in b]
    assert len({x.content_hash for x in a}) > 1


@pytest.mark.parametrize("token", ["quantium", "cultureamp", "prospa"])
def test_all_recorded_boards_map_cleanly(token):
    jobs = load(token)
    assert jobs
    assert all(j.title and j.external_id for j in jobs)


# --- the completeness guard, at the adapter boundary ----------------------
# test_closure_detection.py builds `complete=False` by hand, so it never checks
# that the *adapter* gets this decision right. These do.

import asyncio  # noqa: E402

import httpx  # noqa: E402

from reqtrace.adapters.greenhouse import GreenhouseAdapter  # noqa: E402


def _fetch_with(payload):
    def handler(request):
        return httpx.Response(200, json=payload)

    async def go():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await GreenhouseAdapter().fetch(client, "acme")

    return asyncio.run(go())


JOB = {"id": 1, "title": "Data Scientist", "content": "<p>x</p>",
       "absolute_url": "https://x/1", "location": {"name": "Sydney"}}


def test_matching_meta_total_marks_board_complete():
    snap = _fetch_with({"jobs": [JOB], "meta": {"total": 1}})
    assert snap.complete is True and len(snap.jobs) == 1


def test_missing_meta_total_fails_closed():
    """If meta.total vanishes we have no evidence the board is whole. Failing
    open here would let a partial fetch retire every job that didn't appear."""
    snap = _fetch_with({"jobs": [JOB]})
    assert snap.complete is False


def test_short_board_fails_closed():
    snap = _fetch_with({"jobs": [JOB], "meta": {"total": 40}})
    assert snap.complete is False
