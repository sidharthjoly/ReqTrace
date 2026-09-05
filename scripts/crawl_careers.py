"""Focused careers-page crawl — the half of board discovery Common Crawl can't do.

`discover_boards.py` asks Common Crawl "which URLs on `boards.greenhouse.io`
did you see?". This asks the employers directly: start at a company domain,
follow the careers-ish links, and read the ATS token out of whatever the
careers page embeds. It finds the boards a URL index structurally cannot —
Lever (barely in Common Crawl), iframed and self-hosted embeds, and the
two-part Workday / Oracle / Eightfold identities.

    # a handful of hosts first, always
    uv run python scripts/crawl_careers.py crawl --domain canva.com --max-pages 20

    # the employers Step 0 never resolved — NAB, Macquarie, ANZ and the other 19
    uv run python scripts/crawl_careers.py crawl --seeds unresolved --max-pages 400

    uv run python scripts/crawl_careers.py crawl --seeds au,global --max-pages 800
    uv run python scripts/crawl_careers.py crawl --seeds au --resume
    uv run python scripts/crawl_careers.py report

`report` writes `data/discovery/crawled_<vendor>.json` in exactly the shape
`discover_boards.py harvest` produces, so the existing validate/report phases
consume it unchanged — and the append-only adoption rule that protects
`data/discovered_boards.csv` keeps working, because this never writes that file.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from reqtrace.crawl import (  # noqa: E402
    UA, VALIDATABLE, Crawler, Finding,
)

ROOT = Path(__file__).resolve().parent.parent
DISC = ROOT / "data" / "discovery"
STATE = DISC / "crawl_state.json"
FINDINGS = DISC / "crawl_findings.csv"

SEED_SETS = {
    # name -> (csv, domain column, careers column, filter)
    "au": (ROOT / "data" / "companies_seed.csv", "domain", "careers_url", None),
    "global": (ROOT / "data" / "companies_global.csv", "domain", "careers_url", None),
    "audit": (ROOT / "data" / "step0_ats_audit.csv", "domain", "careers_url", None),
    # The rows Step 0 could not resolve to a board — the crawler's home turf.
    "unresolved": (ROOT / "data" / "step0_ats_audit.csv", "domain", "careers_url",
                   lambda r: not (r.get("board_token") or "").strip()),
}


def load_seeds(names: list[str]) -> list[tuple[str, str]]:
    """-> [(domain, careers_url)], deduplicated, first mention wins."""
    out: dict[str, str] = {}
    for name in names:
        spec = SEED_SETS.get(name)
        if spec is None:
            path, dom_col, car_col, keep = Path(name), "domain", "careers_url", None
            if not path.exists():
                raise SystemExit(f"unknown seed set or missing file: {name}")
        else:
            path, dom_col, car_col, keep = spec
        if not path.exists():
            print(f"  (skipping {name}: {path} not found)", file=sys.stderr)
            continue
        n = 0
        for row in csv.DictReader(path.open()):
            if keep and not keep(row):
                continue
            domain = (row.get(dom_col) or "").strip().lower()
            if not domain or domain in out:
                continue
            out[domain] = (row.get(car_col) or "").strip()
            n += 1
        print(f"  {name}: {n} seeds", file=sys.stderr)
    return list(out.items())


def write_findings(findings: list[Finding]) -> None:
    """One row per (vendor, token) ever found, append-only across runs — the
    same rule `discovered_boards.csv` follows and for the same reason: a board
    that stops being visible is still a board we know about."""
    rows: dict[tuple[str, str, str], dict] = {}
    if FINDINGS.exists():
        for r in csv.DictReader(FINDINGS.open()):
            rows[(r["ats_vendor"], r["board_token"], r.get("seed_domain", ""))] = r
    for f in findings:
        rows.setdefault((f.vendor, f.token, f.seed), f.as_row())
    ordered = sorted(rows.values(),
                     key=lambda r: (r["ats_vendor"], r["board_token"].lower(),
                                    r.get("seed_domain", "")))
    with FINDINGS.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["ats_vendor", "board_token", "found_on",
                                           "seed_domain", "ingestable"])
        w.writeheader()
        w.writerows(ordered)
    print(f"{len(ordered)} findings -> {FINDINGS}", file=sys.stderr)


def emit_candidates() -> None:
    """Hand the validatable vendors to `discover_boards.py` in its own format.

    Deliberately a *separate* file from `candidates_<vendor>.json`: that one is
    the Common Crawl harvest, and overwriting it would throw away 6,832 tokens
    to gain a few hundred. `validate` reads the union.
    """
    if not FINDINGS.exists():
        raise SystemExit("nothing crawled yet — run `crawl` first")
    rows = list(csv.DictReader(FINDINGS.open()))
    for vendor in VALIDATABLE:
        # Lever tokens are case-sensitive and must never be folded.
        tokens = sorted({r["board_token"] for r in rows if r["ats_vendor"] == vendor})
        if not tokens:
            continue
        out = DISC / f"crawled_{vendor}.json"
        out.write_text(json.dumps(tokens, indent=0))
        print(f"{vendor}: {len(tokens)} candidates -> {out}", file=sys.stderr)

    enterprise = [r for r in rows if r["ats_vendor"] not in VALIDATABLE]
    if enterprise:
        print(f"\n{len(enterprise)} findings on vendors validate/ cannot check:",
              file=sys.stderr)
        by_vendor: dict[str, list[dict]] = {}
        for r in enterprise:
            by_vendor.setdefault(r["ats_vendor"], []).append(r)
        for vendor, rs in sorted(by_vendor.items(), key=lambda kv: -len(kv[1])):
            sample = ", ".join(x["board_token"] for x in rs[:3])
            print(f"  {vendor:16s} {len(rs):4d}  {sample}", file=sys.stderr)
        print("  workday/oracle/eightfold rows are ingestable: paste them into\n"
              "  data/step0_ats_audit.csv to adopt. The rest are intelligence\n"
              "  about which suite an employer runs, not tokens.", file=sys.stderr)


async def do_crawl(args: argparse.Namespace) -> int:
    if args.domain or args.url:
        seeds = [(d.strip().lower(), "") for d in (args.domain or "").split(",") if d.strip()]
        urls = [u.strip() for u in (args.url or "").split(",") if u.strip()]
    else:
        print("seeds:", file=sys.stderr)
        seeds, urls = load_seeds([s.strip() for s in args.seeds.split(",")]), []

    if not seeds and not urls:
        raise SystemExit("no seeds")

    verbose = not args.quiet

    def log(kind: str, msg: str) -> None:
        if kind == "found" or verbose:
            print(f"  [{kind}] {msg}", file=sys.stderr)

    limits = httpx.Limits(max_connections=args.concurrency * 2)
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(20.0, connect=10.0),
        headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"},
        follow_redirects=True, limits=limits, http2=False,
    )
    c = Crawler(
        client, max_pages=args.max_pages, max_per_host=args.max_per_host,
        max_depth=args.max_depth, concurrency=args.concurrency,
        delay=args.delay, use_sitemaps=not args.no_sitemaps, on_event=log,
    )
    if args.resume and STATE.exists():
        c.load_state(json.loads(STATE.read_text()))
        print(f"resumed: {len(c.seen)} urls seen, "
              f"{len(c.findings)} findings carried", file=sys.stderr)
    for domain, careers in seeds:
        c.seed_from(domain, careers)
    for u in urls:
        c.enqueue(u, score=500, depth=0, seed="")

    print(f"crawling: {len(seeds) + len(urls)} seeds, budget {args.max_pages} pages, "
          f"{args.max_per_host}/host, depth {args.max_depth}, "
          f"{args.delay}s per host\n", file=sys.stderr)
    try:
        async with client:
            await c.run()
    finally:
        # `finally`, not `except KeyboardInterrupt`: Ctrl-C is raised on the main
        # thread inside asyncio.run's own stack, never delivered into the `await`
        # above, so an except clause here would not fire and the state write would
        # be skipped entirely. That is the one case it must not be — a crawl whose
        # state is lost re-fetches every page it already has on the next run,
        # which is precisely the rudeness --resume exists to avoid.
        DISC.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(c.state(), indent=1))
        write_findings(c.findings)

    print(f"\n{c.pages} pages fetched, {len(c.findings)} boards found", file=sys.stderr)
    interesting = {k: v for k, v in c.stats.items()
                   if k.startswith(("found_", "robots_", "error_", "status_4",
                                    "status_5", "sitemaps"))}
    for k, v in sorted(interesting.items()):
        print(f"  {k}: {v}", file=sys.stderr)
    hosts_hit = sum(1 for h in c.hosts.values() if h.exhausted)
    print(f"  hosts resolved to a board: {hosts_hit}/{len(c.hosts)}", file=sys.stderr)
    print("\nnext: scripts/crawl_careers.py report", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="phase", required=True)

    cr = sub.add_parser("crawl", help="fetch careers pages and extract ATS tokens")
    cr.add_argument("--seeds", default="unresolved",
                    help=f"comma-separated: {', '.join(SEED_SETS)}, or a CSV path")
    cr.add_argument("--domain", help="crawl these domains instead of a seed set")
    cr.add_argument("--url", help="start at these exact URLs")
    cr.add_argument("--max-pages", type=int, default=200,
                    help="global request budget — the crawl's hard stop (default 200)")
    cr.add_argument("--max-per-host", type=int, default=12)
    cr.add_argument("--max-depth", type=int, default=3)
    cr.add_argument("--concurrency", type=int, default=4,
                    help="across DIFFERENT hosts; one host is always serialised")
    cr.add_argument("--delay", type=float, default=1.5,
                    help="minimum seconds between requests to one host")
    cr.add_argument("--no-sitemaps", action="store_true")
    cr.add_argument("--resume", action="store_true")
    cr.add_argument("--quiet", action="store_true")

    sub.add_parser("report", help="write crawled_<vendor>.json for discover_boards.py")

    args = ap.parse_args()
    DISC.mkdir(parents=True, exist_ok=True)

    if args.phase == "report":
        emit_candidates()
        return 0
    try:
        return asyncio.run(do_crawl(args))
    except KeyboardInterrupt:
        # do_crawl's `finally` has already persisted state by the time this
        # lands here; all that is left is to not print a traceback over it.
        print("\ninterrupted — state saved, resume with --resume", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
