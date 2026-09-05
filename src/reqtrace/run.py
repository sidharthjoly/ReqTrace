"""Run one ingestion pass: fetch every configured board, reconcile, report.

Usage:
    uv run python -m reqtrace.run                # all greenhouse boards in the audit CSV
    uv run python -m reqtrace.run --token quantium
    uv run python -m reqtrace.run --from-fixtures   # offline replay, no network
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from pathlib import Path

import httpx

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


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vendor", default="greenhouse")
    ap.add_argument("--token", action="append", help="board token (repeatable)")
    ap.add_argument("--from-fixtures", action="store_true", help="replay recorded boards")
    ap.add_argument("--max-boards", type=int, default=0, help="cap boards this run")
    args = ap.parse_args()

    adapter = ADAPTERS.get(args.vendor)
    if adapter is None:
        print(f"no adapter for {args.vendor}", file=sys.stderr)
        return 2

    tokens = args.token or configured_boards(args.vendor)
    if args.max_boards:
        tokens = tokens[: args.max_boards]
    if not tokens:
        print(f"no {args.vendor} boards configured", file=sys.stderr)
        return 2

    store = Store()
    store.init_schema()
    print(f"store: {store.backend}   boards: {len(tokens)}", file=sys.stderr)

    if args.from_fixtures:
        snaps = [snapshot_from_fixture(args.vendor, t) for t in tokens]
    else:
        sem = asyncio.Semaphore(CONCURRENCY)

        async def one(token):
            async with sem:
                return await run_board(adapter, client, token)

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(45.0, connect=15.0),
            headers={"User-Agent": UA}, follow_redirects=True,
        ) as client:
            snaps = await asyncio.gather(*(one(t) for t in tokens))

    failures = 0
    for snap in snaps:
        res = store.reconcile(snap)
        print(res.summary())
        for t in res.closed_titles:
            print(f"    closed: {t}")
        failures += 1 if res.error else 0

    store.close()
    # One vendor failing must not fail the run for the others; a non-zero exit
    # only signals that *every* board failed.
    return 1 if failures == len(snaps) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
