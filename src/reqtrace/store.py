"""Storage and closure detection.

Closure detection is the headline feature, so the rule that protects it lives
here rather than in the caller: **only a complete board snapshot may retire
jobs.** Every run pulls the full board, and anything in the stored open set that
the board no longer lists is closed. A partial fetch looks exactly like mass
closures, so an incomplete snapshot is allowed to upsert but never to close.

Postgres is the target (set DATABASE_URL). SQLite is the zero-config local
default so the diff can be exercised without provisioning anything; the SQL is
kept to the subset both dialects share.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .models import BoardSnapshot, Job, utcnow
from .normalise import is_australian

DEFAULT_SQLITE = Path(__file__).resolve().parent.parent.parent / "data" / "jobs.db"

JOBS_DDL = """
CREATE TABLE IF NOT EXISTS jobs (
    ats_vendor        TEXT NOT NULL,
    board_token       TEXT NOT NULL,
    external_id       TEXT NOT NULL,
    title             TEXT NOT NULL,
    description_html  TEXT,
    description_text  TEXT,
    location_raw      TEXT,
    location_city     TEXT,
    location_country  TEXT,
    remote_type       TEXT,
    salary_min        DOUBLE PRECISION,
    salary_max        DOUBLE PRECISION,
    salary_currency   TEXT,
    salary_period     TEXT,
    department        TEXT,
    employment_type   TEXT,
    seniority         TEXT,
    function          TEXT,
    apply_url         TEXT,
    posted_at         TEXT,
    content_hash      TEXT,
    first_seen_at     TIMESTAMP NOT NULL,
    last_seen_at      TIMESTAMP NOT NULL,
    closed_at         TIMESTAMP,
    PRIMARY KEY (ats_vendor, board_token, external_id)
)
"""

RUNS_DDL = """
CREATE TABLE IF NOT EXISTS board_runs (
    ats_vendor   TEXT NOT NULL,
    board_token  TEXT NOT NULL,
    fetched_at   TIMESTAMP NOT NULL,
    complete     INTEGER NOT NULL,
    n_fetched    INTEGER NOT NULL,
    n_new        INTEGER NOT NULL,
    n_updated    INTEGER NOT NULL,
    n_closed     INTEGER NOT NULL,
    n_reopened   INTEGER NOT NULL,
    error        TEXT
)
"""

COMPANIES_DDL = """
CREATE TABLE IF NOT EXISTS companies (
    ats_vendor   TEXT NOT NULL,
    board_token  TEXT NOT NULL,
    name         TEXT NOT NULL,
    domain       TEXT,
    careers_url  TEXT,
    PRIMARY KEY (ats_vendor, board_token)
)
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS jobs_open_idx ON jobs (ats_vendor, board_token, closed_at)",
    "CREATE INDEX IF NOT EXISTS jobs_city_idx ON jobs (location_city, closed_at)",
]

UPSERT_COLUMNS = [
    "title", "description_html", "description_text", "location_raw",
    "location_city", "location_country", "remote_type", "salary_min",
    "salary_max", "salary_currency", "salary_period", "department",
    "employment_type", "seniority", "function", "apply_url", "posted_at",
    "content_hash",
]


@dataclass
class ReconcileResult:
    vendor: str
    token: str
    fetched: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    closed: int = 0
    reopened: int = 0
    skipped_close: bool = False
    note: str = ""
    error: str | None = None
    closed_titles: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.error:
            return f"{self.vendor}:{self.token} FAILED — {self.error}"
        tail = ""
        if self.skipped_close:
            tail = f"  (closures skipped: {self.note or 'incomplete board'})"
        return (f"{self.vendor}:{self.token} fetched={self.fetched} new={self.new} "
                f"updated={self.updated} unchanged={self.unchanged} "
                f"closed={self.closed} reopened={self.reopened}{tail}")


class Store:
    """Thin wrapper over whichever DB is configured. `ph` is the paramstyle
    placeholder, the only dialect difference the queries here actually hit."""

    # Descriptions dominate storage: in the first full Greenhouse sweep they
    # were 351MB, of which 12MB (3.5%) were Australian roles. Keeping bodies
    # only for AU jobs takes the index from ~368MB to ~30MB, which is the
    # difference between fitting a Neon/Supabase free tier and not. Non-AU rows
    # are still inserted in full otherwise — closure detection diffs against the
    # whole open set, so dropping the rows themselves would break it.
    def __init__(self, dsn: str | None = None, sqlite_path: Path | None = None,
                 descriptions: str = "au-only"):
        self.descriptions = descriptions
        self.dsn = dsn or os.environ.get("DATABASE_URL")
        if self.dsn and sqlite_path is None:
            import psycopg  # imported lazily: SQLite path needs no driver

            self.conn = psycopg.connect(self.dsn)
            self.ph = "%s"
            self.backend = "postgres"
        else:
            path = sqlite_path or DEFAULT_SQLITE
            self.sqlite_path = Path(path)
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(path, timeout=30)
            # A scheduled sweep holds write transactions for minutes at a time,
            # and under the default rollback journal that locks readers out
            # entirely — the runs page would 500 for the length of every sweep.
            # WAL lets readers carry on against the last committed state.
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=30000")
            self.ph = "?"
            self.backend = f"sqlite:{Path(path).name}"

    # Columns added after the first release. `CREATE TABLE IF NOT EXISTS` is a
    # no-op against an existing database, so without this an upgrade fails on
    # the next write with "no such column" — and the only recovery would be
    # deleting the file, which destroys the first_seen_at/closed_at series the
    # whole project exists to accumulate.
    MIGRATIONS = {"jobs": {"posted_at": "TEXT"}}

    def _migrate(self) -> list[str]:
        applied = []
        cur = self.conn.cursor()
        for table, columns in self.MIGRATIONS.items():
            if self.backend == "postgres":
                for col, ddl in columns.items():
                    cur.execute(
                        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {ddl}")
                    applied.append(f"{table}.{col}")
                continue
            cur.execute(f"PRAGMA table_info({table})")
            have = {r[1] for r in cur.fetchall()}
            if not have:
                continue  # table was just created with the full schema
            for col, ddl in columns.items():
                if col not in have:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                    applied.append(f"{table}.{col}")
        self.conn.commit()
        return applied

    # -- lifecycle ---------------------------------------------------------
    def init_schema(self) -> None:
        cur = self.conn.cursor()
        if self.backend == "postgres":
            # Postgres gets the real schema: TIMESTAMPTZ, tsvector + pg_trgm.
            cur.execute((Path(__file__).parent / "schema_postgres.sql").read_text())
        else:
            cur.execute(JOBS_DDL)
            cur.execute(RUNS_DDL)
            cur.execute(COMPANIES_DDL)
        # Migrate before the indexes, which reference columns that may be new.
        self._migrate()
        if self.backend != "postgres":
            for ix in INDEXES:
                cur.execute(ix)
        self.conn.commit()

    def load_companies(self, rows) -> int:
        """(vendor, token, name, domain, careers_url) -> companies. The jobs
        table keys on board_token, which is not a human-readable name."""
        cur = self.conn.cursor()
        n = 0
        for vendor, token, name, domain, url in rows:
            cur.execute(
                f"INSERT INTO companies (ats_vendor, board_token, name, domain, careers_url) "
                f"VALUES ({', '.join([self.ph] * 5)}) "
                f"ON CONFLICT (ats_vendor, board_token) DO UPDATE SET "
                f"name=EXCLUDED.name, domain=EXCLUDED.domain, careers_url=EXCLUDED.careers_url",
                (vendor, token, name, domain, url),
            )
            n += 1
        self.conn.commit()
        return n

    def close(self) -> None:
        self.conn.close()

    def reopen(self) -> None:
        """Reconnect after a deliberate `close()`.

        Exists for the always-on crawler. A serverless Postgres only suspends
        its compute once nothing is connected, so a process that holds one
        connection for days keeps the compute — and the bill, or the free
        tier's compute-hour budget — running the entire time, however little
        work it is actually doing. Dropping the connection across a long idle
        stretch and picking it up again is the difference between a crawler
        that costs what it uses and one that costs wall-clock time.
        """
        if self.backend == "postgres":
            import psycopg

            self.conn = psycopg.connect(self.dsn)
        else:
            self.conn = sqlite3.connect(self.sqlite_path, timeout=30)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=30000")

    def _now(self):
        now = utcnow()
        return now if self.backend == "postgres" else now.isoformat()

    def _end_read(self) -> None:
        """End the transaction a SELECT opened.

        psycopg does not autocommit, so *reading* starts a transaction that
        stays open until something ends it. That is invisible right up until a
        read is followed by a long wait: Neon sets
        `idle_in_transaction_session_timeout` to five minutes and kills the
        connection, and the next query — often many minutes of HTTP later —
        dies with IdleInTransactionSessionTimeout on a statement that had
        nothing wrong with it.

        A sweep reads `last_attempted` to plan, then spends the better part of
        an hour fetching boards before it writes anything, so this is not an
        edge case for this codebase; it is the normal path.
        """
        if self.backend == "postgres":
            self.conn.rollback()   # read-only: nothing to keep, just end it

    # -- reads -------------------------------------------------------------
    def open_jobs(self, vendor: str, token: str) -> dict[str, str]:
        """external_id -> title for every job currently open on this board."""
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT external_id, title FROM jobs WHERE ats_vendor={self.ph} "
            f"AND board_token={self.ph} AND closed_at IS NULL",
            (vendor, token),
        )
        out = {r[0]: r[1] for r in cur.fetchall()}
        self._end_read()
        return out

    def last_attempted(self, vendor: str) -> dict[str, str]:
        """board_token -> when a sweep last *tried* this board, success or not.

        Feeds the oldest-first sweep order, and "tried" rather than "succeeded"
        is the whole point. Ranking on successful fetches looks obviously
        right and starves the sweep: a board that never completes has no
        successful run, so it sorts ahead of every board that does, gets picked
        first every single night, spends its minutes, fails again, and sorts
        first again tomorrow. Accenture's Workday tenant is 2,000 jobs and did
        not finish in twelve minutes of measurement — a handful like it would
        permanently occupy the front of the budget while the boards that
        actually succeed rotate ever more slowly.

        Ranking on attempts fixes both directions at once: a board that just
        cost us minutes goes to the back whether or not it worked, and a board
        nobody has ever tried is still missing from this dict, so `stalest`
        still puts a newly adopted board first.
        """
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT board_token, max(fetched_at) FROM board_runs "
            f"WHERE ats_vendor={self.ph} GROUP BY board_token",
            (vendor,),
        )
        out = {r[0]: r[1] for r in cur.fetchall()}
        self._end_read()
        return out

    def hashes(self, vendor: str, token: str) -> dict[str, str]:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT external_id, content_hash FROM jobs WHERE ats_vendor={self.ph} "
            f"AND board_token={self.ph}",
            (vendor, token),
        )
        out = {r[0]: r[1] for r in cur.fetchall()}
        self._end_read()
        return out

    # -- writes ------------------------------------------------------------
    def _upsert(self, job: Job, now) -> None:
        cols = ["ats_vendor", "board_token", "external_id"] + UPSERT_COLUMNS + [
            "first_seen_at", "last_seen_at", "closed_at"]
        drop_body = (
            self.descriptions == "au-only"
            and not is_australian(job.location_city, job.location_country, job.location_raw)
        )
        vals = [job.ats_vendor, job.board_token, job.external_id] + [
            "" if (drop_body and c.startswith("description_")) else getattr(job, c)
            for c in UPSERT_COLUMNS] + [now, now, None]
        marks = ", ".join([self.ph] * len(cols))
        # Re-listing a job that had been closed reopens it: closed_at back to NULL,
        # while first_seen_at is preserved so the hiring-signal history stays intact.
        updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in UPSERT_COLUMNS)
        sql = (
            f"INSERT INTO jobs ({', '.join(cols)}) VALUES ({marks}) "
            f"ON CONFLICT (ats_vendor, board_token, external_id) DO UPDATE SET "
            f"{updates}, last_seen_at=EXCLUDED.last_seen_at, closed_at=NULL"
        )
        self.conn.cursor().execute(sql, vals)

    def _dropped(self, exc: BaseException) -> bool:
        """Did the server hang up, as opposed to rejecting the query?"""
        if self.backend != "postgres":
            return False
        import psycopg

        if isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError)):
            return True
        # A server-side termination does not always arrive as one of those.
        # An idle-in-transaction timeout is an InternalError, and reading the
        # class alone let it escape the retry that exists for exactly this.
        # The connection's own state is the reliable test.
        return bool(getattr(self.conn, "closed", 0)
                    or getattr(self.conn, "broken", False))

    def _reconnect(self) -> None:
        import psycopg

        try:
            self.conn.close()
        except Exception:  # noqa: BLE001 - it is already gone
            pass
        self.conn = psycopg.connect(self.dsn)

    def reconcile(self, snap: BoardSnapshot) -> ReconcileResult:
        """Reconcile one board, surviving a server that hung up on us.

        A sweep holds one connection for hours while most of its time is spent
        waiting on seven vendors' HTTP APIs, and a serverless Postgres suspends
        an idle compute — Neon's default is five minutes, which one slow board
        clears easily. The connection then dies with AdminShutdown on the next
        query, hours into a run.

        Retrying the whole board is safe: it is upserts plus a diff against the
        stored open set, so replaying it lands on the same state. Doing it here
        rather than per-statement means a half-applied board is re-applied
        whole, never left torn."""
        try:
            return self._reconcile(snap)
        except Exception as exc:  # noqa: BLE001
            if not self._dropped(exc):
                raise
            self._reconnect()
            return self._reconcile(snap)

    def _reconcile(self, snap: BoardSnapshot) -> ReconcileResult:
        """Upsert everything on the board, then close whatever fell off it."""
        res = ReconcileResult(snap.ats_vendor, snap.board_token,
                              fetched=len(snap.jobs), error=snap.error)
        if snap.error:
            self._record_run(snap, res)
            return res

        now = self._now()
        before_open = self.open_jobs(snap.ats_vendor, snap.board_token)
        before_hash = self.hashes(snap.ats_vendor, snap.board_token)

        for job in snap.jobs:
            known = job.external_id in before_hash
            if not known:
                res.new += 1
            elif before_hash[job.external_id] != job.content_hash:
                res.updated += 1
            else:
                res.unchanged += 1
            if known and job.external_id not in before_open:
                res.reopened += 1
            self._upsert(job, now)

        # --- closure detection ------------------------------------------
        # Only a complete board may retire jobs. Without this guard a truncated
        # or partially-failed fetch would mark every missing job closed.
        # NB: `before_open` was captured BEFORE the upsert loop above, which
        # clears closed_at on every job it touches. Recomputing the open set
        # here instead would find nothing closed. Do not reorder these blocks.
        # A board that comes back complete-but-empty is ambiguous, and the
        # ambiguity is worst on SmartRecruiters, which answers 200 with
        # totalFound 0 both for a board whose roles were all filled and for a
        # token that no longer exists. Acting immediately would stamp closed_at
        # across a whole board's history — and the brief values that history
        # above the listings. So an empty board retires nothing the first time;
        # it must come back empty twice in a row. A genuinely emptied board is
        # therefore closed one run later, which is cheap.
        if snap.complete and not snap.jobs and before_open and not \
                self._last_run_was_empty(snap.ats_vendor, snap.board_token):
            res.skipped_close = True
            res.note = "board came back empty; awaiting a second empty run before closing"
            self._record_run(snap, res)
            self.conn.commit()
            return res

        if snap.complete:
            fetched_ids = {j.external_id for j in snap.jobs}
            gone = [eid for eid in before_open if eid not in fetched_ids]
            if gone:
                marks = ", ".join([self.ph] * len(gone))
                self.conn.cursor().execute(
                    f"UPDATE jobs SET closed_at={self.ph} WHERE ats_vendor={self.ph} "
                    f"AND board_token={self.ph} AND closed_at IS NULL "
                    f"AND external_id IN ({marks})",
                    [now, snap.ats_vendor, snap.board_token, *gone],
                )
            res.closed = len(gone)
            res.closed_titles = [before_open[e] for e in gone][:10]
        else:
            res.skipped_close = True

        self._record_run(snap, res)
        self.conn.commit()
        return res

    def _last_run_was_empty(self, vendor: str, token: str) -> bool:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT n_fetched, complete FROM board_runs WHERE ats_vendor={self.ph} "
            f"AND board_token={self.ph} AND error IS NULL "
            f"ORDER BY fetched_at DESC, rowid DESC" if self.backend != "postgres"
            else f"SELECT n_fetched, complete FROM board_runs WHERE ats_vendor={self.ph} "
                 f"AND board_token={self.ph} AND error IS NULL "
                 f"ORDER BY fetched_at DESC, id DESC",
            (vendor, token),
        )
        row = cur.fetchone()
        return bool(row) and row[0] == 0 and bool(row[1])

    def _record_run(self, snap: BoardSnapshot, res: ReconcileResult) -> None:
        self.conn.cursor().execute(
            f"INSERT INTO board_runs (ats_vendor, board_token, fetched_at, complete, "
            f"n_fetched, n_new, n_updated, n_closed, n_reopened, error) VALUES "
            f"({', '.join([self.ph] * 10)})",
            # A Python bool, not 1/0: Postgres declares this column BOOLEAN and
            # rejects a smallint, while SQLite stores the bool as 1/0 anyway.
            (snap.ats_vendor, snap.board_token, self._now(), snap.complete,
             res.fetched, res.new, res.updated, res.closed, res.reopened, res.error),
        )
        self.conn.commit()
