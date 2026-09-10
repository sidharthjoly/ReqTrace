"""The crawl frontier, in the database instead of a JSON file.

`scripts/crawl_careers.py` keeps its whole state in `crawl_state.json`: the
seen-set, the pending queue and the findings, rewritten in full at the end of
every run. That is exactly right for a crawl that starts from 64 known
employers and stops — it is a few hundred kilobytes and it fits in memory
twice over.

It does not survive a crawl that never stops. A frontier that keeps growing
turns that file into a multi-megabyte read-modify-write on every checkpoint,
the seen-set has to be held in RAM in its entirety to dedupe against, and a
process killed between checkpoints loses everything since the last one. All
three problems are the same problem: the state is a document when it wants to
be a table.

So the queue moves here, and three things fall out of that:

  * **"Seen" becomes "a row exists."** Deduplication is the URL primary key and
    `ON CONFLICT DO NOTHING`, not a Python set, so it costs no memory and is
    correct across restarts and across two crawlers running at once.
  * **Checkpointing becomes continuous.** A row is marked done as it is
    crawled. Kill the process at any moment and the most that is lost is the
    handful of URLs in flight.
  * **The findings table gets an `adopted_at` column**, which is the hinge
    between discovery and ingestion — see `pending_adoption` at the bottom.

The dialect handling follows `store.py`: the SQL is kept to the subset SQLite
and Postgres share, with `ph` for the one placeholder difference.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from .crawl import canonicalise, host_of
from .models import utcnow

FRONTIER_DDL = """
CREATE TABLE IF NOT EXISTS crawl_frontier (
    url          TEXT PRIMARY KEY,
    host         TEXT NOT NULL,
    score        INTEGER NOT NULL,
    depth        INTEGER NOT NULL,
    seed_domain  TEXT NOT NULL DEFAULT '',
    state        TEXT NOT NULL DEFAULT 'pending',
    enqueued_at  TIMESTAMP NOT NULL,
    visited_at   TIMESTAMP
)
"""

# `state` leads the index because every read is "the best pending row" — a
# scan ordered by score alone would walk the whole done-set first, and the
# done-set is the part that grows without limit.
FRONTIER_INDEXES = [
    "CREATE INDEX IF NOT EXISTS crawl_frontier_next_idx "
    "ON crawl_frontier (state, score DESC)",
    "CREATE INDEX IF NOT EXISTS crawl_frontier_host_idx ON crawl_frontier (host)",
]

HOSTS_DDL = """
CREATE TABLE IF NOT EXISTS crawl_hosts (
    host         TEXT PRIMARY KEY,
    pages        INTEGER NOT NULL DEFAULT 0,
    exhausted    INTEGER NOT NULL DEFAULT 0,
    delay        DOUBLE PRECISION,
    robots_body  TEXT,
    robots_at    TIMESTAMP,
    last_seen_at TIMESTAMP
)
"""

FINDINGS_DDL = """
CREATE TABLE IF NOT EXISTS crawl_findings (
    ats_vendor   TEXT NOT NULL,
    board_token  TEXT NOT NULL,
    seed_domain  TEXT NOT NULL DEFAULT '',
    found_on     TEXT,
    ingestable   INTEGER NOT NULL DEFAULT 0,
    found_at     TIMESTAMP NOT NULL,
    adopted_at   TIMESTAMP,
    PRIMARY KEY (ats_vendor, board_token, seed_domain)
)
"""


@dataclass
class Pending:
    url: str
    score: int
    depth: int
    seed: str


class Frontier:
    """The queue, the seen-set and the findings log, over one connection.

    Takes a `Store` rather than a raw connection so it inherits the backend
    detection and the `ph` placeholder already worked out there, and so the
    crawl state lives in the same database as the jobs it eventually produces.
    """

    def __init__(self, store) -> None:
        self.store = store
        self.conn = store.conn
        self.ph = store.ph
        self.backend = store.backend

    def init_schema(self) -> None:
        cur = self.conn.cursor()
        for ddl in (FRONTIER_DDL, HOSTS_DDL, FINDINGS_DDL):
            cur.execute(ddl)
        for ix in FRONTIER_INDEXES:
            cur.execute(ix)
        self.conn.commit()

    def _now(self):
        now = utcnow()
        return now if self.backend == "postgres" else now.isoformat()

    # -- the queue ---------------------------------------------------------

    def add(self, rows: list[tuple[str, str, int, int, str]]) -> int:
        """Enqueue [(url, host, score, depth, seed)]. Already-known URLs are
        dropped by the primary key — that conflict *is* the seen-set check, so
        a URL crawled six restarts ago is never queued twice.

        **Every URL is canonicalised on the way in, and that is load-bearing
        twice over.** The primary key can only be a seen-set if one page has
        one spelling: `…/careers/` and `…/careers` are two rows and two fetches
        of the same page otherwise. And `Crawler.enqueue` canonicalises
        internally, so a row stored in raw form never matches the URL the
        crawler reports back as visited — it is released instead of finished,
        re-claimed on the next lap, and re-fetched every lap forever. A queue
        whose rows silently never retire is worse than no queue at all.

        Returns how many were genuinely new, which is the number the daemon
        prints as "discovered".
        """
        if not rows:
            return 0
        cur = self.conn.cursor()
        now = self._now()
        sql = (
            f"INSERT INTO crawl_frontier "
            f"(url, host, score, depth, seed_domain, state, enqueued_at) "
            f"VALUES ({self.ph}, {self.ph}, {self.ph}, {self.ph}, {self.ph}, "
            f"'pending', {self.ph}) ON CONFLICT (url) DO NOTHING"
        )
        params = []
        for url, host, score, depth, seed in rows:
            url = canonicalise(url) or ""
            if not url:
                continue   # not fetchable: a mailto:, a binary asset, junk
            params.append((url[:2000], host_of(url) or host, int(score),
                           int(depth), seed, now))
        if not params:
            return 0
        # One batched statement, and the count comes from `rowcount` rather
        # than from counting the table before and after. Both matter once this
        # runs against Neon rather than a local file: the old version issued a
        # round trip per URL (a lap discovering 50 links paid 50 of them) and
        # bracketed them with two `SELECT count(*) WHERE state='pending'`
        # scans, whose cost grows with a table that is designed never to stop
        # growing. `ON CONFLICT DO NOTHING` makes rowcount exactly the number
        # of genuinely new URLs, which is the number we wanted anyway.
        cur.executemany(sql, params)
        self.conn.commit()
        n = cur.rowcount
        return n if n and n > 0 else 0

    def claim(self, limit: int, *, max_per_host: int = 12) -> list[Pending]:
        """The next batch to crawl: highest-scoring pending URLs whose host is
        neither exhausted nor over its page cap.

        Rows are handed out and immediately marked `claimed` so a second
        crawler against the same database takes different work. The cost of a
        crash between claim and visit is that those URLs are skipped rather
        than repeated, which is the right way round to be wrong — re-fetching
        somebody's site is the rudeness this whole module is careful about.
        """
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT f.url, f.score, f.depth, f.seed_domain FROM crawl_frontier f "
            f"LEFT JOIN crawl_hosts h ON h.host = f.host "
            f"WHERE f.state = 'pending' "
            f"AND COALESCE(h.exhausted, 0) = 0 "
            f"AND COALESCE(h.pages, 0) < {self.ph} "
            f"ORDER BY f.score DESC, f.depth ASC LIMIT {self.ph}",
            (max_per_host, limit),
        )
        out = [Pending(r[0], r[1], r[2], r[3] or "") for r in cur.fetchall()]
        if out:
            marks = ", ".join([self.ph] * len(out))
            # `visited_at` doubles as "when this row was last acted on", which
            # is what `requeue_stale_claims` needs. Timing the reclaim off
            # `enqueued_at` instead would strand nothing and reclaim
            # everything: a URL that sat pending for a week is stale by that
            # clock the instant it is claimed.
            cur.execute(
                f"UPDATE crawl_frontier SET state = 'claimed', visited_at = {self.ph} "
                f"WHERE url IN ({marks})",
                [self._now(), *[p.url for p in out]],
            )
            self.conn.commit()
        return out

    def finish(self, urls: list[str], state: str = "done") -> None:
        if not urls:
            return
        cur = self.conn.cursor()
        now = self._now()
        marks = ", ".join([self.ph] * len(urls))
        cur.execute(
            f"UPDATE crawl_frontier SET state = {self.ph}, visited_at = {self.ph} "
            f"WHERE url IN ({marks})",
            [state, now, *urls],
        )
        self.conn.commit()

    def release(self, urls: list[str]) -> None:
        """Put claimed-but-unvisited URLs back. The daemon calls this on a
        clean shutdown so an interrupted lap costs nothing at all."""
        if not urls:
            return
        cur = self.conn.cursor()
        marks = ", ".join([self.ph] * len(urls))
        cur.execute(
            f"UPDATE crawl_frontier SET state = 'pending' "
            f"WHERE state = 'claimed' AND url IN ({marks})",
            urls,
        )
        self.conn.commit()

    def requeue_stale_claims(self, hours: int = 6) -> int:
        """Reclaim rows a killed process left claimed forever.

        `release` handles the clean exit; this handles SIGKILL, a lost laptop
        lid and an OOM. Without it every hard stop permanently strands whatever
        was in flight, and a long-running daemon accumulates that leak until
        the frontier looks empty while thousands of rows sit claimed.
        """
        cur = self.conn.cursor()
        # The cutoff is computed in Python, not in SQL, because the two
        # backends do not store this column in the same type and SQLite's own
        # date functions do not produce the format we wrote. `_now()` writes
        # `utcnow().isoformat()` — "2026-09-10T04:13:28.298573+00:00" — while
        # `datetime('now')` yields "2026-09-10 04:13:28": a different separator
        # and no offset. Comparing those as text compares "T" against " ", so
        # the stored value is *always* the greater one and the reclaim silently
        # matched nothing at all. Deriving the cutoff the same way the value
        # was written keeps both backends comparing like with like.
        cutoff = utcnow() - timedelta(hours=hours)
        if self.backend != "postgres":
            cutoff = cutoff.isoformat()
        cur.execute(
            f"UPDATE crawl_frontier SET state = 'pending' "
            f"WHERE state = 'claimed' AND visited_at < {self.ph}", (cutoff,))
        n = cur.rowcount or 0
        self.conn.commit()
        return n

    def count(self, state: str | None = None) -> int:
        cur = self.conn.cursor()
        if state:
            cur.execute(
                f"SELECT count(*) FROM crawl_frontier WHERE state = {self.ph}",
                (state,))
        else:
            cur.execute("SELECT count(*) FROM crawl_frontier")
        return cur.fetchone()[0]

    # -- hosts -------------------------------------------------------------

    def host_state(self, hosts: list[str]) -> dict[str, tuple[int, bool]]:
        """host -> (pages already crawled, exhausted). Carries the per-host
        budget across restarts, so a daemon cannot spend twelve pages a lap on
        one site forever."""
        if not hosts:
            return {}
        cur = self.conn.cursor()
        marks = ", ".join([self.ph] * len(hosts))
        cur.execute(
            f"SELECT host, pages, exhausted FROM crawl_hosts WHERE host IN ({marks})",
            hosts)
        return {r[0]: (r[1], bool(r[2])) for r in cur.fetchall()}

    def save_hosts(self, hosts: dict[str, tuple[int, bool, float]]) -> None:
        """Persist {host: (pages, exhausted, delay)}. Pages are written as an
        absolute value, not an increment: the crawler was handed the stored
        count when the lap started, so it already knows the running total."""
        if not hosts:
            return
        cur = self.conn.cursor()
        now = self._now()
        sql = (
            f"INSERT INTO crawl_hosts (host, pages, exhausted, delay, last_seen_at) "
            f"VALUES ({self.ph}, {self.ph}, {self.ph}, {self.ph}, {self.ph}) "
            f"ON CONFLICT (host) DO UPDATE SET pages = EXCLUDED.pages, "
            f"exhausted = EXCLUDED.exhausted, delay = EXCLUDED.delay, "
            f"last_seen_at = EXCLUDED.last_seen_at"
        )
        for host, (pages, exhausted, delay) in hosts.items():
            cur.execute(sql, (host, int(pages), 1 if exhausted else 0,
                              float(delay), now))
        self.conn.commit()

    def retire_exhausted(self, hosts: list[str] | None = None) -> int:
        """Drop pending URLs on hosts that are already done.

        A host is exhausted the moment it yields a board token, but the links
        queued from its earlier pages are still sitting in the frontier. They
        can never be claimed — `claim` filters exhausted hosts — so left alone
        they accumulate forever as rows that make `status` report a pending
        queue far larger than the work that actually exists. Retiring them
        keeps the count honest, which matters more than usual for a process
        whose whole job is to run unattended for weeks.

        `hosts` narrows the update to the handful a lap actually touched, which
        is what the daemon passes. Without it this is an unbounded-table
        `UPDATE ... WHERE host IN (SELECT ...)` run every lap forever — fine
        over a few hundred rows, a repeated scan of everything once the
        frontier is large and remote. Omitting it does the full sweep, which is
        what a one-off cleanup wants.
        """
        cur = self.conn.cursor()
        sql = ("UPDATE crawl_frontier SET state = 'skipped' WHERE state = 'pending' "
               "AND host IN (SELECT host FROM crawl_hosts WHERE exhausted = 1")
        if hosts:
            marks = ", ".join([self.ph] * len(hosts))
            cur.execute(f"{sql} AND host IN ({marks}))", hosts)
        else:
            cur.execute(f"{sql})")
        n = cur.rowcount or 0
        self.conn.commit()
        return n

    # -- findings ----------------------------------------------------------

    def record(self, findings) -> int:
        """Log board tokens found. New rows land with `adopted_at` NULL, which
        is what `pending_adoption` looks for."""
        if not findings:
            return 0
        cur = self.conn.cursor()
        now = self._now()
        sql = (
            f"INSERT INTO crawl_findings "
            f"(ats_vendor, board_token, seed_domain, found_on, ingestable, found_at) "
            f"VALUES ({self.ph}, {self.ph}, {self.ph}, {self.ph}, {self.ph}, {self.ph}) "
            f"ON CONFLICT (ats_vendor, board_token, seed_domain) DO NOTHING"
        )
        n = 0
        for f in findings:
            cur.execute(sql, (f.vendor, f.token, f.seed, f.source_url[:2000],
                              1 if f.ingestable else 0, now))
            n += cur.rowcount or 0
        self.conn.commit()
        return n

    def pending_adoption(self, ingestable_only: bool = True,
                         unadopted_only: bool = False) -> list[dict]:
        """Findings to offer to ingestion.

        This is the join that makes the crawler continuous rather than a thing
        that produces a report someone pastes into a CSV by hand.

        `unadopted_only` defaults to **False**, and that is the safety
        property, not an oversight. `adopted_at` cannot be the authority on
        what ingestion knows about, because the two are written to different
        places: the flag lands in Postgres, the board lands in a CSV that has
        to be committed and pushed. Anything between those — a rebase
        conflict, a protected branch, a runner dying — leaves the flag saying
        "handled" and the file not listing the board, and nothing would ever
        offer it again.

        So the caller filters against the CSV it is about to write, which is
        the file ingestion actually reads, and adoption becomes idempotent and
        self-healing: a push that fails simply means the same boards are
        offered again on the next run. `adopted_at` is then what it should
        have been all along — a record of when a board first landed, not a
        gate.
        """
        cur = self.conn.cursor()
        sql = ("SELECT ats_vendor, board_token, seed_domain, found_on "
               "FROM crawl_findings WHERE 1 = 1")
        if ingestable_only:
            sql += " AND ingestable = 1"
        if unadopted_only:
            sql += " AND adopted_at IS NULL"
        cur.execute(sql + " ORDER BY found_at")
        return [{"ats_vendor": r[0], "board_token": r[1],
                 "seed_domain": r[2], "found_on": r[3]} for r in cur.fetchall()]

    def mark_adopted(self, pairs: list[tuple[str, str, str]]) -> None:
        if not pairs:
            return
        cur = self.conn.cursor()
        now = self._now()
        for vendor, token, seed in pairs:
            cur.execute(
                f"UPDATE crawl_findings SET adopted_at = {self.ph} "
                f"WHERE ats_vendor = {self.ph} AND board_token = {self.ph} "
                f"AND seed_domain = {self.ph}",
                (now, vendor, token, seed))
        self.conn.commit()

    def stats(self) -> dict:
        cur = self.conn.cursor()
        out: dict[str, int] = {}
        cur.execute("SELECT state, count(*) FROM crawl_frontier GROUP BY state")
        for state, n in cur.fetchall():
            out[f"frontier_{state}"] = n
        cur.execute("SELECT count(*) FROM crawl_hosts")
        out["hosts"] = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM crawl_hosts WHERE exhausted = 1")
        out["hosts_resolved"] = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM crawl_findings")
        out["findings"] = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM crawl_findings WHERE adopted_at IS NULL "
                    "AND ingestable = 1")
        # Never-adopted findings. `adopt` re-checks against the CSV, so this is
        # a floor on the outstanding work, not the exact figure.
        out["never_adopted"] = cur.fetchone()[0]
        return out
