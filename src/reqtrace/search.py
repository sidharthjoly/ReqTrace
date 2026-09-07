"""Search over the index.

Postgres is the target and `schema_postgres.sql` already carries the weighted
tsvector and pg_trgm indexes. On the SQLite dev path the equivalent is FTS5,
which is what runs today. Either way the query surface is the same: full text
over title + body + company, with filters for the things a job hunter actually
narrows on — city, remote type, salary, freshness.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS jobs_fts USING fts5(
    title, body, company,
    vendor UNINDEXED, token UNINDEXED, ext UNINDEXED,
    tokenize = 'porter unicode61'
)
"""

SELECT_COLS = """
    j.ats_vendor, j.board_token, j.external_id, j.title,
    COALESCE(c.name, j.board_token) AS company,
    j.location_city, j.location_country, j.location_raw, j.remote_type,
    j.salary_min, j.salary_max, j.salary_currency, j.salary_period,
    j.department, j.employment_type, j.seniority, j.apply_url,
    j.posted_at, j.first_seen_at, j.last_seen_at, j.closed_at,
    SUBSTR(COALESCE(j.description_text, ''), 1, 320) AS snippet
"""

# Unambiguous data/ML titles.
DATA_TERMS = (
    "data scien", "data engineer", "data analy", "machine learn", "analytics",
    "research scien", "decision scien", "quantitat", "statistic",
    "business intelligence", " ml ", " ai ", "econometric",
)

# "Analyst" alone is far too broad — the index is full of Tax, Inventory, Cyber
# Security and Payroll Analysts, which are not what a data hunter is looking
# for. Bare "analyst" therefore only counts when it is NOT one of these.
ANALYST_EXCLUDE = (
    "tax", "inventory", "cyber", "security", "payroll", "credit control",
    "accounts payable", "accounts receivable", "procurement", "legal",
    "compliance", "claims", "treasury", "audit", "policy", "hr ", "people ",
    "desktop", "helpdesk", "service desk", "network", "soc ", "fraud",
    # finance-operations analysts, which a bank board is full of
    "loan", "mortgage", "collections", "underwrit", "settlements",
    "reconciliation", "accounts ", "billing", "invoice", "kyc",
)


@dataclass
class Query:
    q: str = ""
    city: str = ""
    country: str = "AU"        # user zero is in Sydney; AU is the useful default
    remote: str = ""
    vendor: str = ""
    company: str = ""
    data_only: bool = False
    has_salary: bool = False
    days: int = 0              # only roles first seen in the last N days
    # The dial brushes a span of weeks, which is a range and not an age: an
    # absolute window is the only version that still means the same thing when
    # the static export is read three days after it was built.
    since: str = ""            # posted on or after this ISO date
    until: str = ""            # posted strictly before this ISO date
    include_closed: bool = False
    sort: str = "newest"       # newest | relevance | salary
    limit: int = 50
    offset: int = 0


@dataclass
class Results:
    total: int = 0
    rows: list[dict] = field(default_factory=list)
    facets: dict = field(default_factory=dict)


def reindex(conn) -> int:
    """Rebuild the FTS table from jobs. Cheap at this size; run it after ingest."""
    cur = conn.cursor()
    cur.execute(FTS_DDL)
    cur.execute("DELETE FROM jobs_fts")
    cur.execute("""
        INSERT INTO jobs_fts (title, body, company, vendor, token, ext)
        SELECT j.title, COALESCE(j.description_text, ''),
               COALESCE(c.name, j.board_token),
               j.ats_vendor, j.board_token, j.external_id
        FROM jobs j
        LEFT JOIN companies c
          ON c.ats_vendor = j.ats_vendor AND c.board_token = j.board_token
    """)
    conn.commit()
    return cur.execute("SELECT count(*) FROM jobs_fts").fetchone()[0]


# A term that cannot appear in any document. Returned when the user typed
# something real that survives no tokenising ("+++"), so the query yields zero
# rows rather than silently falling back to "no search at all" and reporting the
# entire filtered set as matches.
NO_MATCH = '"zzqxnomatchzzqx"'

_FTS_META = re.compile(r'["*()^:]')


def _fts_expression(q: str) -> str:
    """Turn user input into an FTS5 expression. Bare words are ANDed and
    prefix-matched so 'data scien' finds 'data scientist'; quoted phrases are
    passed through.

    Terms are quoted rather than filtered out, so 'C++', 'R&D' and 'ML/AI' still
    search — FTS5's tokeniser strips the punctuation itself. Dropping them, as
    an earlier version did, made the query silently match everything."""
    q = q.strip()
    if not q:
        return ""
    if '"' in q:
        return q
    safe = []
    for term in q.split():
        cleaned = _FTS_META.sub("", term).strip()
        alnum = [ch for ch in cleaned if ch.isalnum()]
        if not alnum:
            continue
        # Prefix-match only terms long enough for it to mean something. "C++"
        # tokenises down to "c", and `"c"*` prefix-matches nearly every document
        # in the index — the user would see the entire result set back.
        star = "*" if len(alnum) >= 3 else ""
        safe.append(f'"{cleaned}"{star}')
    return " AND ".join(safe) if safe else NO_MATCH


def _filters(qy: Query, alias="j", ph="?"):
    """Shared WHERE builder. `ph` is the placeholder style: the export can run
    this against Postgres, where `?` is a syntax error."""
    where, params = [], []
    if not qy.include_closed:
        where.append(f"{alias}.closed_at IS NULL")
    if qy.country:
        where.append(f"{alias}.location_country = {ph}")
        params.append(qy.country)
    if qy.city:
        where.append(f"{alias}.location_city = {ph}")
        params.append(qy.city)
    if qy.remote:
        where.append(f"{alias}.remote_type = {ph}")
        params.append(qy.remote)
    if qy.vendor:
        where.append(f"{alias}.ats_vendor = {ph}")
        params.append(qy.vendor)
    if qy.company:
        where.append(f"COALESCE(c.name, {alias}.board_token) = {ph}")
        params.append(qy.company)
    if qy.has_salary:
        where.append(f"{alias}.salary_min IS NOT NULL")
    if qy.days:
        where.append(
            f"COALESCE({alias}.posted_at, {alias}.first_seen_at) >= datetime('now', {ph})")
        params.append(f"-{int(qy.days)} days")
    # Plain string comparison against an ISO date, so the same clause runs on
    # both backends and needs no date arithmetic. `posted_at` carries a time and
    # sometimes an offset, so `until` is exclusive against the next week's
    # midnight rather than inclusive against this one's.
    if qy.since:
        where.append(f"COALESCE({alias}.posted_at, {alias}.first_seen_at) >= {ph}")
        params.append(qy.since)
    if qy.until:
        where.append(f"COALESCE({alias}.posted_at, {alias}.first_seen_at) < {ph}")
        params.append(qy.until)
    if qy.data_only:
        t = f"LOWER(' '||{alias}.title||' ')"
        strong = " OR ".join([f"{t} LIKE {ph}"] * len(DATA_TERMS))
        not_excluded = " AND ".join([f"{t} NOT LIKE {ph}"] * len(ANALYST_EXCLUDE))
        where.append(f"(({strong}) OR ({t} LIKE {ph} AND {not_excluded}))")
        params += [f"%{x}%" for x in DATA_TERMS]
        params += ["%analyst%"] + [f"%{x}%" for x in ANALYST_EXCLUDE]
    return where, params


ORDERS = {
    # The vendor's publish date is what a job hunter means by "newest";
    # first_seen_at only says when this index noticed it.
    "newest": "COALESCE(j.posted_at, j.first_seen_at) DESC",
    "salary": "j.salary_max DESC NULLS LAST, j.first_seen_at DESC",
    "relevance": "rank",
}


def search(conn, qy: Query) -> Results:
    conn.row_factory = sqlite3.Row
    expr = _fts_expression(qy.q)
    where, params = _filters(qy)

    join = ("LEFT JOIN companies c ON c.ats_vendor = j.ats_vendor "
            "AND c.board_token = j.board_token")
    if expr:
        base = (f"FROM jobs_fts f JOIN jobs j ON j.ats_vendor = f.vendor "
                f"AND j.board_token = f.token AND j.external_id = f.ext {join} "
                f"WHERE jobs_fts MATCH ?")
        params = [expr] + params
    else:
        base = f"FROM jobs j {join} WHERE 1=1"

    if where:
        base += " AND " + " AND ".join(where)

    total = conn.execute(f"SELECT count(*) {base}", params).fetchone()[0]

    order = ORDERS.get(qy.sort, ORDERS["newest"])
    if order == "rank" and not expr:
        order = ORDERS["newest"]
    rows = conn.execute(
        f"SELECT {SELECT_COLS} {base} ORDER BY {order} LIMIT ? OFFSET ?",
        [*params, qy.limit, qy.offset],
    ).fetchall()

    # Only on the first page: the rail is already drawn by the time anything
    # pages, and these are four GROUP BYs carrying the whole data-role filter.
    return Results(total=total, rows=[dict(r) for r in rows],
                   facets=facets(conn, qy) if not qy.offset else {})


def facets(conn, qy: Query) -> dict:
    """Counts for the filter rail: each city's share of the page's own scope.

    Scope is the country and the data-role filter — deliberately not the city,
    work type or query currently chosen. The rail is a map of where the roles
    are, and a selected city that zeroed every other city's count would stop
    being a map the moment it became useful.
    """
    conn.row_factory = sqlite3.Row
    where, p = _filters(Query(country=qy.country, data_only=qy.data_only))
    scope = "WHERE " + " AND ".join(where)

    def group(col, limit=12):
        return [dict(r) for r in conn.execute(
            f"SELECT {col} AS key, count(*) AS n FROM jobs j {scope} "
            f"AND {col} IS NOT NULL AND {col} != '' "
            f"GROUP BY 1 ORDER BY n DESC LIMIT {limit}", p).fetchall()]

    companies = [dict(r) for r in conn.execute(
        f"SELECT COALESCE(c.name, j.board_token) AS key, count(*) AS n FROM jobs j "
        f"LEFT JOIN companies c ON c.ats_vendor = j.ats_vendor "
        f"AND c.board_token = j.board_token {scope} GROUP BY 1 ORDER BY n DESC LIMIT 15",
        p).fetchall()]

    return {
        "cities": group("j.location_city", 30),
        "remote": group("j.remote_type", 6),
        "vendors": group("j.ats_vendor", 6),
        "companies": companies,
    }


# The export ships one of these per role in place of a description. The page
# that reads it stopped rendering excerpts when the row became a single line, so
# the field only has to *match* now, never to read — which frees it to be the
# role's vocabulary rather than the first 320 characters of its prose.
#
# A prefix was the worst possible selection: job ads open with boilerplate and
# name their tools at the end, so "pytorch" and "causal" matched nothing at all
# on the published site while matching 22 and 15 roles in the index.
KW_MAX_DF = 0.04
# A floor under the proportional cut. On a small corpus 4% rounds below one
# document and the filter eats every token it is handed — which is silent, and
# leaves a search index that matches nothing.
KW_MIN_DF = 5
_KW_WORD = re.compile(r"[a-z0-9][a-z0-9+.#/-]{1,}")


def keywords(bodies, max_df: float = KW_MAX_DF) -> list[str]:
    """One space-joined, deduplicated token blob per body.

    Tokens carried by more than `max_df` of the corpus are dropped. They are the
    ones that cannot narrow anything — "experience", "team", "role", "working"
    are in nearly every ad — and they are most of the bytes. Pruning at 4% holds
    full recall for the terms a hunter actually types (a skill in 4% of 3,400
    roles is still 137 of them) while cutting the payload by two thirds.

    Rare tokens are kept deliberately: a word in three ads is precisely the one
    worth searching for.
    """
    from collections import Counter

    sets = [{w for w in _KW_WORD.findall((b or "").lower())
             if len(w) > 2 and not w.isdigit()} for b in bodies]
    df = Counter()
    for s in sets:
        df.update(s)
    cap = max(max_df * len(sets), KW_MIN_DF)
    keep = {w for w, n in df.items() if n <= cap}
    return [" ".join(sorted(s & keep)) for s in sets]


PULSE_WEEKS = 16


def _week_starts(n: int = PULSE_WEEKS, today: date | None = None) -> list[str]:
    """The Mondays of the last `n` weeks, oldest first, as ISO dates.

    Absolute dates rather than "weeks ago": the static export is read for days
    after it is built, and a window measured from the reader's clock would slide
    off the bars it was drawn against."""
    today = today or datetime.now(timezone.utc).date()
    monday = today - timedelta(days=today.weekday())
    return [(monday - timedelta(weeks=n - 1 - i)).isoformat() for i in range(n)]


def _day(v) -> str:
    """The YYYY-MM-DD head of a timestamp. SQLite stores strings and Postgres
    hands back datetimes; both agree on the first ten characters."""
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)[:10]


def pulse(conn, qy: Query | None = None, weeks: int = PULSE_WEEKS,
          backend: str = "sqlite", today: date | None = None) -> dict:
    """Weekly openings and closures at role level, oldest week first.

    Role level from `posted_at` / `closed_at`, deliberately not `board_runs`:
    the run log counts a board's first sight as new, so on the week the index
    booted every role it has ever seen would read as an opening.

    Both ends of the series are bounded by what the index has actually looked
    at, and both bounds ship with it. `closed_at` records when *this* index
    noticed a role gone, so closures cannot predate the first sweep
    (`closures_since`). Nothing at all is known about a week that began after
    the last sweep (`observed_to`). Weeks outside those bounds are unobserved,
    not quiet, and the page has to say so rather than draw confident zeroes.
    """
    qy = qy or Query(data_only=True)
    ph = "%s" if backend == "postgres" else "?"
    starts = _week_starts(weeks, today)
    edge = (date.fromisoformat(starts[-1]) + timedelta(days=7)).isoformat()

    # include_closed: a role that closed inside the window is exactly what the
    # lower half of the chart is counting.
    scope = Query(country=qy.country, data_only=qy.data_only, include_closed=True)
    where, params = _filters(scope, ph=ph)

    # Bucketed in Python rather than in SQL: julianday() is SQLite-only and
    # date_trunc() is Postgres-only, and the scope is a few thousand rows.
    rows = conn.execute(
        f"SELECT COALESCE(j.posted_at, j.first_seen_at), j.closed_at "
        f"FROM jobs j WHERE {' AND '.join(where)}", params).fetchall()

    opened = [0] * weeks
    closed = [0] * weeks

    def bucket(day: str) -> int | None:
        if not day or day < starts[0] or day >= edge:
            return None
        lo, hi = 0, weeks - 1
        while lo < hi:                       # the last start not after `day`
            mid = (lo + hi + 1) // 2
            if starts[mid] <= day:
                lo = mid
            else:
                hi = mid - 1
        return lo

    for row in rows:
        i = bucket(_day(row[0]))
        if i is not None:
            opened[i] += 1
        i = bucket(_day(row[1]))
        if i is not None:
            closed[i] += 1

    # The run log is the only record of what the index has looked at, and both
    # ends of the chart are bounded by it.
    first_run, last_run = conn.execute(
        "SELECT min(fetched_at), max(fetched_at) FROM board_runs").fetchone()

    return {
        "weeks": [{"start": s, "opened": opened[i], "closed": closed[i]}
                  for i, s in enumerate(starts)],
        "closures_since": _day(first_run) or None,
        "observed_to": _day(last_run) or None,
        "scope": "data" if qy.data_only else "all",
    }


def stats(conn) -> dict:
    q = lambda s, p=(): conn.execute(s, p).fetchone()[0]  # noqa: E731
    return {
        "jobs": q("SELECT count(*) FROM jobs"),
        "open": q("SELECT count(*) FROM jobs WHERE closed_at IS NULL"),
        "au_open": q("SELECT count(*) FROM jobs WHERE closed_at IS NULL "
                     "AND location_country='AU'"),
        "closed": q("SELECT count(*) FROM jobs WHERE closed_at IS NOT NULL"),
        "boards": q("SELECT count(DISTINCT board_token) FROM jobs"),
        "vendors": q("SELECT count(DISTINCT ats_vendor) FROM jobs"),
    }
