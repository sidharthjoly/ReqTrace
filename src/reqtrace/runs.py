"""Ingest health: what `board_runs` says about the last sweep.

`store.py` writes a `board_runs` row per board per pass and nothing has ever
read it back. Once ingestion is scheduled rather than typed by hand that log is
the only evidence the index is still being fed — a board that started 404ing
six weeks ago looks identical, from the search page, to a board that genuinely
has no open roles.

Queries take a connection rather than a `Store` so the web handler can keep its
connection-per-request pattern. SQLite-only, like `search.py`.
"""

from __future__ import annotations

import sqlite3

# How long after a sweep a board counts as stale.
#
# This has been re-derived twice, and both times because the *schedule* changed
# shape rather than because the number was wrong.
#
# It began at 48 hours, on the reasoning that a daily sweep means a board
# unseen for two days has missed one. `--budget` broke that: the sweep
# deliberately fetches only the stalest N boards and lets the rest wait, so a
# board waiting its turn is not a fault. Tiering broke it again, and harder —
# boards now carry *different* target intervals (12h for the boards that post
# Australian data roles, a week for the tail), so there is no single correct
# threshold at all.
#
# 96 hours is the cold tier's 72-hour interval plus margin, which makes this
# number mean one specific thing: **a board nothing has fetched in four days,
# which not even the slowest tier can explain.** It will not catch a hot board
# that quietly died yesterday.
#
# That is a real gap and it is survivable, because it is not the primary
# signal. `summary()` also reports `last_run` (the schedule stopping shows up
# there immediately) and `failed`/`incomplete` (a board breaking shows up
# there regardless of tier). Closing the gap properly means recording each
# board's target interval on its `board_runs` row and comparing per-board in
# SQL — worth doing when the runs page next gets attention.
STALE_HOURS = 96

# The four places these two dialects actually differ for these queries. Kept as
# a lookup rather than an ORM because `store.py` already branches inline on
# `self.backend`, and one more abstraction layer to serve five expressions
# would be worse than the branch.
#
#  seq      board_runs has no id column on SQLite; rowid is the insertion order.
#           On Postgres the schema declares BIGSERIAL id, and rowid is an error.
#  done     `complete` is INTEGER on SQLite and BOOLEAN on Postgres, so
#           `complete = 1` is a type error on one of them.
#  count_if SQLite sums a 1/0 predicate; Postgres needs a FILTER clause because
#           a boolean cannot be summed.
#  age_h    julianday() is SQLite-only.
#  day      fetched_at is TEXT on SQLite and TIMESTAMPTZ on Postgres, so
#           slicing the first ten characters only works on one of them.
DIALECTS = {
    "sqlite": {
        "ph": "?",
        "seq": "rowid",
        "done": "complete = 1",
        "not_done": "complete = 0",
        "count_if": lambda c: f"COALESCE(sum(CASE WHEN {c} THEN 1 ELSE 0 END), 0)",
        "age_h": "(julianday('now') - julianday(fetched_at)) * 24",
        "day": "substr(fetched_at, 1, 10)",
    },
    "postgres": {
        "ph": "%s",
        "seq": "id",
        "done": "complete",
        "not_done": "NOT complete",
        "count_if": lambda c: f"count(*) FILTER (WHERE {c})",
        "age_h": "EXTRACT(EPOCH FROM (now() - fetched_at)) / 3600",
        # AT TIME ZONE 'UTC' is load-bearing: to_char would otherwise bucket by
        # the session timezone, while SQLite slices a string that is always UTC.
        # Without it the same run lands on different days in the two backends.
        "day": "to_char(fetched_at AT TIME ZONE 'UTC', 'YYYY-MM-DD')",
    },
}

# Named explicitly rather than SELECT *: the Postgres table carries an id column
# the SQLite one does not, so the star would produce two different row shapes.
RUN_COLS = ("ats_vendor, board_token, fetched_at, complete, n_fetched, "
            "n_new, n_updated, n_closed, n_reopened, error")


def _d(backend: str) -> dict:
    return DIALECTS["postgres" if backend == "postgres" else "sqlite"]


def _latest(backend: str) -> str:
    """board_runs is append-only, so "the state of a board" means its most
    recent row."""
    return f"""
WITH latest AS (
    SELECT {RUN_COLS}, ROW_NUMBER() OVER (
               PARTITION BY ats_vendor, board_token
               ORDER BY fetched_at DESC, {_d(backend)['seq']} DESC) AS rn
    FROM board_runs
)
"""


def _rows(conn, sql, params=(), backend="sqlite") -> list[dict]:
    if backend == "postgres":
        from psycopg.rows import dict_row  # imported lazily, as in store.py

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def summary(conn, backend: str = "sqlite") -> dict:
    """One line's worth of "is the pipeline alive"."""
    d = _d(backend)
    n = d["count_if"]
    row = _rows(conn, _latest(backend) + f"""
        SELECT count(*) AS boards,
               max(fetched_at) AS last_run,
               min(fetched_at) AS oldest_run,
               {n(f"error IS NULL AND {d['done']}")} AS ok,
               {n("error IS NOT NULL")} AS failed,
               {n(f"error IS NULL AND {d['not_done']}")} AS incomplete,
               {n(f"{d['age_h']} > {STALE_HOURS}")} AS stale,
               COALESCE(sum(n_new), 0) AS new,
               COALESCE(sum(n_closed), 0) AS closed,
               COALESCE(sum(n_reopened), 0) AS reopened,
               COALESCE(sum(n_fetched), 0) AS fetched
        FROM latest WHERE rn = 1
    """, backend=backend)[0]
    row["stale_hours"] = STALE_HOURS
    # An index with no runs at all should read as empty, not as zeroes that
    # look like a sweep that found nothing.
    row["ever_run"] = bool(row["boards"])
    return row


def by_vendor(conn, backend: str = "sqlite") -> list[dict]:
    """Per-adapter rollup of each board's most recent run."""
    d = _d(backend)
    n = d["count_if"]
    return _rows(conn, _latest(backend) + f"""
        SELECT ats_vendor AS vendor,
               count(*) AS boards,
               {n(f"error IS NULL AND {d['done']}")} AS ok,
               {n("error IS NOT NULL")} AS failed,
               {n(f"error IS NULL AND {d['not_done']}")} AS incomplete,
               COALESCE(sum(n_fetched), 0) AS fetched,
               COALESCE(sum(n_new), 0) AS new,
               COALESCE(sum(n_closed), 0) AS closed,
               COALESCE(sum(n_reopened), 0) AS reopened,
               max(fetched_at) AS last_run, min(fetched_at) AS oldest_run
        FROM latest WHERE rn = 1
        GROUP BY 1 ORDER BY boards DESC, vendor
    """, backend=backend)


def problems(conn, limit: int = 60, backend: str = "sqlite") -> list[dict]:
    """Boards whose latest run errored or came back incomplete.

    Incomplete is worth surfacing next to failed even though it is not an
    error: an incomplete snapshot is forbidden from closing anything, so a
    board stuck there is quietly not doing the one job the index exists for.
    """
    d = _d(backend)
    return _rows(conn, _latest(backend) + f"""
        SELECT ats_vendor AS vendor, board_token AS token, fetched_at,
               complete, n_fetched AS fetched, error
        FROM latest
        WHERE rn = 1 AND (error IS NOT NULL OR {d['not_done']})
        ORDER BY error IS NULL, fetched_at DESC
        LIMIT {d['ph']}
    """, (limit,), backend=backend)


def churn(conn, days: int = 21, backend: str = "sqlite") -> list[dict]:
    """New and closed per day, oldest first — the hiring signal the index is
    named for, at the resolution the run log can support."""
    d = _d(backend)
    rows = _rows(conn, f"""
        SELECT {d['day']} AS day,
               count(*) AS boards, COALESCE(sum(n_new), 0) AS new,
               COALESCE(sum(n_closed), 0) AS closed,
               COALESCE(sum(n_reopened), 0) AS reopened
        FROM board_runs
        GROUP BY 1 ORDER BY 1 DESC LIMIT {d['ph']}
    """, (days,), backend=backend)
    return list(reversed(rows))


def recent(conn, limit: int = 100, backend: str = "sqlite") -> list[dict]:
    """The raw tail of the log, newest first."""
    d = _d(backend)
    return _rows(conn, f"""
        SELECT ats_vendor AS vendor, board_token AS token, fetched_at,
               complete, n_fetched AS fetched, n_new AS new,
               n_updated AS updated, n_closed AS closed,
               n_reopened AS reopened, error
        FROM board_runs
        ORDER BY fetched_at DESC, {d['seq']} DESC LIMIT {d['ph']}
    """, (limit,), backend=backend)


def health(conn, backend: str = "sqlite") -> dict:
    return {
        "summary": summary(conn, backend),
        "vendors": by_vendor(conn, backend),
        "problems": problems(conn, backend=backend),
        "churn": churn(conn, backend=backend),
        "recent": recent(conn, backend=backend),
    }
