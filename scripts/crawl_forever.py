"""The crawler that doesn't stop.

`crawl_careers.py` answers "which board does each of these 64 employers use?"
and finishes. This answers "which employers are there?", which has no end, so
neither does this. It runs laps until something kills it, and every lap is
durable on its own: claim a batch of URLs from the database, crawl them
politely, write back what was found, repeat.

    # watch it work, against the local SQLite index
    uv run python scripts/crawl_forever.py --seed au,global,audit --laps 3

    # the real thing: seeded once, then left alone
    uv run python scripts/crawl_forever.py --expand
    uv run python scripts/crawl_forever.py --expand --lap-pages 40 --rest 120

    # what has it found?
    uv run python scripts/crawl_forever.py status
    uv run python scripts/crawl_forever.py adopt        # hand boards to ingestion

Three properties are worth stating plainly, because they are what make an
always-on crawler defensible rather than just impolite at scale:

**It is slow on purpose.** The default is 20 pages a lap and a minute of rest
between laps — roughly 1,200 pages a day. Every politeness rule the focused
crawler already had still applies underneath (robots.txt per host including
`Crawl-delay`, one request at a time per host, a declared User-Agent, byte
caps), and the rest interval is on top of all of it. "Doesn't have to be fast"
is the design centre, not a compromise.

**It never re-fetches.** Deduplication is a primary key in `crawl_frontier`,
so a URL crawled last week is not crawled again after a restart, a crash, or a
second crawler starting up against the same database.

**It stops per employer, forever.** A host that yields a board fingerprint is
marked exhausted in `crawl_hosts` and never queued again. The crawl is
unbounded in employers and strictly bounded per employer — the same rule
`crawl.py` has always had, now persisted so a restart cannot forget it.

What it produces is rows in `crawl_findings`. `adopt` turns the ingestable ones
into `data/discovered_boards.csv` entries, which is what `reqtrace.run` reads —
and that file stays in git deliberately, because it is the record that protects
closure detection, and a record with no version history is one bad sweep away
from silently un-adopting boards whose jobs would then sit open forever.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import signal
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from reqtrace.crawl import (  # noqa: E402
    CASE_INSENSITIVE, UA, Crawler, canonicalise, host_of, registrable,
)
from reqtrace.frontier import Frontier  # noqa: E402
from reqtrace.store import Store  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DISC = ROOT / "data" / "discovery"
DISCOVERED = ROOT / "data" / "discovered_boards.csv"

SEED_FILES = {
    "au": ROOT / "data" / "companies_seed.csv",
    "global": ROOT / "data" / "companies_global.csv",
    "audit": ROOT / "data" / "step0_ats_audit.csv",
    "discovered": ROOT / "data" / "discovered_boards.csv",
}

# Directory pages: the densest lists of employer domains on the open web, and
# the reason `--expand` has anything to chew on before it has crawled anything.
# Seeding a few by hand beats waiting for the crawl to stumble onto one.
DIRECTORY_SEEDS = [
    "https://www.startupdaily.net/partners/",
    "https://blackbird.vc/portfolio",
    "https://squarepegcap.com/portfolio/",
    "https://airtree.vc/portfolio",
    "https://www.afr.com/companies",
    "https://techcouncil.com.au/members/",
    # startupaus.org/members/ was here and 404s — StartupAUS folded into the
    # Tech Council, whose member list is the line above. Checked 2026-09-10.
]

#: Rest longer than this and the crawler disconnects while it waits, so a
#: serverless compute can suspend. Below it the reconnect churn costs more than
#: the idle connection does — and a suspend timeout is typically five minutes
#: anyway, so a short rest would never have suspended regardless.
RELEASE_DB_AFTER = 60.0

_STOP = False


def _handle_stop(signum, _frame) -> None:
    """Finish the lap in progress, then exit. A crawler killed mid-request
    leaves claimed rows behind; letting the lap end cleanly returns them."""
    global _STOP
    _STOP = True
    print(f"\n  signal {signum} — finishing this lap, then stopping",
          file=sys.stderr)


# ---------------------------------------------------------------------------
# seeding
# ---------------------------------------------------------------------------

def seed_rows(names: list[str]) -> list[tuple[str, str]]:
    """-> [(domain, careers_url)] from the named seed CSVs."""
    out: dict[str, str] = {}
    for name in names:
        path = SEED_FILES.get(name) or Path(name)
        if not path.exists():
            print(f"  (skipping {name}: no {path})", file=sys.stderr)
            continue
        n = 0
        for row in csv.DictReader(path.open()):
            domain = (row.get("domain") or "").strip().lower().lstrip("@")
            if not domain or domain in out:
                continue
            out[domain] = (row.get("careers_url") or "").strip()
            n += 1
        print(f"  {name}: {n} domains", file=sys.stderr)
    return list(out.items())


def enqueue_seeds(f: Frontier, seeds: list[tuple[str, str]], *,
                  score: int = 300) -> int:
    """A seed is two rows: the hand-verified careers URL where the audit has
    one, and the homepage always. The careers URL outranks it — that column is
    a human having already done the crawl's job for that employer."""
    rows = []
    for domain, careers in seeds:
        if careers:
            rows.append((careers, host_of(careers) or domain, 500, 0, domain))
        rows.append((f"https://{domain}/", domain, score, 0, domain))
    return f.add(rows)


# ---------------------------------------------------------------------------
# one lap
# ---------------------------------------------------------------------------

async def one_lap(f: Frontier, args, client: httpx.AsyncClient, lap: int) -> dict:
    """Claim, crawl, write back. Everything durable by the time it returns."""
    batch = f.claim(args.lap_pages, max_per_host=args.max_per_host)
    if not batch:
        return {"claimed": 0}

    def log(kind: str, msg: str) -> None:
        if kind in ("found", "seeds") or args.verbose:
            print(f"    [{kind}] {msg}", file=sys.stderr)

    c = Crawler(
        client, max_pages=args.lap_pages, max_per_host=args.max_per_host,
        max_depth=args.max_depth, concurrency=args.concurrency,
        delay=args.delay, use_sitemaps=not args.no_sitemaps,
        expand=args.expand, max_seeds=args.max_seeds_per_lap, on_event=log,
    )
    # Per-host budgets are stored, not per-lap: without this a daemon would
    # crawl `max_per_host` pages of the same site on every single lap.
    c.load_hosts(f.host_state(sorted({host_of(p.url) for p in batch})))

    for p in batch:
        c.enqueue(p.url, score=p.score, depth=p.depth, seed=p.seed)

    await c.run()

    # --- write back, in the order that keeps a crash recoverable ----------
    # Findings first: they are the payload, and the only thing whose loss is
    # not recoverable by simply crawling the page again.
    new_boards = f.record(c.findings)
    f.save_hosts(c.host_rows())

    discovered = f.add([
        (url, host_of(url), score, depth, seed)
        for url, score, depth, seed in c.pending()
    ])

    seeded = 0
    if args.expand and c.new_seeds:
        # `add` returns how many rows were genuinely new, so domains the crawl
        # already knows cost one row in a batched insert and are reported as
        # zero — no need to pre-load the known set to find that out.
        seeded = enqueue_seeds(f, [(d, "") for d in sorted(c.new_seeds)], score=200)

    # Mark done only what was really fetched; hand the rest of the batch back.
    # Compared on the canonical form: `Frontier.add` normalises everything it
    # stores, but rows written before it did are still in the queue, and those
    # would otherwise never match and never retire.
    visited = [p.url for p in batch if (canonicalise(p.url) or p.url) in c.visited]
    f.finish(visited)
    f.release([p.url for p in batch if p.url not in c.visited])
    # Then retire whatever those releases just made unreachable: a host that
    # resolved to a board this lap is finished, and its remaining queued links
    # are dead rows.
    # Narrowed to the hosts this lap touched — but a link to an already-
    # exhausted host can be discovered many laps after that host finished, and
    # such a row never appears in a later `host_rows()`. `claim` still refuses
    # to serve it, so nothing is fetched wrongly; it is the pending count that
    # drifts, which is the whole reason this call exists. So every 20th lap
    # pays for the unnarrowed sweep and squares the books.
    retired = f.retire_exhausted(None if lap % 20 == 0 else sorted(c.host_rows()))

    return {
        "claimed": len(batch), "fetched": len(visited),
        "boards": new_boards, "discovered": discovered, "seeded": seeded,
        "retired": retired,
        "resolved": sum(1 for st in c.hosts.values() if st.exhausted),
    }


async def forever(args) -> int:
    store = Store()
    store.init_schema()
    f = Frontier(store)
    f.init_schema()

    reclaimed = f.requeue_stale_claims()
    if reclaimed:
        print(f"reclaimed {reclaimed} URLs a previous run left claimed",
              file=sys.stderr)

    if args.seed:
        print("seeding:", file=sys.stderr)
        n = enqueue_seeds(f, seed_rows([s.strip() for s in args.seed.split(",")]))
        print(f"  -> {n} new URLs queued", file=sys.stderr)
    if args.seed_directories:
        n = f.add([(u, host_of(u), 250, 0, registrable(u)) for u in DIRECTORY_SEEDS])
        print(f"directory pages: {n} new URLs queued", file=sys.stderr)

    # An empty frontier seeds itself rather than erroring. This is the state
    # every *first* run is in — a fresh database, or the first scheduled run
    # after the frontier moved from a local SQLite file to Postgres — and
    # failing there means the scheduled crawl never starts at all, and a
    # KeepAlive daemon respawns into the same failure forever.
    if not f.count("pending"):
        print("frontier is empty; seeding from the known employers",
              file=sys.stderr)
        n = enqueue_seeds(f, seed_rows(list(SEED_FILES)))
        n += f.add([(u, host_of(u), 250, 0, registrable(u))
                    for u in DIRECTORY_SEEDS])
        print(f"  -> {n} URLs queued", file=sys.stderr)
        if not n:
            print("nothing to seed from — no seed CSVs found", file=sys.stderr)
            store.close()
            return 1

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    budget = f", stopping after {args.minutes:g}m" if args.minutes else ""
    print(f"store: {store.backend}   pending: {f.count('pending')}   "
          f"{args.lap_pages} pages/lap, {args.rest}s rest, "
          f"expand={'on' if args.expand else 'off'}{budget}\n", file=sys.stderr)

    lap = 0
    totals = {"fetched": 0, "boards": 0, "discovered": 0, "seeded": 0}
    started = time.monotonic()
    # A wall-clock deadline, not a lap count. How long a lap takes is emergent
    # — it depends on how many of the claimed URLs share a host, what
    # `Crawl-delay` those hosts declare, and how many time out — so converting
    # "crawl for 45 minutes" into a number of laps ahead of time is guesswork
    # that is wrong by an order of magnitude in either direction. On a runner
    # with a hard job timeout, being wrong the slow way kills the job.
    deadline = started + args.minutes * 60 if args.minutes else None
    try:
        while not _STOP and (not args.laps or lap < args.laps):
            if deadline and time.monotonic() >= deadline:
                print(f"reached the {args.minutes}-minute budget", file=sys.stderr)
                break
            lap += 1
            r = await one_lap(f, args, args.client, lap)
            if not r["claimed"]:
                # Nothing claimable: either the frontier really is drained, or
                # every pending host is exhausted. Resting is right in both
                # cases — with --expand a later lap can refill it.
                print(f"lap {lap}: nothing to claim, resting", file=sys.stderr)
                if not args.expand and not f.count("pending"):
                    print("frontier drained and expansion is off — done",
                          file=sys.stderr)
                    break
            else:
                for k in totals:
                    totals[k] += r.get(k, 0)
                print(f"lap {lap}: fetched {r['fetched']}/{r['claimed']}  "
                      f"boards +{r['boards']}  urls +{r['discovered']}  "
                      f"seeds +{r['seeded']}  | pending {f.count('pending')}",
                      file=sys.stderr)
            if _STOP or (args.laps and lap >= args.laps):
                break
            rest = args.rest
            if deadline:
                # Never sleep past the deadline: the rest interval is politeness
                # between laps, not a reason to hold a runner open doing nothing.
                rest = min(rest, max(0.0, deadline - time.monotonic()))
            # Let go of the database across a long rest. A serverless Postgres
            # only suspends its compute once nothing is connected, so a process
            # that runs for weeks holding one connection bills — or spends its
            # free-tier compute-hour budget on — every hour it is alive rather
            # than every hour it is working. At a 6-minute rest that is the
            # difference between ~180 compute-hours a month and under ten.
            #
            # Nothing is lost by dropping it: every Frontier method commits as
            # it goes, so there is never uncommitted state to carry across.
            if rest >= RELEASE_DB_AFTER:
                store.close()
                await asyncio.sleep(rest)
                store.reopen()
                f.rebind()
            else:
                await asyncio.sleep(rest)
    finally:
        mins = (time.monotonic() - started) / 60
        print(f"\n{lap} laps in {mins:.1f}m — {totals['fetched']} pages, "
              f"{totals['boards']} new boards, {totals['discovered']} urls queued, "
              f"{totals['seeded']} employers discovered", file=sys.stderr)
        show_status(f)
        store.close()
    return 0


# ---------------------------------------------------------------------------
# status and adoption
# ---------------------------------------------------------------------------

def show_status(f: Frontier) -> None:
    st = f.stats()
    print("\nfrontier:", file=sys.stderr)
    for k, v in sorted(st.items()):
        print(f"  {k:22s} {v}", file=sys.stderr)
    waiting = st.get("never_adopted", 0)
    if waiting:
        print(f"\n{waiting} ingestable boards are waiting — "
              f"scripts/crawl_forever.py adopt", file=sys.stderr)


def _report_order(row: dict) -> tuple:
    """`discover_boards.py report`'s sort key, tolerant of the blank counts
    `adopt` writes for a board nothing has validated yet."""
    def n(key: str) -> int:
        try:
            return int(row.get(key) or 0)
        except ValueError:
            return 0
    return (-n("n_au_data"), -n("n_au"))


def adopt(f: Frontier, dry_run: bool = False) -> int:
    """Findings -> `data/discovered_boards.csv`, the file ingestion reads.

    Append-only, the same rule `discover_boards.py report` follows and for the
    same reason: a board that stops showing roles must keep being fetched, or
    every job it left behind sits `closed_at IS NULL` forever. This only ever
    adds rows.

    Boards land here *unvalidated* — the crawl proved the token appears on the
    employer's own careers page, which is a stronger signal than a URL-index
    regex ever gives, and `run_board` already treats a 404 as a settled answer
    rather than an error. A token that turns out to be wrong costs one request
    per sweep and shows up on the runs page as a failing board.

    It is also **idempotent**: what counts as adopted is what this file says,
    not what `adopted_at` says. Running it twice adds nothing the second time,
    and a run whose commit never reaches the remote simply offers the same
    boards again — see `Frontier.pending_adoption` for why that matters.
    """
    rows = f.pending_adoption(ingestable_only=True)
    if not rows:
        print("nothing new to adopt", file=sys.stderr)
        return 0

    def key(vendor: str, token: str) -> tuple[str, str]:
        """Lever tokens are case-sensitive — `jobs.lever.co/Zeller` resolves and
        `/zeller` 404s — so folding every vendor to lowercase here, as this did,
        would treat two distinct Lever boards as one and drop the second."""
        return (vendor, token.lower() if vendor in CASE_INSENSITIVE else token)

    existing: dict[tuple[str, str], dict] = {}
    fields = ["ats_vendor", "board_token", "board_name", "n_jobs", "n_au",
              "au_ratio", "n_au_data", "sample_au_role", "board_url"]
    if DISCOVERED.exists():
        reader = csv.DictReader(DISCOVERED.open())
        fields = reader.fieldnames or fields
        for r in reader:
            existing[key(r["ats_vendor"], r["board_token"])] = r

    added = []
    for r in rows:
        k = key(r["ats_vendor"], r["board_token"])
        if k in existing:
            continue
        row = {k: "" for k in fields}
        row.update({
            "ats_vendor": r["ats_vendor"], "board_token": r["board_token"],
            "board_name": "", "n_jobs": "0", "n_au": "0", "au_ratio": "0",
            "n_au_data": "0", "sample_au_role": "",
            "board_url": r.get("found_on", ""),
        })
        existing[k] = row
        added.append(r)

    if not added:
        if not dry_run:
            f.mark_adopted([(r["ats_vendor"], r["board_token"], r["seed_domain"])
                            for r in rows])
        print(f"{len(rows)} findings are already in {DISCOVERED.name}",
              file=sys.stderr)
        return 0

    by_vendor: dict[str, int] = {}
    for r in added:
        by_vendor[r["ats_vendor"]] = by_vendor.get(r["ats_vendor"], 0) + 1
    print(f"adopting {len(added)} boards: "
          + ", ".join(f"{v} {k}" for k, v in sorted(by_vendor.items())),
          file=sys.stderr)
    for r in added[:15]:
        print(f"  {r['ats_vendor']}:{r['board_token']}  <- {r['seed_domain']}",
              file=sys.stderr)
    if len(added) > 15:
        print(f"  ... and {len(added) - 15} more", file=sys.stderr)

    if dry_run:
        print("(dry run — nothing written)", file=sys.stderr)
        return 0

    # The same ordering `discover_boards.py report` uses, and matching it is
    # not cosmetic. The two write this file alternately; when they disagreed on
    # sort order, every run rewrote all 300-odd rows, so each crawl commit
    # touched the whole file — history that says nothing about what changed,
    # and a rebase conflict surface spanning the entire file for what is
    # actually a handful of appended rows.
    out = sorted(existing.values(), key=_report_order)
    with DISCOVERED.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(out)
    f.mark_adopted([(r["ats_vendor"], r["board_token"], r["seed_domain"])
                    for r in rows])
    print(f"{len(out)} boards -> {DISCOVERED}", file=sys.stderr)
    print("next sweep picks them up: uv run python -m reqtrace.run --vendor all",
          file=sys.stderr)
    return 0


def export_candidates(f: Frontier) -> int:
    """Write `crawled_<vendor>.json` so `discover_boards.py validate` can check
    the crawl's findings against the vendors' feeds, exactly as before."""
    rows = f.pending_adoption(ingestable_only=False)
    DISC.mkdir(parents=True, exist_ok=True)
    by_vendor: dict[str, set[str]] = {}
    for r in rows:
        by_vendor.setdefault(r["ats_vendor"], set()).add(r["board_token"])
    # Workday is here because `discover_boards.py` can now check it — two
    # POSTs per token against the search endpoint. The vendors absent from this
    # list are the ones with no validation path at all (Avature, SuccessFactors,
    # iCIMS and the rest); their findings stay intelligence about which suite an
    # employer runs until somebody writes the adapter.
    for vendor in ("greenhouse", "lever", "ashby", "smartrecruiters", "workday"):
        tokens = by_vendor.get(vendor)
        if not tokens:
            continue
        path = DISC / f"crawled_{vendor}.json"
        prior = set(json.loads(path.read_text())) if path.exists() else set()
        merged = sorted(prior | tokens)   # never drop a candidate we once had
        path.write_text(json.dumps(merged, indent=0))
        print(f"{vendor}: {len(merged)} candidates ({len(tokens - prior)} new) "
              f"-> {path}", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", default="crawl",
                    choices=["crawl", "status", "adopt", "candidates"])
    ap.add_argument("--seed", help=f"seed from: {', '.join(SEED_FILES)}, or a CSV path")
    ap.add_argument("--seed-directories", action="store_true",
                    help="queue the built-in portfolio/member directory pages")
    ap.add_argument("--expand", action="store_true",
                    help="harvest new employer domains from directory pages — "
                         "this is what makes the crawl unbounded")
    ap.add_argument("--laps", type=int, default=0, help="stop after N laps (0 = forever)")
    ap.add_argument("--minutes", type=float, default=0,
                    help="stop after this much wall clock (0 = no deadline). "
                         "What a scheduled run should use — lap duration is "
                         "emergent, so a lap count is not a time budget.")
    ap.add_argument("--lap-pages", type=int, default=20, help="pages per lap")
    ap.add_argument("--rest", type=float, default=60.0, help="seconds between laps")
    ap.add_argument("--max-per-host", type=int, default=12,
                    help="lifetime page budget per host, across all laps")
    ap.add_argument("--max-depth", type=int, default=3)
    ap.add_argument("--concurrency", type=int, default=3,
                    help="across DIFFERENT hosts; one host is always serialised")
    ap.add_argument("--delay", type=float, default=1.5,
                    help="minimum seconds between requests to one host")
    ap.add_argument("--max-seeds-per-lap", type=int, default=200)
    ap.add_argument("--no-sitemaps", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="adopt: show, don't write")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.command in ("status", "adopt", "candidates"):
        store = Store()
        store.init_schema()
        f = Frontier(store)
        f.init_schema()
        try:
            if args.command == "status":
                show_status(f)
                return 0
            if args.command == "candidates":
                return export_candidates(f)
            return adopt(f, dry_run=args.dry_run)
        finally:
            store.close()

    async def run() -> int:
        limits = httpx.Limits(max_connections=args.concurrency * 2)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=10.0),
            headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"},
            follow_redirects=True, limits=limits, http2=False,
        ) as client:
            args.client = client
            return await forever(args)

    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
