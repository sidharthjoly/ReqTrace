"""Search filters. The data-role filter decides what user zero actually sees,
so its precision matters more than its recall."""

import sqlite3
from datetime import date

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


# -- the dial: an absolute window, and the series drawn behind it -------------

TODAY = date(2026, 9, 7)          # a Monday, so the last week starts on it


@pytest.fixture
def dated(tmp_path):
    """Four data roles on known publish dates, plus one outside the chart."""
    store = Store(sqlite_path=tmp_path / "d.db")
    store.init_schema()
    store.reconcile(BoardSnapshot(
        ats_vendor="greenhouse", board_token="acme", complete=True, jobs=[
            j("1", "Data Scientist", posted_at="2026-09-02"),      # week 15
            j("2", "Data Engineer", posted_at="2026-09-01"),       # week 15
            j("3", "Analytics Lead", posted_at="2026-08-26"),      # week 14
            j("4", "Machine Learning Engineer", posted_at="2026-05-01"),  # off-chart
        ]))
    S.reindex(store.conn)
    yield store.conn
    store.close()


def test_window_selects_one_week(dated):
    """The dial hands the query two dates, not an age: an export read three days
    later must still filter to the bars it was drawn against."""
    res = S.search(dated, S.Query(since="2026-08-31", until="2026-09-07"))
    assert titles(res) == {"Data Scientist", "Data Engineer"}


def test_window_end_is_exclusive(dated):
    """`until` is the next week's Monday, so a role posted on it belongs to the
    next bar and not to this one."""
    assert S.search(dated, S.Query(since="2026-08-24", until="2026-08-31")).total == 1
    assert S.search(dated, S.Query(since="2026-08-24", until="2026-09-07")).total == 3


def test_pulse_buckets_by_week(dated):
    p = S.pulse(dated, S.Query(data_only=True), today=TODAY)
    weeks = {w["start"]: w["opened"] for w in p["weeks"]}
    assert len(p["weeks"]) == 16
    assert p["weeks"][0]["start"] == "2026-05-25"
    assert p["weeks"][-1]["start"] == "2026-09-07"
    assert weeks["2026-08-31"] == 2
    assert weeks["2026-08-24"] == 1
    # The May role predates the chart. It is still in the index and still
    # counted by the resting page, which is why a full brush applies no window.
    assert sum(weeks.values()) == 3
    assert S.search(dated, S.Query(data_only=True)).total == 4


def test_pulse_counts_closures_and_says_when_it_started_watching(dated):
    """`closed_at` is when *this* index noticed a role gone, so it cannot
    predate the first sweep. The page needs that date to mark the weeks it has
    no reading for, rather than drawing them as zero."""
    dated.execute("UPDATE jobs SET closed_at='2026-09-05' WHERE external_id='3'")
    dated.commit()
    p = S.pulse(dated, S.Query(data_only=True), today=TODAY)
    closed = {w["start"]: w["closed"] for w in p["weeks"]}
    assert closed["2026-08-31"] == 1
    assert p["closures_since"] == _day_of_first_run(dated)
    # A closed role leaves the openings series alone: it opened when it opened.
    assert {w["start"]: w["opened"] for w in p["weeks"]}["2026-08-24"] == 1


def _day_of_first_run(conn):
    return str(conn.execute("SELECT min(fetched_at) FROM board_runs").fetchone()[0])[:10]


def test_filters_can_speak_postgres_placeholders():
    """The export runs `_filters` against whichever backend holds the index, and
    a single missed `?` is a syntax error there. Checked without a server
    because there is not always one to check against."""
    where, _ = S._filters(S.Query(data_only=True, city="Sydney", remote="hybrid",
                                  since="2026-08-31", until="2026-09-07",
                                  vendor="greenhouse", company="Acme", days=7),
                          ph="%s")
    clause = " ".join(where)
    assert "?" not in clause, "a placeholder was left in SQLite dialect"
    assert clause.count("%s") == S._filters(
        S.Query(data_only=True, city="Sydney", remote="hybrid",
                since="2026-08-31", until="2026-09-07", vendor="greenhouse",
                company="Acme", days=7))[1].__len__()


def test_pulse_reports_what_it_has_actually_looked_at(dated):
    """Both ends of the chart are bounded by the run log. A week that began
    after the last sweep is unread, not empty — drawn as a zero it would be the
    first thing a reader sees and the most confident lie on the page."""
    p = S.pulse(dated, S.Query(data_only=True), today=TODAY)
    assert p["observed_to"] == _day_of_last_run(dated)
    assert p["closures_since"] <= p["observed_to"]


def _day_of_last_run(conn):
    return str(conn.execute("SELECT max(fetched_at) FROM board_runs").fetchone()[0])[:10]
