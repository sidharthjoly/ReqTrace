#!/bin/sh
# What the scheduled sweep actually runs. Kept as a script rather than inlined
# into the plist so the command and the schedule can change independently, and
# so each sweep gets a timestamp — launchd's StandardOutPath appends forever
# with no marker for where one run ended and the next began.
#
#   scripts/install_autorun.py   installs the launchd agent that calls this
#   sh scripts/autorun.sh        runs one sweep by hand, same as the agent does
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$ROOT/data/logs/ingest.log"
mkdir -p "$ROOT/data/logs"

# Bound the log. A daily full sweep over 350+ boards writes a line per board,
# and nothing else would ever truncate this.
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 5242880 ]; then
    mv -f "$LOG" "$LOG.1"
fi

# launchd hands a job a near-empty PATH, so a bare `uv` is not findable. The
# installer bakes the absolute path in as UV; the fallbacks are for running
# this script from a shell.
UV="${UV:-$(command -v uv 2>/dev/null || true)}"
[ -n "$UV" ] || UV="$HOME/.local/bin/uv"
if [ ! -x "$UV" ]; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) autorun: no uv at '$UV'" >> "$LOG"
    exit 127
fi

# REQTRACE_ARGS is appended, so it can narrow the sweep for a smoke test
# (`REQTRACE_ARGS="--vendor lever --max-boards 1"`) without the plist and the
# scheduled command diverging. Unset in normal operation.
echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) sweep starting ===" >> "$LOG"
cd "$ROOT" || exit 1
# shellcheck disable=SC2086 - word splitting is the point for REQTRACE_ARGS
"$UV" run --project "$ROOT" python -m reqtrace.run --vendor all \
    ${REQTRACE_ARGS:-} >> "$LOG" 2>&1
status=$?
echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) sweep finished, exit $status ===" >> "$LOG"
exit $status
