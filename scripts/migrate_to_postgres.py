#!/usr/bin/env python3
"""Copy the SQLite index into Postgres, timestamps and all.

Not a re-sweep. Sweeping into an empty Postgres would stamp `first_seen_at` on
every row with today's date, drop every `closed_at`, and replace the run log
with one fresh entry per board — restarting precisely the series the README
says is worth more than the listings. The rows have to be carried over.

    DATABASE_URL='postgres://…' python scripts/migrate_to_postgres.py
    DATABASE_URL='…' python scripts/migrate_to_postgres.py --force   # re-copy

Idempotent by refusal rather than by merge: it stops if the target already has
jobs, because a partial second copy is harder to reason about than a clean one.
`--force` truncates first.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from reqtrace.store import DEFAULT_SQLITE, Store  # noqa: E402

# `function` is a keyword in enough dialects that COPY column lists are a
# common place for it to trip; quoting every name costs nothing.
JOB_COLS = ("ats_vendor", "board_token", "external_id", "title",
            "description_html", "description_text", "location_raw",
            "location_city", "location_country", "remote_type", "salary_min",
            "salary_max", "salary_currency", "salary_period", "department",
            "employment_type", "seniority", "function", "apply_url",
            "posted_at", "content_hash", "first_seen_at", "last_seen_at",
            "closed_at")
RUN_COLS = ("ats_vendor", "board_token", "fetched_at", "complete", "n_fetched",
            "n_new", "n_updated", "n_closed", "n_reopened", "error")
COMPANY_COLS = ("ats_vendor", "board_token", "name", "domain", "careers_url")

# id is BIGSERIAL on the Postgres side and absent from SQLite: let the sequence
# assign them rather than synthesising values.
TIMESTAMPS = {"posted_at", "first_seen_at", "last_seen_at", "closed_at",
              "fetched_at"}
BOOLEANS = {"complete"}


def _ts(v):
    """SQLite keeps timestamps as ISO text; Postgres wants a real timestamptz.
    Empty strings are the trap — '' is a fine TEXT value and an invalid
    timestamptz, and seven vendors supply `posted_at`."""
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None


def _text(v):
    # A NUL anywhere in a body aborts the whole COPY. These bodies came from
    # seven vendors' HTML through nh3, so they are not trusted to be clean.
    return v.replace("\x00", "") if isinstance(v, str) else v


def _row(cols, row):
    out = []
    for col, v in zip(cols, row):
        if col in TIMESTAMPS:
            out.append(_ts(v))
        elif col in BOOLEANS:
            out.append(bool(v))
        else:
            out.append(_text(v))
    return out


def copy_table(src, dst, table, cols) -> int:
    quoted = ", ".join(f'"{c}"' for c in cols)
    n = 0
    with dst.cursor() as cur:
        with cur.copy(f"COPY {table} ({quoted}) FROM STDIN") as cp:
            for row in src.execute(f"SELECT {quoted} FROM {table}"):
                cp.write_row(_row(cols, row))
                n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sqlite", type=Path, default=DEFAULT_SQLITE)
    ap.add_argument("--force", action="store_true",
                    help="truncate the target tables first")
    args = ap.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        return print("set DATABASE_URL to the target Postgres", file=sys.stderr) or 2
    if not args.sqlite.exists():
        return print(f"no index at {args.sqlite}", file=sys.stderr) or 2

    import psycopg

    # The schema is applied through Store so the migration and a normal ingest
    # cannot drift apart on DDL.
    store = Store(dsn=dsn)
    store.init_schema()
    dst = store.conn

    have = dst.execute("SELECT count(*) FROM jobs").fetchone()[0]
    if have and not args.force:
        print(f"target already holds {have:,} jobs — pass --force to replace",
              file=sys.stderr)
        return 1
    if args.force:
        dst.execute("TRUNCATE jobs, board_runs, companies")

    src = sqlite3.connect(f"file:{args.sqlite}?mode=ro", uri=True)
    counts = {}
    for table, cols in (("companies", COMPANY_COLS), ("jobs", JOB_COLS),
                        ("board_runs", RUN_COLS)):
        counts[table] = copy_table(src, dst, table, cols)
        print(f"  {table:12} {counts[table]:>7,} rows")
    dst.commit()

    # Verify the thing the migration exists for, not just that rows arrived.
    ok = True
    for table, n in counts.items():
        got = dst.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        if got != n:
            print(f"MISMATCH {table}: copied {n}, target has {got}", file=sys.stderr)
            ok = False

    lo_s, hi_s, closed_s = src.execute(
        "SELECT min(first_seen_at), max(first_seen_at), "
        "count(closed_at) FROM jobs").fetchone()
    lo_p, hi_p, closed_p = dst.execute(
        "SELECT min(first_seen_at), max(first_seen_at), "
        "count(closed_at) FROM jobs").fetchone()
    # Both rendered in UTC. Postgres hands back an aware datetime in the
    # session timezone, which compares equal but *reads* like a mismatch.
    utc = lambda d: d.astimezone(timezone.utc).isoformat()  # noqa: E731
    print(f"\n  first_seen_at  sqlite {utc(_ts(lo_s))} .. {utc(_ts(hi_s))}")
    print(f"                 pgres  {utc(lo_p)} .. {utc(hi_p)}")
    if _ts(lo_s) != lo_p or _ts(hi_s) != hi_p:
        print("MISMATCH: the first_seen_at series did not survive", file=sys.stderr)
        ok = False
    if closed_s != closed_p:
        print(f"MISMATCH closed_at: {closed_s} vs {closed_p}", file=sys.stderr)
        ok = False
    print(f"  closed_at      {closed_p} rows carried over")

    src.close()
    store.close()
    print("\nmigration verified" if ok else "\nMIGRATION FAILED VERIFICATION")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
