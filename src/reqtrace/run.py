"""Run one ingestion pass: fetch every configured board, reconcile, report.

Usage:
    uv run python -m reqtrace.run                # all greenhouse boards in the audit CSV
    uv run python -m reqtrace.run --vendor all   # every adapter, one vendor at a time
    uv run python -m reqtrace.run --token quantium
    uv run python -m reqtrace.run --from-fixtures   # offline replay, no network

`--vendor all` is what the scheduled sweep runs; see `scripts/install_autorun.py`.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import search as S
from .adapters import ADAPTERS
from .adapters.base import run_board
from .models import BoardSnapshot, token_slug, utcnow
from .store import Store

ROOT = Path(__file__).resolve().parent.parent.parent
AUDIT = ROOT / "data" / "step0_ats_audit.csv"
DISCOVERED = ROOT / "data" / "discovered_boards.csv"
GLOBAL = ROOT / "data" / "global_ats_audit.csv"
RAW = ROOT / "fixtures" / "raw"
SAMPLES = ROOT / "fixtures" / "samples"

UA = "reqtrace/0.1 (+personal job-search index; contact via repo)"
CONCURRENCY = 4

# Longest one board may run before it is abandoned.
#
# `--deadline` alone does not bound the sweep, because it is checked before a
# board starts and never again: with CONCURRENCY=4, four boards can begin one
# second before the deadline and run for as long as they like past it. And they
# can — httpx's 45s timeout is per *request*, while a large Workday tenant is
# 100+ paged requests plus a detail fetch per maybe-Australian role. Accenture
# was still going after twelve minutes of measurement.
#
# 25 minutes, set against the largest board actually adopted rather than a
# round number. `pwc.wd3/Global_Experienced_Careers` is 4,666 jobs, which at the
# slowest per-job rate measured across tenants (0.241s) is 18.7 minutes — 7%
# under a 20-minute cap, and PwC is precisely the kind of employer this index
# exists to cover. 25 gives that a third of headroom instead.
#
# The ceiling on this number is the workflow: four boards can start moments
# before `--deadline 140` and each run the full timeout, so 140 + 25 = 165
# against a 210-minute job limit, leaving 45 minutes for the export and the
# publish. Raising the deadline and this together will run out of room.
BOARD_TIMEOUT = 25 * 60

# How many finished boards may go unwritten before the sweep stops to write.
#
# The sweep used to fetch a whole vendor and reconcile afterwards, which made
# a killed run worth precisely nothing. `_record_run` fires inside
# `reconcile`, so a run cut off mid-fetch recorded no attempt against any of
# the boards it had already fetched, and `stalest` — which ranks on attempts —
# selected exactly the same boards next time.
#
# That is not a slow sweep, it is a stalled one, and it showed: with ~890
# boards adopted at once by the crawler and none ever attempted, three
# consecutive scheduled runs each picked up the same 400 never-attempted
# Workday tenants, spent 3.5 hours, were cancelled at the job timeout, and
# advanced nothing. The never-attempted count stayed pinned at the budget
# while the crawler kept adding more behind it.
#
# Writing in chunks bounds the loss to a chunk instead of a run.
#
# A chunk is fetched concurrently, then reconciled with nothing in flight,
# rather than reconciling each board the moment it lands. Reconcile is
# synchronous and `_upsert` costs a round trip per job, so writing a large
# Workday tenant blocks the event loop for seconds and a whole chunk for
# minutes; doing that while other boards are mid-fetch would push them past
# httpx's 45s read timeout and fail them for no reason.
#
# The cost of chunking is the tail: a chunk is only as quick as its slowest
# board, so one tenant that runs the full BOARD_TIMEOUT holds up the writing
# of the other 19. That is a throughput loss, not a correctness one, and it
# is bounded at 20 boards where the old whole-vendor pass was bounded at 400.
#
# On the never-attempted Workday backlog that bound will bind rather than
# stay theoretical: 2,000- and 4,000-job tenants are common there, not
# outliers, so two or three chunks stalling on a 25-minute board would spend
# most of a 140-minute deadline waiting on four-board wavefronts. If the
# drain rate turns out to be the problem, this is the number to revisit --
# but measure it across several runs first, because the boards are swept in
# `stalest` order and the early chunks are not the cheap ones.
FLUSH_EVERY = 20


# Which vendors resolve tokens case-insensitively is one fact with several
# readers (this, `discover_boards.py report`, `crawl_forever.py adopt`), and it
# went stale here the moment Workday arrived — see `crawl.CASE_INSENSITIVE`.
from .crawl import CASE_INSENSITIVE  # noqa: E402


def configured_boards(vendor: str) -> list[str]:
    """Boards to ingest: the hand-curated Step 0 audit, the global-employer
    audit, plus anything discovery has since validated. Curated entries win on
    ordering so a hand-checked board is always fetched first."""
    tokens: list[str] = []
    for path, col in ((AUDIT, "board_token"), (GLOBAL, "board_token"),
                      (DISCOVERED, "board_token")):
        if not path.exists():
            continue
        for r in csv.DictReader(path.open()):
            if r.get("ats_vendor") == vendor and r.get(col):
                tokens.append(r[col])
    seen, out = set(), []
    for t in tokens:
        key = t.lower() if vendor in CASE_INSENSITIVE else t
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out


#: How often each tier wants to be fetched, in hours.
#:
#: These were set when the repo was private and GitHub Actions minutes were the
#: binding constraint. The repo is public now, minutes are free, and the
#: constraint moved rather than disappeared: **these are other people's
#: servers.** Unlimited runner time is not a licence to hammer Greenhouse and
#: Workday, so the numbers below are still chosen deliberately, just against
#: politeness instead of a bill.
#:
#: The arithmetic that matters: a tier of N boards on an H-hour interval is
#: `N * 24/H` board-fetches a day, and one board-fetch is anywhere from a
#: single request to ~230 for a large paged Workday tenant. At 190/54/640
#: boards these intervals come to ~1,030 board-fetches a day — on the order of
#: 15-20k HTTP requests spread over seven vendors and twenty-four hours, which
#: is a handful per minute per vendor. That is a well-behaved client. Ten times
#: it would not be, and no amount of free runner time would make it so.
DEFAULT_INTERVALS = {"hot": 6.0, "warm": 24.0, "cold": 72.0}


def _age_hours(when, now) -> float:
    """Hours since `when`, whatever shape the backend handed it back in.

    Postgres returns a datetime and SQLite an ISO string, and the sweep order
    now does arithmetic on this rather than merely sorting it, so the two have
    to be reconciled here instead of being papered over with `str()`.
    """
    if isinstance(when, str):
        try:
            when = datetime.fromisoformat(when)
        except ValueError:
            return float("inf")     # unparseable: treat as maximally overdue
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (now - when).total_seconds() / 3600.0)


def board_tier(row: dict) -> str:
    """Which tier a board belongs to, from what discovery already measured.

    `hot` is the only tier defined by *relevance* rather than volume: a board
    that has posted an Australian data role is one this index exists to watch,
    whether it posts three roles or three hundred. `warm` catches the big
    Australian employers that happen not to have a data opening right now —
    they are exactly the ones where a new one appearing matters. Everything
    else is `cold`: real boards, worth keeping, not worth a fetch every day.
    """
    def n(key: str) -> int:
        try:
            return int(row.get(key) or 0)
        except (TypeError, ValueError):
            return 0
    if n("n_au_data") >= 1:
        return "hot"
    if n("n_au") >= 25:
        return "warm"
    return "cold"


def target_intervals(intervals: dict[str, float] | None = None
                     ) -> dict[tuple[str, str], float]:
    """-> {(vendor, token): hours between fetches}.

    Read from the same CSVs `configured_boards` reads, so a board adopted by
    the crawler is tiered the moment it is adopted. A board absent from these
    files — a `--token` on the command line, say — simply has no entry and
    `stalest` treats it as hot, which is the safe direction: the cost of
    over-fetching one board is a fetch, and the cost of under-fetching it is a
    job that sits open in the index after it closed.
    """
    want = {**DEFAULT_INTERVALS, **(intervals or {})}
    out: dict[tuple[str, str], float] = {}
    for path in (AUDIT, GLOBAL, DISCOVERED):
        if not path.exists():
            continue
        for r in csv.DictReader(path.open()):
            token = r.get("board_token")
            if not token:
                continue
            key = (r.get("ats_vendor", ""), token)
            hours = want[board_tier(r)]
            # Curated audits come first and carry no AU counts, so they tier as
            # cold; a later, richer row for the same board must be able to
            # promote it. Never demote.
            out[key] = min(out.get(key, hours), hours)
    return out


def stalest(plan: dict[str, list[str]], store: Store, budget: int,
            targets: dict[tuple[str, str], float] | None = None
            ) -> dict[str, list[str]]:
    """Cut `plan` down to `budget` boards, longest-unfetched first.

    A full sweep costs ~50s per board, so "every board every night" has a hard
    ceiling — around 390 boards against the workflow's 330-minute timeout, and
    discovery is meant to blow past that. Timing out mid-sweep is the worst
    available failure: it leaves a partial pass whose remaining boards look
    exactly like mass closures on the next run.

    Rotating the boards instead is safe in a way truncating the list is not,
    and the reason is `store.reconcile` — it is only ever called for a board
    that was actually fetched, so a board left out of tonight's pass simply
    keeps its existing rows and its `closed_at` values untouched. It goes
    *stale*, which `runs.summary` already counts and the runs page already
    shows, rather than wrong. Nothing is falsely closed by not looking.

    Never-*attempted* boards go first, so a board discovery adopted today is in
    the index tomorrow rather than whenever the rotation reaches it. The order
    is by last attempt rather than last success on purpose — see
    `Store.last_attempted`; ranking on success lets a board that never
    completes camp at the front of the queue forever.

    Boards are not ranked by raw age but by how *overdue* each one is against
    its own target interval — elapsed / target, biggest first. That single
    change is what lets the sweep run several times a day without several times
    the cost. Ranked by age alone, every board competes on one clock, so
    fetching the 190 boards that carry the Australian data roles twice a day
    means also fetching 640 tail boards twice a day. Ranked by overdue-ness a
    tail board on a seven-day interval simply does not become eligible in
    between, and the extra runs cost only what the hot tier costs.
    """
    targets = target_intervals() if targets is None else targets
    now = utcnow()
    ranked: list[tuple[float, str, str]] = []
    for vendor, tokens in plan.items():
        seen = store.last_attempted(vendor)
        for token in tokens:
            when = seen.get(token)
            if not when:
                overdue = float("inf")      # never attempted: always first
            else:
                hours = targets.get((vendor, token), DEFAULT_INTERVALS["hot"])
                overdue = _age_hours(when, now) / max(hours, 0.01)
            ranked.append((-overdue, vendor, token))
    ranked.sort(key=lambda r: r[0])

    out: dict[str, list[str]] = {}
    due = 0
    for score, vendor, token in ranked[:budget]:
        # A board fetched more recently than its interval is not due; taking it
        # anyway would spend the budget re-fetching the hot tier instead of
        # letting the next tier down come round.
        if -score < 1.0:
            continue
        out.setdefault(vendor, []).append(token)
        due += 1
    fresh = sum(1 for r in ranked[:budget] if r[0] == float("-inf"))
    print(f"budget {budget}: {due} due now ({fresh} never attempted), "
          f"{len(ranked) - due} not due or held over", file=sys.stderr)
    return out


def snapshot_from_fixture(vendor: str, token: str) -> BoardSnapshot:
    """Replay a recorded board through the adapter's own parser — lets closure
    detection be exercised without hitting the network, for any vendor.

    Note SmartRecruiters bodies are absent here: they come from a per-job
    request the replay deliberately does not make."""
    # Prefer the full dump; fall back to the committed sample so a fresh clone
    # (where fixtures/raw/ is git-ignored) can still replay offline.
    name = f"{token_slug(token)}.json"
    path = RAW / vendor / name
    if not path.exists():
        path = SAMPLES / vendor / name
    return ADAPTERS[vendor].parse(json.loads(path.read_text()), token)


def replay_board(vendor: str, token: str) -> BoardSnapshot:
    """`snapshot_from_fixture` with the isolation `run_board` gives the network
    path. A board with no recording — most of them, since `fixtures/raw/` is
    git-ignored — must not abort a `--vendor all` replay for the rest."""
    try:
        return snapshot_from_fixture(vendor, token)
    except Exception as exc:  # noqa: BLE001 - same vendor-isolation boundary
        return BoardSnapshot(
            ats_vendor=vendor, board_token=token, complete=False,
            error=f"{type(exc).__name__}: {exc}"[:300],
        )


async def sweep(vendor: str, tokens: list[str], store: Store,
                client: httpx.AsyncClient | None,
                deadline: float | None = None) -> tuple[int, int, int]:
    """Fetch and reconcile one vendor's boards. -> (boards, failures, skipped).

    Vendors are swept one after another, not concurrently: CONCURRENCY is a
    per-vendor politeness budget, and fanning seven adapters out at once would
    make it 28 requests in flight.

    `deadline` stops *starting* boards once the clock runs out, and skipped
    boards are never reconciled — the same safety argument `stalest` rests on.
    A board count is only a proxy for time, and a poor one: boards are not
    interchangeable units. Workday pages at 20 postings a request and then
    fetches per-job detail for every maybe-Australian role, so one 2,000-job
    tenant can cost minutes where a small Greenhouse board costs one request.
    The clock is what the runner actually enforces, so the clock is what the
    sweep should watch.

    Boards are reconciled in batches as they finish rather than in one pass
    at the end, so that a run the runner cancels keeps the work it had
    already done. See FLUSH_EVERY.
    """
    failures = 0
    skipped = 0

    def report(res) -> int:
        # flush=True because these lines are the only record that a board was
        # done at all, and the runs that most need reading are the ones the
        # runner kills. stdout is a pipe under Actions, so the default block
        # buffering drops the last few KB at exactly the wrong moment: a
        # cancelled sweep looked like it had reconciled nothing for two and a
        # half hours when it had simply never flushed.
        print(res.summary(), flush=True)
        for t in res.closed_titles:
            print(f"    closed: {t}", flush=True)
        return 1 if res.error else 0

    if client is None:
        # Offline replay. There is no long fetch to let go of the database
        # across, so the connection stays exactly as the caller left it.
        snaps = [replay_board(vendor, t) for t in tokens]
        for snap in snaps:
            failures += report(store.reconcile(snap))
        return len(snaps), failures, skipped

    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(token):
        async with sem:
            # Checked inside the semaphore, so it reflects the time the
            # board would actually start rather than when it was queued.
            if deadline is not None and time.monotonic() >= deadline:
                return None
            if not BOARD_TIMEOUT:
                return await run_board(ADAPTERS[vendor], client, token)
            try:
                async with asyncio.timeout(BOARD_TIMEOUT):
                    return await run_board(ADAPTERS[vendor], client, token)
            except TimeoutError:
                # `complete=False` is doing real work here: `store.py`
                # refuses to retire jobs from an incomplete snapshot, so a
                # board cut off halfway cannot be read as mass closures.
                return BoardSnapshot(
                    ats_vendor=vendor, board_token=token, complete=False,
                    error=f"exceeded {BOARD_TIMEOUT / 60:.0f}m board timeout")

    def flush(batch: list[BoardSnapshot]) -> None:
        """Write one chunk's boards, then let go of the database again.

        Synchronous, and called only between chunks — never while a request
        is in flight. That ordering is not tidiness: `_upsert` is one round
        trip per job, so reconciling a chunk of large Workday tenants is
        minutes of blocked event loop, and anything still fetching would sit
        past httpx's 45s read timeout and fail for no reason.
        """
        nonlocal failures
        if not batch:
            return
        store.reopen()
        try:
            for snap in batch:
                failures += report(store.reconcile(snap))
        finally:
            store.close()

    # Let go of the database for the fetch. Nothing in `one` touches it, and
    # this phase is long -- a whole-vendor Workday pass once measured 53
    # minutes before it wrote a single row, and a chunk of it still runs for
    # minutes. Holding the connection across that keeps a serverless compute
    # awake for the whole of it, which on a free tier is compute-hours spent
    # waiting on somebody else's HTTP.
    #
    # It also removes the failure this code was fixed for twice over: a
    # connection that is not held cannot be killed for being idle.
    store.close()
    done = 0
    try:
        for i in range(0, len(tokens), FLUSH_EVERY):
            results = await asyncio.gather(
                *(one(t) for t in tokens[i:i + FLUSH_EVERY]))
            batch = [r for r in results if r is not None]
            skipped += len(results) - len(batch)
            done += len(batch)
            flush(batch)
    finally:
        store.reopen()
    return done, failures, skipped


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vendor", default="greenhouse",
                    help="adapter name, or 'all' to sweep every vendor in turn")
    ap.add_argument("--token", action="append", help="board token (repeatable)")
    ap.add_argument("--from-fixtures", action="store_true", help="replay recorded boards")
    ap.add_argument("--max-boards", type=int, default=0, help="cap boards per vendor")
    ap.add_argument("--budget", type=int, default=0,
                    help="sweep at most this many boards in total, stalest "
                         "first (0 = every board, every pass)")
    ap.add_argument("--interval-hot", type=float, default=DEFAULT_INTERVALS["hot"],
                    help="hours between fetches for boards that post AU data "
                         "roles (default %(default)s)")
    ap.add_argument("--interval-warm", type=float, default=DEFAULT_INTERVALS["warm"],
                    help="hours for large AU employers with no data role open")
    ap.add_argument("--interval-cold", type=float, default=DEFAULT_INTERVALS["cold"],
                    help="hours for the tail")
    ap.add_argument("--deadline", type=float, default=0,
                    help="stop starting new boards after this many minutes "
                         "(0 = no limit). Boards are not interchangeable units "
                         "of time, so this is the real guard, not --budget")
    ap.add_argument("--no-reindex", action="store_true",
                    help="skip the FTS rebuild the UI searches over")
    args = ap.parse_args()

    if args.vendor == "all":
        if args.token:
            print("--token names a board on one board's vendor, so it needs a "
                  "single --vendor", file=sys.stderr)
            return 2
        vendors = list(ADAPTERS)
    elif args.vendor in ADAPTERS:
        vendors = [args.vendor]
    else:
        print(f"no adapter for {args.vendor}", file=sys.stderr)
        return 2

    # The store comes first now: the sweep order is a question only the
    # database can answer (which boards went longest without a fetch), so the
    # plan cannot be built before there is a connection to ask.
    store = Store()
    store.init_schema()

    plan: dict[str, list[str]] = {}
    for v in vendors:
        tokens = args.token or configured_boards(v)
        if args.max_boards:
            tokens = tokens[: args.max_boards]
        if tokens:
            plan[v] = tokens
    if not plan:
        print(f"no boards configured for {', '.join(vendors)}", file=sys.stderr)
        store.close()
        return 2

    configured = sum(len(t) for t in plan.values())
    if args.budget and not args.token:
        plan = stalest(plan, store, args.budget, target_intervals({
            "hot": args.interval_hot, "warm": args.interval_warm,
            "cold": args.interval_cold,
        }))
    total = sum(len(t) for t in plan.values())
    scope = f"{total} of {configured}" if total != configured else str(total)
    print(f"store: {store.backend}   vendors: {len(plan)}   boards: {scope}",
          file=sys.stderr)

    boards = failures = skipped = 0
    started = time.monotonic()

    deadline = started + args.deadline * 60 if args.deadline else None

    async def sweep_all(client):
        nonlocal boards, failures, skipped
        for vendor, tokens in plan.items():
            if deadline is not None and time.monotonic() >= deadline:
                skipped += len(tokens)
                print(f"--- {vendor}: {len(tokens)} boards skipped (out of time)",
                      file=sys.stderr)
                continue
            print(f"--- {vendor}: {len(tokens)} boards", file=sys.stderr)
            n, failed, n_skipped = await sweep(vendor, tokens, store, client,
                                               deadline)
            boards += n
            failures += failed
            skipped += n_skipped

    if args.from_fixtures:
        await sweep_all(None)
    else:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(45.0, connect=15.0),
            headers={"User-Agent": UA}, follow_redirects=True,
        ) as client:
            await sweep_all(client)

    # The UI searches jobs_fts, not jobs. Rebuilding it only at server start was
    # fine while every run was manual; once a scheduled sweep is landing jobs
    # daily, skipping this leaves the index fresh and the search stale.
    if not args.no_reindex and store.backend != "postgres":
        print(f"search index: {S.reindex(store.conn)} rows", file=sys.stderr)

    store.close()
    tail = f", {skipped} skipped (out of time)" if skipped else ""
    print(f"{boards - failures}/{boards} boards ok in "
          f"{time.monotonic() - started:.0f}s{tail}", file=sys.stderr)
    # One vendor failing must not fail the run for the others; a non-zero exit
    # only signals that *every* board failed. Note `boards` is never 0 here —
    # an empty plan returned above — so this cannot report 0-of-0 as failure.
    return 1 if boards and failures == boards else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
