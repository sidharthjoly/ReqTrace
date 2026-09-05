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
from pathlib import Path

import httpx

from . import search as S
from .adapters import ADAPTERS
from .adapters.base import run_board
from .models import BoardSnapshot, token_slug
from .store import Store

ROOT = Path(__file__).resolve().parent.parent.parent
AUDIT = ROOT / "data" / "step0_ats_audit.csv"
DISCOVERED = ROOT / "data" / "discovered_boards.csv"
GLOBAL = ROOT / "data" / "global_ats_audit.csv"
RAW = ROOT / "fixtures" / "raw"
SAMPLES = ROOT / "fixtures" / "samples"

UA = "reqtrace/0.1 (+personal job-search index; contact via repo)"
CONCURRENCY = 4


# Greenhouse/Ashby/SmartRecruiters resolve tokens case-insensitively; Lever
# does not, so only the former may be deduped on lowercase.
CASE_INSENSITIVE = {"greenhouse", "ashby", "smartrecruiters"}


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
                client: httpx.AsyncClient | None) -> tuple[int, int]:
    """Fetch and reconcile one vendor's boards. Returns (boards, failures).

    Vendors are swept one after another, not concurrently: CONCURRENCY is a
    per-vendor politeness budget, and fanning seven adapters out at once would
    make it 28 requests in flight."""
    if client is None:
        snaps = [replay_board(vendor, t) for t in tokens]
    else:
        sem = asyncio.Semaphore(CONCURRENCY)

        async def one(token):
            async with sem:
                return await run_board(ADAPTERS[vendor], client, token)

        snaps = await asyncio.gather(*(one(t) for t in tokens))

    failures = 0
    for snap in snaps:
        res = store.reconcile(snap)
        print(res.summary())
        for t in res.closed_titles:
            print(f"    closed: {t}")
        failures += 1 if res.error else 0
    return len(snaps), failures


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vendor", default="greenhouse",
                    help="adapter name, or 'all' to sweep every vendor in turn")
    ap.add_argument("--token", action="append", help="board token (repeatable)")
    ap.add_argument("--from-fixtures", action="store_true", help="replay recorded boards")
    ap.add_argument("--max-boards", type=int, default=0, help="cap boards per vendor")
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

    plan: dict[str, list[str]] = {}
    for v in vendors:
        tokens = args.token or configured_boards(v)
        if args.max_boards:
            tokens = tokens[: args.max_boards]
        if tokens:
            plan[v] = tokens
    if not plan:
        print(f"no boards configured for {', '.join(vendors)}", file=sys.stderr)
        return 2

    store = Store()
    store.init_schema()
    total = sum(len(t) for t in plan.values())
    print(f"store: {store.backend}   vendors: {len(plan)}   boards: {total}",
          file=sys.stderr)

    boards = failures = 0
    started = time.monotonic()

    async def sweep_all(client):
        nonlocal boards, failures
        for vendor, tokens in plan.items():
            print(f"--- {vendor}: {len(tokens)} boards", file=sys.stderr)
            n, failed = await sweep(vendor, tokens, store, client)
            boards += n
            failures += failed

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
    print(f"{boards - failures}/{boards} boards ok in "
          f"{time.monotonic() - started:.0f}s", file=sys.stderr)
    # One vendor failing must not fail the run for the others; a non-zero exit
    # only signals that *every* board failed. Note `boards` is never 0 here —
    # an empty plan returned above — so this cannot report 0-of-0 as failure.
    return 1 if boards and failures == boards else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
