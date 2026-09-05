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

LABEL = "com.reqtrace.ingest"
ROOT = Path(__file__).resolve().parent.parent
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOGS = ROOT / "data" / "logs"

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


def build_plist(uv: Path, hour: int, minute: int) -> dict:
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


def launchctl(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True,
                          text=True, check=check)


def domain() -> str:
    return f"gui/{os.getuid()}"


def uninstall() -> int:
    r = launchctl("bootout", f"{domain()}/{LABEL}")
    # 3 / "No such process" is what an agent that was never loaded returns.
    if r.returncode and "No such process" not in (r.stderr + r.stdout):
        print(r.stderr.strip() or r.stdout.strip(), file=sys.stderr)
    if PLIST.exists():
        PLIST.unlink()
        print(f"removed {PLIST}")
    else:
        print(f"no plist at {PLIST}")
    print("the schedule is gone; data/jobs.db and its history are untouched")
    return 0


def status() -> int:
    print(f"plist:  {PLIST}{'' if PLIST.exists() else '  (not installed)'}")
    r = launchctl("print", f"{domain()}/{LABEL}")
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
    log = LOGS / "ingest.log"
    if log.exists():
        tail = log.read_text(errors="replace").splitlines()[-3:]
        print("log:", *(f"\n  {t}" for t in tail))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", default="05:30", metavar="HH:MM",
                    help="local time to sweep daily (default 05:30)")
    ap.add_argument("--uv", type=Path, help="path to the uv binary")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--run-now", action="store_true",
                    help="start a sweep now as well as scheduling it")
    args = ap.parse_args()

    if args.uninstall:
        return uninstall()
    if args.status:
        return status()

    if sys.platform != "darwin":
        sys.exit("launchd is macOS-only; on Linux use systemd or cron to run "
                 "scripts/autorun.sh daily")

    try:
        hour, minute = (int(x) for x in args.at.split(":", 1))
        assert 0 <= hour < 24 and 0 <= minute < 60
    except (ValueError, AssertionError):
        sys.exit(f"--at wants HH:MM in 24-hour local time, got {args.at!r}")

    plist = build_plist(args.uv or find_uv(), hour, minute)
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
