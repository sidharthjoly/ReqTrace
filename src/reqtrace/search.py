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


def _filters(qy: Query, alias="j"):
    where, params = [], []
    if not qy.include_closed:
        where.append(f"{alias}.closed_at IS NULL")
    if qy.country:
        where.append(f"{alias}.location_country = ?")
        params.append(qy.country)
    if qy.city:
        where.append(f"{alias}.location_city = ?")
        params.append(qy.city)
    if qy.remote:
        where.append(f"{alias}.remote_type = ?")
        params.append(qy.remote)
    if qy.vendor:
        where.append(f"{alias}.ats_vendor = ?")
        params.append(qy.vendor)
    if qy.company:
        where.append("COALESCE(c.name, j.board_token) = ?")
        params.append(qy.company)
    if qy.has_salary:
        where.append(f"{alias}.salary_min IS NOT NULL")
    if qy.days:
        where.append(
            f"COALESCE({alias}.posted_at, {alias}.first_seen_at) >= datetime('now', ?)")
        params.append(f"-{int(qy.days)} days")
    if qy.data_only:
        t = f"LOWER(' '||{alias}.title||' ')"
        strong = " OR ".join([f"{t} LIKE ?"] * len(DATA_TERMS))
        not_excluded = " AND ".join([f"{t} NOT LIKE ?"] * len(ANALYST_EXCLUDE))
        where.append(f"(({strong}) OR ({t} LIKE ? AND {not_excluded}))")
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

    return Results(total=total, rows=[dict(r) for r in rows],
                   facets=facets(conn, qy))


def facets(conn, qy: Query) -> dict:
    """Counts for the filter chips, respecting the current country filter."""
    conn.row_factory = sqlite3.Row
    scope = "WHERE j.closed_at IS NULL" + (
        " AND j.location_country = ?" if qy.country else "")
    p = [qy.country] if qy.country else []

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
        "cities": group("j.location_city"),
        "remote": group("j.remote_type", 6),
        "vendors": group("j.ats_vendor", 6),
        "companies": companies,
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
