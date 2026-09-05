"""Ingest health — reading the run log back.

`board_runs` is append-only and one row per board per pass, so every question
the page asks ("is this board still being fetched?", "did the last sweep close
anything?") is really "what does this board's *latest* row say?". These pin
that down, plus the two states that matter most: a board whose newest run
failed, and a board that was fine yesterday and has not been seen since.
"""

from datetime import datetime, timedelta, timezone

import pytest

from reqtrace import runs
from reqtrace.models import BoardSnapshot, Job
from reqtrace.store import Store


def job(ext_id, vendor="greenhouse", token="acme"):
    return Job(ats_vendor=vendor, board_token=token, external_id=ext_id,
               title="Data Scientist", content_hash=ext_id,
               apply_url=f"https://x/{ext_id}")


def snap(jobs, vendor="greenhouse", token="acme", complete=True, error=None):
    return BoardSnapshot(ats_vendor=vendor, board_token=token,
                         complete=complete, jobs=jobs, error=error)


@pytest.fixture
def store(tmp_path):
    s = Store(sqlite_path=tmp_path / "t.db")
    s.init_schema()
    yield s
    s.close()


def backdate(store, hours):
    """Age every run row. Staleness is the signal that the *schedule* stopped,
    and there is no other way to reach it in a test."""
    when = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    store.conn.execute("UPDATE board_runs SET fetched_at = ?", (when,))
    store.conn.commit()


def test_no_runs_reads_as_empty_not_as_zeroes(store):
    s = runs.summary(store.conn)
    assert s["ever_run"] is False
    assert s["boards"] == 0


def test_summary_counts_boards_not_runs(store):
    for _ in range(3):
        store.reconcile(snap([job("1")]))
    store.reconcile(snap([job("9")], token="beta"))
    s = runs.summary(store.conn)
    assert s["boards"] == 2 and s["ok"] == 2
    assert s["ever_run"] is True


def test_only_the_latest_run_decides_a_boards_state(store):
    store.reconcile(snap([job("1")], complete=False))
    store.reconcile(snap([job("1")]))                       # recovered
    s = runs.summary(store.conn)
    assert (s["ok"], s["incomplete"], s["failed"]) == (1, 0, 0)
    assert runs.problems(store.conn) == []


def test_a_board_that_has_started_failing_is_a_problem(store):
    store.reconcile(snap([job("1")]))
    store.reconcile(snap([], complete=False, error="HTTPStatusError: 404"))
    s = runs.summary(store.conn)
    assert (s["ok"], s["failed"]) == (0, 1)
    p = runs.problems(store.conn)
    assert len(p) == 1 and p[0]["error"].startswith("HTTPStatusError")


def test_incomplete_is_reported_apart_from_failed(store):
    # An incomplete board is not an error — but it is forbidden from closing
    # anything, so it must not be counted as healthy either.
    store.reconcile(snap([job("1")], complete=False))
    s = runs.summary(store.conn)
    assert (s["ok"], s["incomplete"], s["failed"]) == (0, 1, 0)
    assert runs.problems(store.conn)[0]["error"] is None


def test_a_board_nobody_has_fetched_lately_is_stale(store):
    store.reconcile(snap([job("1")]))
    assert runs.summary(store.conn)["stale"] == 0
    backdate(store, runs.STALE_HOURS + 2)
    s = runs.summary(store.conn)
    # Still "ok" — the last run succeeded. Stale is the separate, worse signal
    # that nothing has run it since.
    assert (s["ok"], s["stale"]) == (1, 1)


def test_vendor_rollup_groups_by_adapter(store):
    store.reconcile(snap([job("1")]))
    store.reconcile(snap([job("2")], token="beta"))
    store.reconcile(snap([job("3", vendor="lever", token="zeller")],
                         vendor="lever", token="zeller"))
    by = {r["vendor"]: r for r in runs.by_vendor(store.conn)}
    assert by["greenhouse"]["boards"] == 2
    assert by["lever"]["boards"] == 1
    # Ordered by board count so the adapter carrying the most coverage leads.
    assert [r["vendor"] for r in runs.by_vendor(store.conn)] == ["greenhouse", "lever"]


def test_churn_is_a_day_per_row_oldest_first(store):
    store.reconcile(snap([job("1"), job("2")]))
    rows = runs.churn(store.conn)
    assert len(rows) == 1 and rows[0]["new"] == 2
    backdate(store, 30)
    store.reconcile(snap([job("1"), job("2"), job("3")]))
    days = [r["day"] for r in runs.churn(store.conn)]
    assert days == sorted(days)


def test_health_answers_every_section_of_the_page(store):
    store.reconcile(snap([job("1")]))
    h = runs.health(store.conn)
    assert set(h) == {"summary", "vendors", "problems", "churn", "recent"}
    assert h["recent"][0]["token"] == "acme"
