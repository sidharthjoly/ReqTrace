"""The sweep budget — which boards a pass actually fetches.

Discovery is meant to push the board count well past what fits in one nightly
run. At roughly 50 seconds a board the workflow's 330-minute timeout tops out
near 390 boards, and timing out mid-sweep is the specific failure the whole
store is built to avoid: a partial pass leaves the boards it never reached
looking exactly like a board whose jobs all closed at once.

`stalest` is the answer — rotate boards instead of truncating the list — and
these tests pin the two properties that make the rotation safe rather than
merely smaller: never-fetched boards jump the queue, and a board left out of a
pass is untouched rather than closed.
"""

from datetime import datetime, timedelta, timezone

import pytest

from reqtrace.models import BoardSnapshot, Job
from reqtrace.run import board_tier, stalest, sweep
from reqtrace.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(sqlite_path=tmp_path / "t.db")
    s.init_schema()
    yield s
    s.close()


def record(store, token, *, hours_ago, vendor="greenhouse", complete=True,
           error=None):
    """Put one board_runs row in the past. `stalest` reads that table, so this
    is the only way to express "this board was fetched a while ago"."""
    store.reconcile(BoardSnapshot(
        ats_vendor=vendor, board_token=token, complete=complete,
        jobs=[Job(ats_vendor=vendor, board_token=token, external_id="1",
                  title="Data Scientist", content_hash="h")],
        error=error))
    when = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    store.conn.execute(
        "UPDATE board_runs SET fetched_at = ? WHERE board_token = ?", (when, token))
    store.conn.commit()


def test_the_stalest_boards_go_first(store):
    record(store, "fresh", hours_ago=1)
    record(store, "old", hours_ago=200)
    plan = stalest({"greenhouse": ["fresh", "old"]}, store, budget=1)
    assert plan == {"greenhouse": ["old"]}


def test_a_never_fetched_board_outranks_every_fetched_one(store):
    """A board discovery adopted today should be in the index tomorrow, not
    whenever the rotation happens to reach it."""
    record(store, "ancient", hours_ago=10_000)
    plan = stalest({"greenhouse": ["ancient", "brand-new"]}, store, budget=1)
    assert plan == {"greenhouse": ["brand-new"]}


def test_the_budget_is_global_not_per_vendor(store):
    """One nightly run has one time budget; splitting it per vendor would let
    seven adapters each spend it."""
    record(store, "a", hours_ago=100)
    record(store, "b", hours_ago=200, vendor="ashby")
    record(store, "c", hours_ago=300, vendor="lever")
    plan = stalest({"greenhouse": ["a"], "ashby": ["b"], "lever": ["c"]},
                   store, budget=2)
    assert sum(len(v) for v in plan.values()) == 2
    assert "greenhouse" not in plan          # the freshest of the three


def test_a_spare_budget_does_not_refetch_boards_that_are_not_due(store):
    """The property that makes running several times a day affordable.

    A leftover budget is not a reason to fetch something again. If it were,
    every extra run of the day would re-sweep the whole hot tier and then start
    on boards that were fetched an hour ago, and the Actions bill would scale
    with how often the workflow runs rather than with how much genuinely needs
    refreshing.
    """
    record(store, "just-fetched", hours_ago=1)
    plan = stalest({"greenhouse": ["just-fetched", "never-fetched"]},
                   store, budget=99,
                   targets={("greenhouse", "just-fetched"): 12.0})
    assert plan == {"greenhouse": ["never-fetched"]}


def test_a_board_becomes_due_once_its_own_interval_has_passed(store):
    """Targets are passed explicitly rather than left to DEFAULT_INTERVALS —
    the defaults are a tuning decision that has already moved twice, and a test
    that silently encodes today's value fails the next time it is tuned."""
    record(store, "past-due", hours_ago=13)
    record(store, "not-yet", hours_ago=11)
    targets = {("greenhouse", "past-due"): 12.0, ("greenhouse", "not-yet"): 12.0}
    plan = stalest({"greenhouse": ["past-due", "not-yet"]}, store, budget=9,
                   targets=targets)
    assert plan == {"greenhouse": ["past-due"]}


def test_a_board_that_never_succeeds_does_not_camp_at_the_front(store):
    """The starvation loop, which an earlier version of this file asserted the
    wrong way round.

    Ranking on *successful* fetches looks obviously right and starves the
    sweep: a board that never completes has no successful run, so it sorts
    ahead of every board that does, is picked first every night, spends its
    minutes, fails, and sorts first again tomorrow. Accenture's 2,000-job
    Workday tenant did not finish in twelve minutes of measurement — a few like
    it would permanently occupy the front of the budget.

    Ranking on attempts means a board that just cost us minutes goes to the
    back whether or not it worked.
    """
    record(store, "always-fails", hours_ago=1, complete=False, error="timeout")
    record(store, "works", hours_ago=48)
    plan = stalest({"greenhouse": ["always-fails", "works"]}, store, budget=1)
    assert plan == {"greenhouse": ["works"]}


def test_a_failing_board_is_still_retried_when_its_turn_comes(store):
    """Deprioritised, not abandoned — a board 404ing today may be back
    tomorrow, and dropping it would need a separate decision from this one."""
    record(store, "always-fails", hours_ago=200, complete=False, error="timeout")
    record(store, "works", hours_ago=1)
    plan = stalest({"greenhouse": ["always-fails", "works"]}, store, budget=1)
    assert plan == {"greenhouse": ["always-fails"]}


def test_boards_left_out_of_a_pass_keep_their_jobs_open(store):
    """The property that makes rotating safe where truncating is not.

    `reconcile` is only ever called for a board that was fetched, so a board
    the budget skipped is not diffed against an empty snapshot — it simply goes
    stale. Nothing is closed by not looking.
    """
    record(store, "skipped", hours_ago=500)
    record(store, "swept", hours_ago=1)
    plan = stalest({"greenhouse": ["skipped", "swept"]}, store, budget=1)
    assert plan == {"greenhouse": ["skipped"]}

    # Sweep only what the budget chose; "swept" is never reconciled this pass.
    for token in plan["greenhouse"]:
        store.reconcile(BoardSnapshot(ats_vendor="greenhouse", board_token=token,
                                      complete=True, jobs=[]))
    assert store.open_jobs("greenhouse", "swept") != {}


# -- the wall-clock guard --------------------------------------------------

async def _sweep(tokens, store, deadline):
    """`sweep` against a client that is never used: an expired deadline must
    short-circuit before any request is made."""
    import httpx
    async with httpx.AsyncClient() as client:
        return await sweep("greenhouse", tokens, store, client, deadline)


def test_an_expired_deadline_skips_boards_without_fetching(store):
    """`--budget` counts boards; the runner enforces minutes. Boards are not
    interchangeable units of time — one 2,000-job Workday tenant costs what
    dozens of small Greenhouse boards cost — so the clock needs its own guard.
    """
    import asyncio
    import time
    n, failed, skipped = asyncio.run(
        _sweep(["a", "b", "c"], store, deadline=time.monotonic() - 1))
    assert (n, failed, skipped) == (0, 0, 3)


def test_boards_skipped_for_time_are_not_reconciled(store):
    """Same property that makes `stalest` safe: a board that was not fetched
    is not diffed, so its open jobs stay open rather than being closed by an
    empty snapshot."""
    import asyncio
    import time
    record(store, "a", hours_ago=1)
    assert store.open_jobs("greenhouse", "a") != {}
    asyncio.run(_sweep(["a"], store, deadline=time.monotonic() - 1))
    assert store.open_jobs("greenhouse", "a") != {}


def test_a_board_that_overruns_is_cut_off_without_closing_its_jobs(store, monkeypatch):
    """`--deadline` is checked before a board starts and never again, so four
    boards can begin a second before it and run past it unbounded. The per-board
    timeout is the actual bound — and a board cut off mid-fetch must come back
    `complete=False`, or `reconcile` would read the partial result as every job
    on the board closing at once."""
    import asyncio
    from reqtrace import run as R

    record(store, "slow", hours_ago=1)
    assert store.open_jobs("greenhouse", "slow") != {}

    async def never_finishes(*_a, **_kw):
        await asyncio.sleep(3600)

    monkeypatch.setattr(R, "BOARD_TIMEOUT", 0.05)
    monkeypatch.setattr(R, "run_board", never_finishes)
    n, failed, skipped = asyncio.run(_sweep(["slow"], store, deadline=None))

    assert (n, failed, skipped) == (1, 1, 0)
    assert store.open_jobs("greenhouse", "slow") != {}   # nothing retired


# -- tiering ---------------------------------------------------------------

def test_tier_is_relevance_first_then_volume():
    """`hot` is defined by having posted an Australian *data* role, not by
    size: a board with three relevant roles is worth more to this index than
    one with three hundred irrelevant ones. `warm` then catches the large
    Australian employers where a new data role appearing would matter."""
    assert board_tier({"n_au_data": "4", "n_au": "9"}) == "hot"
    assert board_tier({"n_au_data": "0", "n_au": "200"}) == "warm"
    assert board_tier({"n_au_data": "0", "n_au": "3"}) == "cold"
    assert board_tier({}) == "cold"
    assert board_tier({"n_au_data": "", "n_au": "oops"}) == "cold"


def test_a_cold_board_yields_to_a_hot_one_that_is_less_old(store):
    """Raw age would rank the cold board first; overdue-ness does not, and that
    inversion is the whole point of tiering."""
    record(store, "cold-old", hours_ago=100)        # 100/168 = 0.6 overdue
    record(store, "hot-newer", hours_ago=24)        # 24/12  = 2.0 overdue
    plan = stalest({"greenhouse": ["cold-old", "hot-newer"]}, store, budget=1,
                   targets={("greenhouse", "cold-old"): 168.0,
                            ("greenhouse", "hot-newer"): 12.0})
    assert plan == {"greenhouse": ["hot-newer"]}


def test_a_cold_board_still_comes_round_when_it_is_genuinely_overdue(store):
    """Tiering delays the tail; it must not abandon it."""
    record(store, "cold-ancient", hours_ago=400)    # 400/168 = 2.4 overdue
    record(store, "hot-recent", hours_ago=18)       # 18/12   = 1.5 overdue
    plan = stalest({"greenhouse": ["cold-ancient", "hot-recent"]}, store, budget=1,
                   targets={("greenhouse", "cold-ancient"): 168.0,
                            ("greenhouse", "hot-recent"): 12.0})
    assert plan == {"greenhouse": ["cold-ancient"]}
