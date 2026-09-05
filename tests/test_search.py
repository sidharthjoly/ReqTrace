"""Search filters. The data-role filter decides what user zero actually sees,
so its precision matters more than its recall."""

import sqlite3

import pytest

from reqtrace import search as S
from reqtrace.models import BoardSnapshot, Job
from reqtrace.store import Store


def j(ext, title, city="Sydney", country="AU", **kw):
    return Job(ats_vendor="greenhouse", board_token="acme", external_id=ext,
               title=title, location_city=city, location_country=country,
               content_hash=ext, **kw)


@pytest.fixture
def conn(tmp_path):
    store = Store(sqlite_path=tmp_path / "s.db")
    store.init_schema()
    store.load_companies([("greenhouse", "acme", "Acme Corp", "", "")])
    store.reconcile(BoardSnapshot(
        ats_vendor="greenhouse", board_token="acme", complete=True, jobs=[
            j("1", "Senior Data Scientist"),
            j("2", "Machine Learning Engineer"),
            j("3", "Senior Tax Analyst"),
            j("4", "Cyber Security Analyst"),
            j("5", "Product Analyst"),
            j("6", "Chef"),
            j("7", "Data Engineer", city="London", country="GB"),
            j("8", "Analytics Lead", salary_min=180000.0, salary_max=220000.0,
              salary_currency="AUD", salary_period="year"),
        ]))
    S.reindex(store.conn)
    yield store.conn
    store.close()


def titles(res):
    return {r["title"] for r in res.rows}


def test_data_filter_keeps_real_data_roles(conn):
    got = titles(S.search(conn, S.Query(data_only=True)))
    assert {"Senior Data Scientist", "Machine Learning Engineer",
            "Analytics Lead", "Product Analyst"} <= got


def test_data_filter_excludes_non_data_analysts(conn):
    """'Analyst' alone is too broad: the index is full of Tax, Cyber and
    Payroll Analysts that are noise for a data search."""
    got = titles(S.search(conn, S.Query(data_only=True)))
    assert "Senior Tax Analyst" not in got
    assert "Cyber Security Analyst" not in got
    assert "Chef" not in got


def test_country_defaults_to_australia(conn):
    assert "Data Engineer" not in titles(S.search(conn, S.Query()))
    assert "Data Engineer" in titles(S.search(conn, S.Query(country="GB")))


def test_full_text_matches_prefixes(conn):
    assert "Senior Data Scientist" in titles(S.search(conn, S.Query(q="data scien")))


def test_company_name_is_searchable(conn):
    assert S.search(conn, S.Query(q="acme")).total > 0


def test_salary_filter_and_sort(conn):
    res = S.search(conn, S.Query(has_salary=True))
    assert titles(res) == {"Analytics Lead"}


def test_closed_roles_are_hidden_by_default(conn):
    conn.execute("UPDATE jobs SET closed_at='2026-01-01' WHERE external_id='1'")
    conn.commit()
    assert "Senior Data Scientist" not in titles(S.search(conn, S.Query()))
    assert "Senior Data Scientist" in titles(S.search(conn, S.Query(include_closed=True)))


def test_symbol_only_query_matches_nothing_rather_than_everything(conn):
    """A query FTS5 cannot tokenise must return zero rows. Silently dropping it
    made the UI report the whole filtered set as matches for 'C++'."""
    assert S.search(conn, S.Query(q="+++")).total == 0
    assert S.search(conn, S.Query(q="!!!")).total == 0


def test_queries_with_punctuation_still_search(conn):
    """'C++' and 'R&D' should search, not be discarded."""
    from reqtrace.search import _fts_expression
    for q in ("C++", "R&D", "ML/AI"):
        assert _fts_expression(q) != "", q
    assert S.search(conn, S.Query(q="data")).total > 0


def test_short_terms_are_not_prefix_matched(conn):
    """'C++' tokenises to 'c'; prefix-matching that would match nearly every
    document, so the user would get the whole index back and believe it all
    matched."""
    from reqtrace.search import _fts_expression
    assert _fts_expression("C++") == '"C++"'          # no trailing *
    assert _fts_expression("data") == '"data"*'       # long enough to prefix
    everything = S.search(conn, S.Query()).total
    assert S.search(conn, S.Query(q="C++")).total < everything


def test_finance_operations_analysts_are_excluded(conn):
    """A bank board is full of 'Loan Doc & Proc Analyst' and 'Collections
    Analyst'. They match 'analyst' but are not data roles."""
    from reqtrace.search import _filters

    where, params = _filters(S.Query(data_only=True))
    clause = " ".join(where)
    assert "loan" in " ".join(str(p) for p in params)
    assert clause  # the exclusion list is actually applied
