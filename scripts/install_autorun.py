#!/usr/bin/env python3
"""Install (or remove) the launchd agent that sweeps every board daily.

Closure detection is the point of this index, and it is only as truthful as the
last run: a role that was filled yesterday still shows as open until something
diffs the board again. Until now that something was a person typing a command.

macOS launchd rather than GitHub Actions, deliberately — the README's Next list
gates Actions on a Postgres that does not exist yet, and an ephemeral runner
cannot see `data/jobs.db`. When a DATABASE_URL is provisioned, this becomes the
fallback rather than the plan.

    python scripts/install_autorun.py                # daily at 05:30 local
    python scripts/install_autorun.py --at 03:30
    python scripts/install_autorun.py --dry-run      # print the plist, touch nothing
    python scripts/install_autorun.py --run-now      # kick a sweep off immediately
    python scripts/install_autorun.py --status
    python scripts/install_autorun.py --uninstall
"""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "data" / "logs"

# Two agents, because they want opposite things from launchd. The sweep is a
# job: it runs at a time and exits, and running it more often achieves nothing
# because its tier schedule fetches only what is due. The crawler is a daemon:
# its frontier is a queue that grows as it drains, so it should be up whenever
# the machine is, and launchd should put it back if it dies.
LABEL = "com.reqtrace.ingest"
CRAWL_LABEL = "com.reqtrace.crawl"
AGENTS = Path.home() / "Library" / "LaunchAgents"
PLIST = AGENTS / f"{LABEL}.plist"
CRAWL_PLIST = AGENTS / f"{CRAWL_LABEL}.plist"

# Where uv lands when the shell that installed this is not the one launchd runs.
UV_CANDIDATES = (Path.home() / ".local/bin/uv", Path("/opt/homebrew/bin/uv"),
                 Path("/usr/local/bin/uv"))


def find_uv() -> Path:
    found = shutil.which("uv")
    if found:
        return Path(found)
    for c in UV_CANDIDATES:
        if c.is_file() and os.access(c, os.X_OK):
            return c
    sys.exit("uv not found; install it or pass --uv /path/to/uv")


def build_plist(uv: Path, hour: int, minute: int, publish: bool = False) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": ["/bin/sh", str(ROOT / "scripts" / "autorun.sh")],
        "WorkingDirectory": str(ROOT),
        # A full sweep is 350+ boards against seven vendors; daily is the most
        # it is polite to ask of them, and closure detection only claims daily
        # resolution anyway.
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        # If the Mac was asleep at the scheduled time, launchd runs the job on
        # wake. That is the behaviour we want — a missed day is a gap in the
        # first_seen_at/closed_at series that no later run can fill in.
        "RunAtLoad": False,
        "EnvironmentVariables": {
            "UV": str(uv),
            "PATH": f"{uv.parent}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(Path.home()),
            # With this set the sweep also force-pushes the static export to
            # gh-pages, so the published site tracks the index instead of
            # freezing at whenever someone last ran it by hand.
            **({"REQTRACE_PUBLISH": "1"} if publish else {}),
        },
        # The wrapper writes the real log; these catch anything that fails
        # before it gets that far, which is where launchd problems show up.
        "StandardOutPath": str(LOGS / "launchd.out"),
        "StandardErrorPath": str(LOGS / "launchd.err"),
        # A background index has no business competing with whatever the person
        # at the keyboard is doing.
        "Nice": 5,
        "LowPriorityIO": True,
        "ProcessType": "Background",
    }


def build_crawl_plist(uv: Path) -> dict:
    """The always-on crawler. Differs from the sweep's plist in three ways, and
    each one is the difference between a scheduled job and a daemon."""
    return {
        "Label": CRAWL_LABEL,
        "ProgramArguments": ["/bin/sh", str(ROOT / "scripts" / "crawl_daemon.sh")],
        "WorkingDirectory": str(ROOT),
        # Start when the agent loads and whenever the machine comes back, not
        # at a clock time. There is no moment that is the right moment to
        # discover employers.
        "RunAtLoad": True,
        # Put it back if it dies. `SuccessfulExit: False` means "restart only
        # on failure", which is what we want: a clean exit is a deliberate
        # `crawl_forever.py status`-style stop or a Ctrl-C, and respawning
        # through that would make the agent impossible to stop.
        "KeepAlive": {"SuccessfulExit": False},
        # launchd throttles respawns to once per 10s by default; a crawler that
        # is failing (no DATABASE_URL, no network) should back off further
        # rather than spin.
        "ThrottleInterval": 300,
        "EnvironmentVariables": {
            "UV": str(uv),
            "PATH": f"{uv.parent}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(Path.home()),
        },
        "StandardOutPath": str(LOGS / "crawl-launchd.out"),
        "StandardErrorPath": str(LOGS / "crawl-launchd.err"),
        # This runs for weeks alongside whatever the person at the keyboard is
        # doing, so it yields on CPU, disk and — via ProcessType Background —
        # gets deprioritised by the scheduler outright.
        "Nice": 10,
        "LowPriorityIO": True,
        "ProcessType": "Background",
    }


def launchctl(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True,
                          text=True, check=check)


def domain() -> str:
    return f"gui/{os.getuid()}"


def uninstall(crawler: bool = False) -> int:
    label, plist = (CRAWL_LABEL, CRAWL_PLIST) if crawler else (LABEL, PLIST)
    r = launchctl("bootout", f"{domain()}/{label}")
    # 3 / "No such process" is what an agent that was never loaded returns.
    if r.returncode and "No such process" not in (r.stderr + r.stdout):
        print(r.stderr.strip() or r.stdout.strip(), file=sys.stderr)
    if plist.exists():
        plist.unlink()
        print(f"removed {plist}")
    else:
        print(f"no plist at {plist}")
    if crawler:
        # The frontier is in Postgres, so stopping the local agent loses
        # nothing: the queue, the seen-set and the findings all stay put and
        # the GitHub crawl carries on from the same rows.
        print("the local crawler is gone; the frontier in Postgres is untouched")
    else:
        print("the schedule is gone; data/jobs.db and its history are untouched")
    return 0


def status(crawler: bool = False) -> int:
    label, plist = (CRAWL_LABEL, CRAWL_PLIST) if crawler else (LABEL, PLIST)
    print(f"plist:  {plist}{'' if plist.exists() else '  (not installed)'}")
    if plist.exists() and not crawler:
        env = plistlib.loads(plist.read_bytes()).get("EnvironmentVariables", {})
        cal = plistlib.loads(plist.read_bytes()).get("StartCalendarInterval", {})
        print(f"sweeps: daily at {cal.get('Hour', 0):02d}:{cal.get('Minute', 0):02d}"
              f"   publish: {'on' if env.get('REQTRACE_PUBLISH') == '1' else 'off'}")
    elif plist.exists():
        print("crawls: continuously, restarted on failure")
    r = launchctl("print", f"{domain()}/{label}")
    if r.returncode:
        print("launchd: not loaded")
        return 1
    # Only the top-level keys. `launchctl print` nests a "state = active" per
    # endpoint, which otherwise drowns the one state that means anything.
    keep = ("state =", "runs =", "last exit code", "program =", "next fire")
    for line in r.stdout.splitlines():
        if line.startswith("\t") and not line.startswith("\t\t") \
                and any(k in line for k in keep):
            print("launchd: " + line.strip())
    log = LOGS / ("crawl.log" if crawler else "ingest.log")
    if log.exists():
        tail = log.read_text(errors="replace").splitlines()[-3:]
        print("log:", *(f"\n  {t}" for t in tail))
    return 0


def install_crawler(args) -> int:
    """Install the always-on crawler agent.

    Refuses without a DATABASE_URL, and the reason is not caution for its own
    sake: `Frontier.claim` marks rows so two crawlers against one queue take
    different work, which is what lets this agent and the GitHub Actions crawl
    run simultaneously. On SQLite the local agent would build a private
    frontier instead, re-crawl what CI already fetched, and adopt boards into a
    database nothing publishes from.
    """
    dsn = os.environ.get("DATABASE_URL")
    if not dsn and (ROOT / ".env.local").exists():
        for line in (ROOT / ".env.local").read_text().splitlines():
            if line.startswith("DATABASE_URL="):
                # .strip(quotes) because dotenv files conventionally
                # quote values and a quoted DSN is not a URL any driver
                # can parse -- psycopg rejects it with an error that
                # quotes the whole string, password included.
                dsn = line.split("=", 1)[1].strip().strip("\"'")
                break
    if not dsn:
        sys.exit(
            "no DATABASE_URL, and the local crawler needs the same database as "
            "the GitHub one.\nThe frontier IS the database: with SQLite this "
            "agent would build a second, private\nqueue and re-crawl "
            "everything CI has already done. Put it in .env.local or export it.")

    plist = build_crawl_plist(args.uv or find_uv())
    if args.dry_run:
        sys.stdout.write(plistlib.dumps(plist).decode())
        return 0

    LOGS.mkdir(parents=True, exist_ok=True)
    CRAWL_PLIST.parent.mkdir(parents=True, exist_ok=True)
    CRAWL_PLIST.write_bytes(plistlib.dumps(plist))

    launchctl("bootout", f"{domain()}/{CRAWL_LABEL}")
    r = launchctl("bootstrap", domain(), str(CRAWL_PLIST))
    if r.returncode:
        print(r.stderr.strip() or r.stdout.strip(), file=sys.stderr)
        return 1

    print(f"installed {CRAWL_PLIST}")
    print("crawls continuously while this Mac is awake, restarted on failure")
    print("shares the frontier with the GitHub crawl - neither repeats the "
          "other's work")
    print(f"log: {LOGS / 'crawl.log'}")
    print("status:  python scripts/install_autorun.py --crawler --status")
    print("remove:  python scripts/install_autorun.py --crawler --uninstall")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", default="05:30", metavar="HH:MM",
                    help="local time to sweep daily (default 05:30)")
    ap.add_argument("--uv", type=Path, help="path to the uv binary")
    ap.add_argument("--publish", action=argparse.BooleanOptionalAction, default=None,
                    help="also push the static export to gh-pages after each "
                         "sweep (default: keep whatever the installed plist has)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--run-now", action="store_true",
                    help="start a sweep now as well as scheduling it")
    ap.add_argument("--crawler", action="store_true",
                    help="act on the always-on discovery crawler instead of "
                         "the sweep: a separate agent that runs continuously")
    args = ap.parse_args()

    if args.uninstall:
        return uninstall(args.crawler)
    if args.status:
        return status(args.crawler)

    if sys.platform != "darwin":
        sys.exit("launchd is macOS-only; on Linux use systemd or cron to run "
                 "scripts/autorun.sh daily")

    if args.crawler:
        return install_crawler(args)

    try:
        hour, minute = (int(x) for x in args.at.split(":", 1))
        assert 0 <= hour < 24 and 0 <= minute < 60
    except (ValueError, AssertionError):
        sys.exit(f"--at wants HH:MM in 24-hour local time, got {args.at!r}")

    # Reinstalling to change the time should not silently switch publishing off,
    # so an unspecified --publish inherits what the installed plist already says.
    publish = args.publish
    if publish is None:
        try:
            publish = plistlib.loads(PLIST.read_bytes()).get(
                "EnvironmentVariables", {}).get("REQTRACE_PUBLISH") == "1"
        except (OSError, ValueError):
            publish = False
    plist = build_plist(args.uv or find_uv(), hour, minute, publish)
    if args.dry_run:
        sys.stdout.write(plistlib.dumps(plist).decode())
        return 0

    LOGS.mkdir(parents=True, exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_bytes(plistlib.dumps(plist))

    # bootout first: bootstrap refuses a label that is already loaded, so
    # re-running this to change the time would otherwise be a no-op.
    launchctl("bootout", f"{domain()}/{LABEL}")
    r = launchctl("bootstrap", domain(), str(PLIST))
    if r.returncode:
        print(r.stderr.strip() or r.stdout.strip(), file=sys.stderr)
        return 1

    print(f"installed {PLIST}")
    print(f"sweeps every board daily at {hour:02d}:{minute:02d} local")
    print("publishes the static export to gh-pages after each sweep"
          if publish else
          "does not publish; pass --publish to push the export to gh-pages too")
    print(f"log: {LOGS / 'ingest.log'}")
    print("status:  python scripts/install_autorun.py --status")
    print("remove:  python scripts/install_autorun.py --uninstall")

    if args.run_now:
        r = launchctl("kickstart", "-p", f"{domain()}/{LABEL}")
        print("kickstarted a sweep" if not r.returncode
              else (r.stderr.strip() or "kickstart failed"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
