#!/usr/bin/env python3
"""Export the index as a static site — the same two pages, no Python behind them.

The pages in `src/reqtrace/static/` already run in two modes: served by
`reqtrace.web` they call `/api/*`, and served as plain files they look for
`data/manifest.json` and filter in the browser instead. This writes the second
half of that, so nothing here is a fork of the live UI — it is the same files
plus JSON.

    python scripts/export_static.py              # build site/
    python scripts/export_static.py --serve      # build, then serve it locally
    python scripts/export_static.py --publish    # build, push to the gh-pages branch

**Australian open roles only.** The whole index is 51k rows and 28MB of JSON,
which is not a page, it is a download. AU open roles are 3,356 rows and ~3MB,
which gzips to well under a megabyte — and AU is what the index is for.

A static export is a snapshot: it is stale the moment the next sweep lands.
Both pages therefore carry the export timestamp, and `/runs` says outright that
its "N hours ago" figures are counted from the export rather than from now.
Nothing here re-exports on its own — a GitHub Actions runner cannot see
`data/jobs.db` (see the README), so this runs locally, from the same machine
as the sweep.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from reqtrace import runs, search  # noqa: E402
from reqtrace.store import DEFAULT_SQLITE  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "reqtrace" / "static"
SITE = ROOT / "site"
PAGES = ("index.html", "runs.html")

JOBS_SQL = """
SELECT j.ats_vendor, j.board_token, j.external_id, j.title,
       COALESCE(c.name, j.board_token) AS company,
       j.location_city, j.location_country, j.location_raw, j.remote_type,
       j.salary_min, j.salary_max, j.salary_currency, j.salary_period,
       j.department, j.seniority, j.apply_url,
       j.posted_at, j.first_seen_at,
       SUBSTR(COALESCE(j.description_text, ''), 1, 320) AS snippet
FROM jobs j
LEFT JOIN companies c
  ON c.ats_vendor = j.ats_vendor AND c.board_token = j.board_token
WHERE j.closed_at IS NULL AND j.location_country = 'AU'
"""


def build(db: Path) -> dict:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    jobs = [dict(r) for r in conn.execute(JOBS_SQL).fetchall()]
    health = runs.health(conn)
    stats = search.stats(conn)
    conn.close()

    (SITE / "data").mkdir(parents=True, exist_ok=True)
    for name in PAGES:
        shutil.copy2(STATIC / name, SITE / name)
    # Pages would otherwise run the output through Jekyll, which drops files
    # and directories beginning with an underscore.
    (SITE / ".nojekyll").write_text("")

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": stats,
        "scope": "open roles in Australia",
        "jobs": len(jobs),
        # Shipped rather than retyped in JS: one definition of "is this a data
        # role", so the server filter and the static filter cannot drift.
        "data_terms": list(search.DATA_TERMS),
        "analyst_exclude": list(search.ANALYST_EXCLUDE),
    }
    write = lambda name, obj: (SITE / "data" / name).write_text(  # noqa: E731
        json.dumps(obj, separators=(",", ":"), default=str))
    write("manifest.json", manifest)
    write("jobs.json", jobs)
    write("health.json", health)

    return manifest


def size_report() -> None:
    for f in sorted(SITE.rglob("*")):
        if f.is_file():
            print(f"  {f.relative_to(SITE)!s:24} {f.stat().st_size / 1e3:>9.1f} KB")


def publish(branch: str = "gh-pages") -> int:
    """Push `site/` to an orphan branch, one commit deep.

    An orphan commit each time rather than a history: the export is a
    regenerable snapshot, and 3MB of JSON committed daily would be a gigabyte
    of git history a year. `main` never carries the data at all."""
    if subprocess.run(["git", "diff", "--quiet"], cwd=ROOT).returncode:
        print("working tree is dirty — commit or stash before publishing",
              file=sys.stderr)
        return 1
    tmp = ROOT / ".git" / "pages-worktree"
    subprocess.run(["git", "worktree", "remove", "--force", str(tmp)],
                   cwd=ROOT, capture_output=True)
    r = subprocess.run(["git", "worktree", "add", "--detach", str(tmp)],
                       cwd=ROOT, capture_output=True, text=True)
    if r.returncode:
        print(r.stderr.strip(), file=sys.stderr)
        return 1
    try:
        subprocess.run(["git", "checkout", "--orphan", branch], cwd=tmp, check=True,
                       capture_output=True)
        subprocess.run(["git", "rm", "-rf", "."], cwd=tmp, check=True,
                       capture_output=True)
        for item in SITE.iterdir():
            dest = tmp / item.name
            shutil.copytree(item, dest) if item.is_dir() else shutil.copy2(item, dest)
        subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        subprocess.run(["git", "commit", "-q", "-m", f"Export {stamp}"],
                       cwd=tmp, check=True)
        subprocess.run(["git", "push", "-f", "origin", f"HEAD:{branch}"],
                       cwd=tmp, check=True)
    except subprocess.CalledProcessError as e:
        print(f"publish failed: {e}", file=sys.stderr)
        return 1
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(tmp)],
                       cwd=ROOT, capture_output=True)
    print(f"pushed site/ to origin/{branch}")
    print("GitHub Pages must be pointed at that branch once, under "
          "Settings -> Pages -> Source")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DEFAULT_SQLITE)
    ap.add_argument("--serve", action="store_true", help="serve site/ on :8766")
    ap.add_argument("--publish", action="store_true",
                    help="force-push site/ to the gh-pages branch")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"no index at {args.db} — run an ingest first", file=sys.stderr)
        return 2

    m = build(args.db)
    print(f"site/  {m['jobs']:,} {m['scope']}  (of {m['stats']['open']:,} open "
          f"worldwide)")
    size_report()

    if args.publish:
        return publish()
    if args.serve:
        import http.server, functools  # noqa: E401
        handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                    directory=str(SITE))
        print("serving http://127.0.0.1:8766  (ctrl-c to stop)")
        try:
            http.server.ThreadingHTTPServer(("127.0.0.1", 8766), handler).serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
