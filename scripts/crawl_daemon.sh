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
# for. `--rest 45` is deliberately slower than the CI crawl's 20s, because this
# one has all day and nothing to prove — the politeness rules underneath
# (robots.txt with Crawl-delay, one request at a time per host, 12 pages per
# host ever) do the real work, and a longer rest simply spreads the load
# further. REQTRACE_CRAWL_ARGS narrows it for a smoke test without editing the
# plist.
# shellcheck disable=SC2086 - word splitting is the point for the args var
"$UV" run --project "$ROOT" python scripts/crawl_forever.py \
    --expand --lap-pages 20 --rest 45 \
    ${REQTRACE_CRAWL_ARGS:-} >> "$LOG" 2>&1
status=$?

echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) crawler exited, status $status ===" >> "$LOG"
exit $status
