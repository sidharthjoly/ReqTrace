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

# board_runs is append-only, so "the state of a board" means its most recent
# row. There is no id column on the SQLite side, hence the rowid tiebreak:
# a whole sweep can share a fetched_at down to the second.
LATEST = """
WITH latest AS (
    SELECT *, ROW_NUMBER() OVER (
               PARTITION BY ats_vendor, board_token
               ORDER BY fetched_at DESC, rowid DESC) AS rn
    FROM board_runs
)
"""

# How long after a sweep a board counts as stale. The schedule is daily, so a
# board unseen for two days has missed one — that is a real signal, not jitter.
STALE_HOURS = 48


def _rows(conn, sql, params=()) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def summary(conn) -> dict:
    """One line's worth of "is the pipeline alive"."""
    row = _rows(conn, LATEST + f"""
        SELECT count(*) AS boards,
               max(fetched_at) AS last_run,
               min(fetched_at) AS oldest_run,
               sum(error IS NULL AND complete = 1) AS ok,
               sum(error IS NOT NULL) AS failed,
               sum(error IS NULL AND complete = 0) AS incomplete,
               sum((julianday('now') - julianday(fetched_at)) * 24 > {STALE_HOURS})
                   AS stale,
               sum(n_new) AS new, sum(n_closed) AS closed,
               sum(n_reopened) AS reopened, sum(n_fetched) AS fetched
        FROM latest WHERE rn = 1
    """)[0]
    row["stale_hours"] = STALE_HOURS
    # An index with no runs at all should read as empty, not as zeroes that
    # look like a sweep that found nothing.
    row["ever_run"] = bool(row["boards"])
    return row


def by_vendor(conn) -> list[dict]:
    """Per-adapter rollup of each board's most recent run."""
    return _rows(conn, LATEST + """
        SELECT ats_vendor AS vendor,
               count(*) AS boards,
               sum(error IS NULL AND complete = 1) AS ok,
               sum(error IS NOT NULL) AS failed,
               sum(error IS NULL AND complete = 0) AS incomplete,
               sum(n_fetched) AS fetched, sum(n_new) AS new,
               sum(n_closed) AS closed, sum(n_reopened) AS reopened,
               max(fetched_at) AS last_run, min(fetched_at) AS oldest_run
        FROM latest WHERE rn = 1
        GROUP BY 1 ORDER BY boards DESC
    """)


def problems(conn, limit: int = 60) -> list[dict]:
    """Boards whose latest run errored or came back incomplete.

    Incomplete is worth surfacing next to failed even though it is not an
    error: an incomplete snapshot is forbidden from closing anything, so a
    board stuck there is quietly not doing the one job the index exists for.
    """
    return _rows(conn, LATEST + """
        SELECT ats_vendor AS vendor, board_token AS token, fetched_at,
               complete, n_fetched AS fetched, error
        FROM latest
        WHERE rn = 1 AND (error IS NOT NULL OR complete = 0)
        ORDER BY error IS NULL, fetched_at DESC
        LIMIT ?
    """, (limit,))


def churn(conn, days: int = 21) -> list[dict]:
    """New and closed per day, oldest first — the hiring signal the index is
    named for, at the resolution the run log can support."""
    rows = _rows(conn, """
        SELECT substr(fetched_at, 1, 10) AS day,
               count(*) AS boards, sum(n_new) AS new,
               sum(n_closed) AS closed, sum(n_reopened) AS reopened
        FROM board_runs
        GROUP BY 1 ORDER BY 1 DESC LIMIT ?
    """, (days,))
    return list(reversed(rows))


def recent(conn, limit: int = 100) -> list[dict]:
    """The raw tail of the log, newest first."""
    return _rows(conn, """
        SELECT ats_vendor AS vendor, board_token AS token, fetched_at,
               complete, n_fetched AS fetched, n_new AS new,
               n_updated AS updated, n_closed AS closed,
               n_reopened AS reopened, error
        FROM board_runs
        ORDER BY fetched_at DESC, rowid DESC LIMIT ?
    """, (limit,))


def health(conn) -> dict:
    return {
        "summary": summary(conn),
        "vendors": by_vendor(conn),
        "problems": problems(conn),
        "churn": churn(conn),
        "recent": recent(conn),
    }
