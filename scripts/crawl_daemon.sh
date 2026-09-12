#!/bin/sh
# What the always-on crawler agent runs. The sweep's `autorun.sh`, for the
# other half of the pipeline — but where that one runs a job and exits, this
# one is meant never to return.
#
#   scripts/install_autorun.py --crawler   installs the launchd agent
#   sh scripts/crawl_daemon.sh             runs it in this terminal instead
#
# Why 24/7 makes sense here and not for the sweep: the sweep fetches only what
# its tier schedule says is due, so running it continuously finds nothing to do
# for six hours after every pass. The crawl frontier has no such ceiling — it
# is a queue that grows as it is drained, so more hours are simply more
# employers discovered.
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$ROOT/data/logs/crawl.log"
mkdir -p "$ROOT/data/logs"

# DATABASE_URL is not optional, and the reason is specific rather than general.
# The frontier IS the database: `claim` marks rows so that two crawlers against
# one queue take different work, which is exactly what lets this agent and the
# GitHub Actions crawl run at the same time without fetching the same pages
# twice. Fall back to SQLite and that stops being true — the local crawler
# would build a second, private frontier, re-crawl everything the CI runs
# already did, and adopt boards into a database nothing publishes from.
if [ -z "${DATABASE_URL:-}" ] && [ -f "$ROOT/.env.local" ]; then
    # Only this one assignment, and only into this process.
    #
    # `tr -d` strips surrounding quotes, which is not cosmetic: dotenv files
    # conventionally quote values, `cut` keeps the quotes, and a DSN of
    # `"postgres://..."` is not a malformed URL to the driver but a malformed
    # *option string* -- so psycopg rejects it with an error that quotes the
    # whole DSN, password included, straight into this log.
    DATABASE_URL="$(grep -m1 '^DATABASE_URL=' "$ROOT/.env.local" | cut -d= -f2- | tr -d '"'"'"'\047')"
    export DATABASE_URL
fi
if [ -z "${DATABASE_URL:-}" ]; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) crawl_daemon: no DATABASE_URL (set it, or put it in .env.local)" >> "$LOG"
    exit 78   # EX_CONFIG: launchd will not respawn-loop on this
fi

# Bound the log. This process runs for weeks and prints a line per lap.
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 5242880 ]; then
    mv -f "$LOG" "$LOG.1"
fi

UV="${UV:-$(command -v uv 2>/dev/null || true)}"
[ -n "$UV" ] || UV="$HOME/.local/bin/uv"
if [ ! -x "$UV" ]; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) crawl_daemon: no uv at '$UV'" >> "$LOG"
    exit 127
fi

echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) crawler starting ===" >> "$LOG"
cd "$ROOT" || exit 1

# No --laps and no --minutes: this is the mode `crawl_forever.py` was written
# for.
#
# `--rest 360` is not throttling for its own sake, it is what makes running
# this for weeks affordable. A serverless Postgres suspends its compute only
# once nothing is connected, and crawl_forever drops the connection across any
# rest over a minute — so a six-minute rest means the compute is awake for the
# ~15 seconds a lap takes and asleep the rest of the time. Held open instead,
# a 24/7 crawler spends ~180 compute-hours a month against a free tier that
# allows 191.9, and the sweep still needs its share.
#
# The rate that buys: ~20 pages every 6 minutes, so ~4,600 pages and ~2 MB of
# frontier rows a day. Discovery is measured in weeks; this is a pace it can
# hold for months without filling a 0.5 GB database.
#
# REQTRACE_CRAWL_ARGS narrows it for a smoke test without editing the plist.
# shellcheck disable=SC2086 - word splitting is the point for the args var
"$UV" run --project "$ROOT" python scripts/crawl_forever.py \
    --expand --lap-pages 20 --rest 360 \
    ${REQTRACE_CRAWL_ARGS:-} >> "$LOG" 2>&1
status=$?

echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) crawler exited, status $status ===" >> "$LOG"
exit $status
