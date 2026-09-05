"""Company display names.

Jobs key on `board_token`, which is not something anyone wants to read in a
results list. Names come from the curated audit first, then the discovery
sweep's board name, and finally a prettified token — Ashby's feed carries no
company name at all, so ~92 boards have nothing better available.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
AUDIT = ROOT / "data" / "step0_ats_audit.csv"
DISCOVERED = ROOT / "data" / "discovered_boards.csv"
GLOBAL = ROOT / "data" / "global_ats_audit.csv"

_SUFFIXES = re.compile(r"(careers?|jobs|hq|inc|global|group|limited|ltd)$", re.I)


def prettify(token: str) -> str:
    """'doordashaustralia' -> 'Doordashaustralia'; 'bjakcareer' -> 'Bjak';
    'relevanceai' -> 'Relevanceai'. Crude, but better than a raw slug."""
    t = re.sub(r"[-_]+", " ", token).strip()
    t = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", t)          # camelCase -> spaced
    parts = t.split()
    if len(parts) == 1:
        stripped = _SUFFIXES.sub("", parts[0])
        if len(stripped) >= 3:
            parts = [stripped]
    return " ".join(w if w.isupper() else w.capitalize() for w in parts)


def rows() -> list[tuple]:
    """-> [(vendor, token, name, domain, careers_url)] highest-quality first."""
    out: dict[tuple, tuple] = {}

    # lowest priority first, so better sources overwrite
    if DISCOVERED.exists():
        for r in csv.DictReader(DISCOVERED.open()):
            key = (r["ats_vendor"], r["board_token"])
            name = (r.get("board_name") or "").strip() or prettify(r["board_token"])
            out[key] = (*key, name, "", "")

    for audit in (GLOBAL, AUDIT):   # curated AU audit wins over the global one
        if not audit.exists():
            continue
        for r in csv.DictReader(audit.open()):
            if not r.get("board_token"):
                continue
            key = (r["ats_vendor"], r["board_token"])
            out[key] = (*key, r["company"], r.get("domain", ""), r.get("careers_url", ""))

    return list(out.values())
