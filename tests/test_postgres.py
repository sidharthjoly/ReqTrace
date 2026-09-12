"""The Postgres path, run against a real server.

Every other test in this suite runs on SQLite, which is exactly how the
Postgres path stayed broken while looking finished: `schema_postgres.sql` was
written, wired and never executed, and `_record_run` was passing an integer
into a BOOLEAN column the whole time. Type errors like that are invisible until
a server rejects them.

Skipped unless REQTRACE_TEST_DSN points at a throwaway database:

    createdb reqtrace_test
    REQTRACE_TEST_DSN=postgresql:///reqtrace_test uv run pytest tests/test_postgres.py

The database is emptied between tests, so do not point this at anything real.
"""

import os

import pytest

from reqtrace import runs, search
from reqtrace.models import BoardSnapshot, Job
from reqtrace.store import Store

DSN = os.environ.get("REQTRACE_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set REQTRACE_TEST_DSN to a throwaway Postgres to run these")


def job(ext_id, title="Data Scientist", h="h1", **kw):
    return Job(ats_vendor="greenhouse", board_token="acme", external_id=ext_id,
               title=title, content_hash=h, apply_url=f"https://x/{ext_id}",
               location_country="AU", **kw)


def snap(jobs, complete=True, error=None, token="acme"):
    return BoardSnapshot(ats_vendor="greenhouse", board_token=token,
                         complete=complete, jobs=jobs, error=error)


# The fixture below TRUNCATEs. That warning used to live only in the module
# docstring, and a docstring does not stop anything: this suite was pointed at
# the production branch and emptied 57,119 jobs, 2,707 board_runs and every
# company row. The `first_seen_at` series it destroyed is the one thing here
# that cannot be recovered by fetching again, and Neon's six-hour restore
# window was missed. So the rule is now enforced rather than documented.
def _refuse_anything_that_looks_real(dsn: str) -> None:
    """Allow only a database that is obviously disposable.

    Deliberately a whitelist: 'does this look like production?' fails open on
    every DSN nobody thought of, and the cost of being wrong is measured in
    history that no re-scrape brings back.
    """
    import urllib.parse

    if dsn == os.environ.get("DATABASE_URL"):
        pytest.exit("REQTRACE_TEST_DSN is the same database as DATABASE_URL. "
                    "These tests TRUNCATE. Point them at a throwaway.")
    p = urllib.parse.urlsplit(dsn)
    name = (p.path or "").lstrip("/").split("?")[0]
    host = (p.hostname or "").lower()
    local = host in ("", "localhost", "127.0.0.1", "::1")
    if not (local or "test" in name.lower()):
        pytest.exit(
            f"REQTRACE_TEST_DSN points at {host or 'a socket'}/{name!r}, which is "
            "neither local nor named like a test database. These tests TRUNCATE "
            "jobs, board_runs and companies. Use a throwaway, or rename it to "
            "include 'test'.")


@pytest.fixture
def store():
    _refuse_anything_that_looks_real(DSN)
    s = Store(dsn=DSN)
    s.init_schema()
    s.conn.execute("TRUNCATE jobs, board_runs, companies")
    s.conn.commit()
    yield s
    s.close()


def test_the_schema_applies(store):
    assert store.backend == "postgres"
    tables = {r[0] for r in store.conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()}
    assert {"jobs", "board_runs", "companies"} <= tables


def test_a_run_is_recorded_at_all(store):
    # The regression this file exists for: `complete` is BOOLEAN here and
    # INTEGER on SQLite, so passing 1/0 raised DatatypeMismatch and every
    # ingest against Postgres died on its first board.
    store.reconcile(snap([job("1")]))
    row = store.conn.execute(
        "SELECT complete, n_fetched FROM board_runs").fetchone()
    assert row == (True, 1)


def test_closure_detection_survives_the_port(store):
    store.reconcile(snap([job("1"), job("2"), job("3")]))
    r = store.reconcile(snap([job("1"), job("2")]))
    assert (r.closed, r.new) == (1, 0)


def test_incomplete_board_never_closes_on_postgres(store):
    store.reconcile(snap([job("1"), job("2")]))
    r = store.reconcile(snap([job("1")], complete=False))
    assert r.closed == 0


def test_relisted_job_reopens_and_keeps_first_seen(store):
    store.reconcile(snap([job("1")]))
    first = store.conn.execute("SELECT first_seen_at FROM jobs").fetchone()[0]
    store.reconcile(snap([]))
    store.reconcile(snap([]))          # empty twice before anything retires
    assert store.conn.execute(
        "SELECT closed_at FROM jobs").fetchone()[0] is not None
    r = store.reconcile(snap([job("1")]))
    assert r.reopened == 1
    assert store.conn.execute("SELECT first_seen_at FROM jobs").fetchone()[0] == first


def test_empty_board_needs_two_runs_before_retiring(store):
    store.reconcile(snap([job("1")]))
    r = store.reconcile(snap([]))
    assert r.closed == 0, "an empty board retired jobs on first sight"
    r = store.reconcile(snap([]))
    assert r.closed == 1


# -- the query layer ---------------------------------------------------------

def test_every_health_query_runs(store):
    store.reconcile(snap([job("1"), job("2")]))
    store.reconcile(snap([job("9")], token="beta", complete=False))
    h = runs.health(store.conn, backend="postgres")
    assert h["summary"]["boards"] == 2
    assert h["summary"]["ok"] == 1 and h["summary"]["incomplete"] == 1
    assert {v["vendor"] for v in h["vendors"]} == {"greenhouse"}
    assert len(h["problems"]) == 1 and h["problems"][0]["token"] == "beta"
    assert h["churn"] and h["recent"]


def test_the_two_backends_agree(store, tmp_path):
    """Same snapshots into both, then diff the health payloads.

    Guards the dialect table: `rowid` vs `id`, boolean vs 1/0, FILTER vs
    sum(CASE), julianday vs EXTRACT. Any of those silently returning something
    different would show up here rather than on the published page."""
    lite = Store(sqlite_path=tmp_path / "cmp.db")
    lite.init_schema()
    for s in (store, lite):
        s.reconcile(snap([job("1"), job("2"), job("3")]))
        s.reconcile(snap([job("1")], token="beta"))
        s.reconcile(snap([job("1"), job("2")]))          # closes one

    pg = runs.health(store.conn, backend="postgres")
    sq = runs.health(lite.conn)
    lite.close()

    # Timestamps and ordering-by-time will differ; the counts must not.
    keys = ("boards", "ok", "failed", "incomplete", "stale", "new", "closed",
            "reopened", "fetched", "ever_run")
    assert {k: pg["summary"][k] for k in keys} == {k: sq["summary"][k] for k in keys}
    strip = lambda rows: [  # noqa: E731
        {k: v for k, v in r.items() if k not in ("last_run", "oldest_run")}
        for r in rows]
    assert strip(pg["vendors"]) == strip(sq["vendors"])
    assert [r["closed"] for r in pg["churn"]] == [r["closed"] for r in sq["churn"]]


def test_a_sweep_survives_the_server_hanging_up(store):
    """The failure that killed the first real Actions run.

    A sweep holds one connection for hours while spending most of its time
    waiting on vendor HTTP APIs, and a serverless Postgres suspends an idle
    compute — Neon's default is five minutes, which one slow board clears.
    The next query then dies with AdminShutdown.

    Simulated here by terminating the store's own backend from a second
    connection, which is what Neon's suspend does to it."""
    import psycopg

    store.reconcile(snap([job("1"), job("2")]))
    pid = store.conn.execute("SELECT pg_backend_pid()").fetchone()[0]

    with psycopg.connect(DSN) as killer:
        killer.execute("SELECT pg_terminate_backend(%s)", (pid,))

    # Same call the sweep makes for its next board. Before the fix this raised
    # psycopg.errors.AdminShutdown and took the whole run with it.
    r = store.reconcile(snap([job("1")]))
    assert r.closed == 1, "reconcile did not complete after the reconnect"
    assert store.conn.execute("SELECT pg_backend_pid()").fetchone()[0] != pid


def test_the_pulse_runs_on_postgres(store):
    """`search.pulse` is the first query layer to reach Postgres at all.

    Everything else in `search.py` is SQLite-only, so `_filters` was written
    with `?` placeholders and nothing noticed — the export calls this one on
    whichever backend holds the index, and `?` is a syntax error on this side.
    """
    from datetime import date

    store.reconcile(snap([
        job("1", posted_at="2026-09-02"),
        job("2", "Data Engineer", posted_at="2026-08-26"),
        job("3", "Chef", posted_at="2026-09-02"),          # outside the scope
    ]))
    p = search.pulse(store.conn, search.Query(data_only=True),
                     backend="postgres", today=date(2026, 9, 7))
    weeks = {w["start"]: w["opened"] for w in p["weeks"]}
    assert len(p["weeks"]) == 16
    assert weeks["2026-08-31"] == 1 and weeks["2026-08-24"] == 1
    assert sum(weeks.values()) == 2, "the Chef leaked into a data-only pulse"
    assert p["closures_since"]


def test_the_two_backends_bucket_the_same_weeks(store, tmp_path):
    """Same roles into both, then diff the series. Guards the placeholder swap
    and the timestamptz-vs-text difference in `_day`."""
    from datetime import date

    lite = Store(sqlite_path=tmp_path / "pulse.db")
    lite.init_schema()
    rows = [job("1", posted_at="2026-09-02"),
            job("2", "Data Engineer", posted_at="2026-08-26"),
            job("3", "Analytics Lead", posted_at="2026-07-14"),
            job("4", "Data Scientist", posted_at="2026-01-01")]   # off-chart
    for s in (store, lite):
        s.reconcile(snap(rows))

    args = dict(qy=search.Query(data_only=True), today=date(2026, 9, 7))
    pg = search.pulse(store.conn, backend="postgres", **args)
    sq = search.pulse(lite.conn, **args)
    lite.close()

    series = lambda p: [(w["start"], w["opened"], w["closed"]) for w in p["weeks"]]
    assert series(pg) == series(sq)
    assert sum(w["opened"] for w in pg["weeks"]) == 3, "the January role was charted"


# -- idle transactions, which only Postgres can show us --------------------
#
# These exist because a bug shipped that the whole SQLite suite was structurally
# blind to. Python's sqlite3 does not begin a transaction for a SELECT, so a
# read that forgets to end its transaction is invisible there and fatal here:
# psycopg leaves the transaction open, Neon's idle_in_transaction_session_timeout
# is five minutes, and a sweep reads its plan and then fetches boards for the
# better part of an hour before writing anything. Both scheduled workflows died
# on it, every run, with an error pointing at an innocent statement.

def idle(store) -> bool:
    """True when the connection holds no open transaction."""
    import psycopg
    return store.conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


def test_reads_do_not_leave_a_transaction_open(store):
    store.reconcile(snap([job("1")]))
    assert idle(store), "reconcile should leave nothing open"

    store.open_jobs("greenhouse", "acme")
    assert idle(store), "open_jobs left a transaction open"

    store.hashes("greenhouse", "acme")
    assert idle(store), "hashes left a transaction open"

    store.last_attempted("greenhouse")
    assert idle(store), "last_attempted left a transaction open -- this is the "\
                        "one the sweep calls before an hour of HTTP"


def test_the_store_survives_being_closed_and_reopened(store):
    """The sweep drops its connection across the fetch phase, so reopening has
    to actually work against Postgres and not merely against SQLite."""
    store.reconcile(snap([job("1")]))
    store.close()
    store.reopen()
    assert store.open_jobs("greenhouse", "acme")
    assert idle(store)


def test_a_dead_connection_is_recognised_whatever_the_error_class(store):
    """`_dropped` used to test the exception class alone, so an
    idle-in-transaction kill -- an InternalError, not an OperationalError --
    escaped the reconnect that exists for exactly this."""
    import psycopg
    assert not store._dropped(ValueError("unrelated"))
    store.conn.close()
    assert store._dropped(psycopg.errors.IdleInTransactionSessionTimeout("gone"))
    store.reopen()
