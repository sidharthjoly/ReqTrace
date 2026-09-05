"""Closure detection — the headline feature.

Every run pulls the full board, so a job that disappears from it has been
filled or pulled. The guard that makes this safe: a board we did not fetch
completely must never retire anything, because a truncated fetch is
indistinguishable from mass closures.
"""

import pytest

from reqtrace.models import BoardSnapshot, Job
from reqtrace.store import Store


def job(ext_id, title="Data Scientist", h="h1"):
    return Job(ats_vendor="greenhouse", board_token="acme", external_id=ext_id,
               title=title, content_hash=h, apply_url=f"https://x/{ext_id}")


def snap(jobs, complete=True, error=None):
    return BoardSnapshot(ats_vendor="greenhouse", board_token="acme",
                         complete=complete, jobs=jobs, error=error)


@pytest.fixture
def store(tmp_path):
    s = Store(sqlite_path=tmp_path / "t.db")
    s.init_schema()
    yield s
    s.close()


def test_first_run_inserts_everything(store):
    r = store.reconcile(snap([job("1"), job("2"), job("3")]))
    assert (r.new, r.closed, r.updated) == (3, 0, 0)


def test_unchanged_board_closes_nothing(store):
    store.reconcile(snap([job("1"), job("2")]))
    r = store.reconcile(snap([job("1"), job("2")]))
    assert (r.new, r.unchanged, r.closed) == (0, 2, 0)


def test_job_missing_from_full_board_is_closed(store):
    store.reconcile(snap([job("1"), job("2", title="Filled Role")]))
    r = store.reconcile(snap([job("1")]))
    assert r.closed == 1
    assert r.closed_titles == ["Filled Role"]
    assert "2" not in store.open_jobs("greenhouse", "acme")


def test_incomplete_board_never_closes_anything(store):
    """The guard. Without it, one truncated fetch retires the whole board."""
    store.reconcile(snap([job("1"), job("2"), job("3")]))
    r = store.reconcile(snap([job("1")], complete=False))
    assert r.closed == 0
    assert r.skipped_close is True
    assert len(store.open_jobs("greenhouse", "acme")) == 3


def test_failed_fetch_closes_nothing(store):
    store.reconcile(snap([job("1"), job("2")]))
    r = store.reconcile(snap([], complete=False, error="HTTPStatusError: 503"))
    assert r.closed == 0 and r.error
    assert len(store.open_jobs("greenhouse", "acme")) == 2


def test_relisted_job_reopens_and_keeps_first_seen(store):
    store.reconcile(snap([job("1"), job("2")]))
    store.reconcile(snap([job("1")]))              # 2 closes
    cur = store.conn.cursor()
    cur.execute("SELECT first_seen_at FROM jobs WHERE external_id='2'")
    first_seen = cur.fetchone()[0]

    r = store.reconcile(snap([job("1"), job("2")]))  # 2 comes back
    assert r.reopened == 1 and r.new == 0
    cur.execute("SELECT first_seen_at, closed_at FROM jobs WHERE external_id='2'")
    seen_again, closed_at = cur.fetchone()
    assert closed_at is None
    assert seen_again == first_seen, "first_seen_at is the hiring-signal series; keep it"


def test_content_change_counts_as_updated(store):
    store.reconcile(snap([job("1", h="old")]))
    r = store.reconcile(snap([job("1", title="Senior Data Scientist", h="new")]))
    assert (r.updated, r.new, r.closed) == (1, 0, 0)
    cur = store.conn.cursor()
    cur.execute("SELECT title FROM jobs WHERE external_id='1'")
    assert cur.fetchone()[0] == "Senior Data Scientist"


def test_boards_are_isolated_from_each_other(store):
    store.reconcile(snap([job("1")]))
    other = BoardSnapshot(ats_vendor="greenhouse", board_token="other", complete=True,
                          jobs=[Job(ats_vendor="greenhouse", board_token="other",
                                    external_id="1", title="Other", content_hash="z")])
    store.reconcile(other)
    r = store.reconcile(snap([job("1")]))
    assert r.closed == 0
    assert len(store.open_jobs("greenhouse", "other")) == 1


def test_non_au_rows_are_stored_without_bodies_but_still_diffed(tmp_path):
    """Descriptions are kept only for AU roles (storage), but every row is still
    inserted — closure detection diffs against the whole open set, so dropping
    non-AU rows entirely would break it."""
    s = Store(sqlite_path=tmp_path / "b.db")
    s.init_schema()
    syd = Job(ats_vendor="greenhouse", board_token="acme", external_id="1",
              title="Data Scientist", location_raw="Sydney", location_city="Sydney",
              location_country="AU", description_html="<p>au</p>",
              description_text="au", content_hash="a")
    ldn = Job(ats_vendor="greenhouse", board_token="acme", external_id="2",
              title="Data Scientist", location_raw="London, United Kingdom",
              location_city="London", location_country="GB",
              description_html="<p>uk</p>", description_text="uk", content_hash="b")
    s.reconcile(BoardSnapshot(ats_vendor="greenhouse", board_token="acme",
                              complete=True, jobs=[syd, ldn]))
    rows = dict(s.conn.execute(
        "SELECT external_id, description_text FROM jobs").fetchall())
    assert rows["1"] == "au", "AU descriptions must be kept"
    assert rows["2"] == "", "non-AU bodies are dropped to fit the free tier"
    assert len(s.open_jobs("greenhouse", "acme")) == 2, "both rows still tracked"

    # and the non-AU row still participates in closure detection
    r = s.reconcile(BoardSnapshot(ats_vendor="greenhouse", board_token="acme",
                                  complete=True, jobs=[syd]))
    assert r.closed == 1
    s.close()


def test_descriptions_all_keeps_every_body(tmp_path):
    s = Store(sqlite_path=tmp_path / "c.db", descriptions="all")
    s.init_schema()
    ldn = Job(ats_vendor="greenhouse", board_token="acme", external_id="2",
              title="X", location_raw="London", location_country="GB",
              description_text="uk", content_hash="b")
    s.reconcile(BoardSnapshot(ats_vendor="greenhouse", board_token="acme",
                              complete=True, jobs=[ldn]))
    assert s.conn.execute(
        "SELECT description_text FROM jobs").fetchone()[0] == "uk"
    s.close()


def test_empty_board_needs_two_runs_before_retiring_everything(store):
    """SmartRecruiters answers 200 with totalFound 0 both for a board whose
    roles were all filled and for a token that no longer exists. Acting on the
    first empty response would stamp closed_at across a whole board's history,
    which the brief values above the listings — so require confirmation."""
    store.reconcile(snap([job("1"), job("2"), job("3")]))

    first = store.reconcile(snap([]))
    assert first.closed == 0 and first.skipped_close
    assert len(store.open_jobs("greenhouse", "acme")) == 3

    second = store.reconcile(snap([]))
    assert second.closed == 3
    assert store.open_jobs("greenhouse", "acme") == {}


def test_a_board_that_recovers_is_never_retired(store):
    store.reconcile(snap([job("1"), job("2")]))
    store.reconcile(snap([]))                     # one blip, nothing closed
    r = store.reconcile(snap([job("1"), job("2")]))
    assert r.closed == 0
    assert len(store.open_jobs("greenhouse", "acme")) == 2


def test_empty_board_with_no_history_is_not_special(store):
    r = store.reconcile(snap([]))
    assert r.closed == 0 and not r.skipped_close


def test_empty_board_guard_is_isolated_per_board(store):
    """The guard reads the previous run from board_runs. With many boards
    reconciled in one pass their timestamps are near-identical, so this checks
    the lookup is really scoped per board and one board's empty run cannot
    satisfy another board's confirmation."""
    def other(jobs, complete=True):
        return BoardSnapshot(ats_vendor="greenhouse", board_token="beta",
                             complete=complete, jobs=jobs)

    def bjob(ext_id):
        return Job(ats_vendor="greenhouse", board_token="beta",
                   external_id=ext_id, title=f"Role {ext_id}", content_hash="x")

    store.reconcile(snap([job("1"), job("2")]))
    store.reconcile(other([bjob("1"), bjob("2")]))

    # both go empty in the same pass — neither may close
    a1 = store.reconcile(snap([]))
    b1 = store.reconcile(other([]))
    assert (a1.closed, b1.closed) == (0, 0)
    assert a1.skipped_close and b1.skipped_close
    assert len(store.open_jobs("greenhouse", "acme")) == 2
    assert len(store.open_jobs("greenhouse", "beta")) == 2

    # acme empties a second time; beta recovers. Only acme retires.
    a2 = store.reconcile(snap([]))
    b2 = store.reconcile(other([bjob("1"), bjob("2")]))
    assert a2.closed == 2, "second consecutive empty run must close"
    assert b2.closed == 0
    assert store.open_jobs("greenhouse", "acme") == {}
    assert len(store.open_jobs("greenhouse", "beta")) == 2


def test_schema_migration_preserves_an_existing_database(tmp_path):
    """Columns added after the first release must be migrated in. Without this
    the only recovery is deleting the file, which destroys the
    first_seen_at/closed_at series the brief calls the most valuable output."""
    import sqlite3

    path = tmp_path / "legacy.db"
    old = sqlite3.connect(path)
    old.execute("""CREATE TABLE jobs (
        ats_vendor TEXT NOT NULL, board_token TEXT NOT NULL,
        external_id TEXT NOT NULL, title TEXT NOT NULL,
        description_html TEXT, description_text TEXT, location_raw TEXT,
        location_city TEXT, location_country TEXT, remote_type TEXT,
        salary_min DOUBLE PRECISION, salary_max DOUBLE PRECISION,
        salary_currency TEXT, salary_period TEXT, department TEXT,
        employment_type TEXT, seniority TEXT, function TEXT, apply_url TEXT,
        content_hash TEXT, first_seen_at TIMESTAMP NOT NULL,
        last_seen_at TIMESTAMP NOT NULL, closed_at TIMESTAMP,
        PRIMARY KEY (ats_vendor, board_token, external_id))""")
    old.execute("INSERT INTO jobs VALUES ('greenhouse','acme','1','Old Role',"
                "'','','','','AU','',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,"
                "'','h','2020-01-01','2020-01-01',NULL)")
    old.commit()
    old.close()

    s = Store(sqlite_path=path)
    s.init_schema()
    cols = {r[1] for r in s.conn.execute("PRAGMA table_info(jobs)")}
    assert "posted_at" in cols

    # the pre-existing history survived, and writes work again
    row = s.conn.execute("SELECT first_seen_at FROM jobs WHERE external_id='1'").fetchone()
    assert row[0] == "2020-01-01", "existing history must not be destroyed"
    r = s.reconcile(snap([job("1"), job("2")]))
    assert r.new == 1 and r.updated == 1
    s.close()


def test_token_slug_is_filesystem_safe():
    """Workday and Oracle board tokens carry a site path, so the raw token
    cannot be a filename — fixture replay broke on exactly this."""
    from reqtrace.models import token_slug

    assert token_slug("cba.wd3/CommBank_Careers") == "cba.wd3_CommBank_Careers"
    assert token_slug("ebuu.fa.ap1.oraclecloud.com/CX_1") == "ebuu.fa.ap1.oraclecloud.com_CX_1"
    # simple tokens are unchanged, so existing fixture names still resolve
    assert token_slug("quantium") == "quantium"
    assert token_slug("Zeller") == "Zeller"
    assert "/" not in token_slug("a/b/c")
