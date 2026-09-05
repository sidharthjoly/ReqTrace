"""Crawler tests, entirely offline.

Same split as the seven adapters: the parts that decide anything are pure
functions over recorded input, and the engine is exercised against a mock
transport rather than the live web. Fixtures under `fixtures/careers/` are the
embed shapes real careers pages use — the ones a URL index cannot resolve,
which is the whole reason this crawler exists.
"""

import asyncio
from pathlib import Path

import httpx
import pytest

from reqtrace.crawl import (
    STRONG, Crawler, canonicalise, extract_links, find_boards, plausible_token,
    registrable, same_site, score_link, sitemap_urls,
)

PAGES = Path(__file__).resolve().parent.parent / "fixtures" / "careers"


def page(name: str) -> str:
    return (PAGES / f"{name}.html").read_text()


# ---------------------------------------------------------------------------
# canonicalisation — two URLs that differ only by tracking junk are one page
# ---------------------------------------------------------------------------

def test_canonicalise_collapses_the_same_page():
    forms = [
        "https://Acme.com:443/careers/?utm_source=nav&utm_campaign=x#roles",
        "https://acme.com/careers",
        "https://acme.com/careers/#top",
        "https://acme.com/careers?gclid=abc",
    ]
    assert len({canonicalise(u) for u in forms}) == 1


def test_canonicalise_keeps_meaningful_query_params():
    assert canonicalise("https://acme.com/jobs?dept=data&utm_medium=x") == \
        "https://acme.com/jobs?dept=data"


def test_canonicalise_rejects_what_is_not_a_page():
    for href in ("mailto:hi@acme.com", "tel:+61", "javascript:void(0)", "#top",
                 "/brochure.pdf", "https://acme.com/logo.svg", ""):
        assert canonicalise(href, "https://acme.com/") is None


def test_canonicalise_resolves_relative_links():
    assert canonicalise("../jobs", "https://acme.com/about/careers") == \
        "https://acme.com/jobs"


def test_registrable_domain_handles_australian_suffixes():
    assert registrable("https://careers.westpac.com.au/search") == "westpac.com.au"
    assert registrable("www.canva.com") == "canva.com"
    # A careers page usually lives on a sibling host, so the crawl boundary has
    # to be the registrable domain or it stops one link short of the board.
    assert same_site("https://jobs.acme.com/x", "https://www.acme.com/")
    assert not same_site("https://twitter.com/acme", "https://acme.com/")


# ---------------------------------------------------------------------------
# link scoring — the "focused" in focused crawler
# ---------------------------------------------------------------------------

def test_careers_link_outranks_everything_else_on_a_homepage():
    links = extract_links(page("homepage"), "https://acme.com/")
    scored = sorted(((score_link(u, a, origin="https://acme.com/"), u) for u, a in links),
                    reverse=True)
    assert scored[0][1] == "https://acme.com/about/careers"
    assert scored[0][0] >= STRONG


def test_scoring_drops_the_sinks():
    o = "https://acme.com/"
    # A blog post about hiring is not a careers page.
    assert score_link("https://acme.com/blog/2024/why-we-are-hiring", "We're hiring", origin=o) == 0
    assert score_link("https://acme.com/pricing", "Pricing", origin=o) == 0
    assert score_link("https://acme.com/legal/privacy", "Privacy", origin=o) == 0
    # Off-site links are read by the fingerprints, never followed.
    assert score_link("https://twitter.com/acme", "Follow us", origin=o) == 0


def test_extraction_normalises_and_drops_non_pages():
    urls = {u for u, _ in extract_links(page("homepage"), "https://acme.com/")}
    assert "https://acme.com/join-us" in urls        # utm + fragment stripped
    assert not any(u.endswith(".pdf") for u in urls)
    assert not any(u.startswith("mailto") for u in urls)


def test_sitemap_locs():
    xml = """<urlset><url><loc>https://acme.com/careers</loc></url>
             <url><loc>https://acme.com/blog/1</loc></url></urlset>"""
    assert sitemap_urls(xml) == ["https://acme.com/careers", "https://acme.com/blog/1"]


# ---------------------------------------------------------------------------
# fingerprints — the payload, one test per shape Common Crawl cannot see
# ---------------------------------------------------------------------------

def test_greenhouse_embed_token_comes_from_the_query_string():
    """The whole point. `boards.greenhouse.io/embed/job_board/js?for=acmelabs`
    has no `/token` path segment, so a URL-index regex reads `embed` and loses
    the board. Ordering the embed pattern first is what fixes it."""
    hits = find_boards(page("greenhouse_embed"), url="https://acme.com/careers")
    assert ("greenhouse", "acmelabs") in hits
    assert not any(t == "embed" for _, t in hits)


def test_lever_embed_token_keeps_its_case():
    """jobs.lever.co/Zeller resolves and /zeller 404s — nothing may lowercase these."""
    hits = find_boards(page("lever_embed"), url="https://zeller.com/careers")
    assert ("lever", "Zeller") in hits
    assert ("lever", "zeller") not in hits


def test_ashby_and_smartrecruiters_embeds():
    assert ("ashby", "lorikeet") in find_boards(page("ashby_embed"))
    hits = find_boards(page("smartrecruiters_link"))
    assert ("smartrecruiters", "Canva") in hits
    assert not any(t in ("assets", "images") for _, t in hits)


def test_workday_composite_identity():
    """`board_token` is tenant + wdN + site, which is why adapters/workday.py
    says these come from careers-page crawling and never from slug guessing."""
    tokens = {t for v, t in find_boards(page("workday_link")) if v == "workday"}
    assert tokens == {"cba.wd3/CommBank_Careers", "acme.wd5/External"}


def test_oracle_composite_identity():
    assert ("oracle", "ebuu.fa.ap1.oraclecloud.com/CX_1") in find_boards(page("oracle_link"))


def test_eightfold_token_needs_the_employer_domain():
    """Eightfold identity is `tenant/domain`. A URL index never knows the
    employer's domain; a crawl always does, because it arrived from that site."""
    assert ("eightfold", "citi/citi.com") in \
        find_boards(page("eightfold_link"), domain="citi.com")
    assert find_boards(page("eightfold_link"), domain="") == []


def test_unadapted_suites_are_recorded_as_intelligence():
    """NAB on Avature, Macquarie on SuccessFactors — Step 0 left these
    unresolved. Knowing the suite is the precondition for the adapter."""
    hits = dict(find_boards(page("enterprise_suites")))
    assert hits["avature"] == "nab"
    assert hits["successfactors"] == "macquarieP"
    assert hits["icims"] == "example"


def test_token_plausibility():
    assert plausible_token("greenhouse", "acmelabs")
    assert not plausible_token("greenhouse", "embed")
    assert not plausible_token("greenhouse", "1234")
    assert not plausible_token("greenhouse", "a3f9c1d7b2e4f8a6")
    assert not plausible_token("greenhouse", "bundle.js")
    # Composite identities are checked by shape, not by the slug blocklist.
    assert plausible_token("workday", "cba.wd3/CommBank_Careers")
    assert not plausible_token("workday", "cba.wd3")


# ---------------------------------------------------------------------------
# the engine
# ---------------------------------------------------------------------------

def crawl(routes: dict[str, tuple[int, str, str]], **kw) -> Crawler:
    """Run a crawl against a fixed site. routes: path -> (status, ctype, body)."""
    def handler(request: httpx.Request) -> httpx.Response:
        status, ctype, body = routes.get(request.url.path, (404, "text/html", ""))
        return httpx.Response(status, headers={"content-type": ctype}, text=body)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = Crawler(client, delay=0, use_sitemaps=kw.pop("use_sitemaps", False), **kw)
            c.seed_from("acme.com")
            await c.run()
            return c
    return asyncio.run(go())


HTML = "text/html; charset=utf-8"


def test_crawler_walks_a_homepage_to_the_board_and_stops_there():
    c = crawl({
        "/": (200, HTML, page("homepage")),
        "/about/careers": (200, HTML, page("greenhouse_embed")),
        "/join-us": (200, HTML, "<html><a href='/nothing'>x</a></html>"),
    }, max_pages=20)
    assert [(f.vendor, f.token) for f in c.findings] == [("greenhouse", "acmelabs")]
    assert c.findings[0].seed == "acme.com"
    # A host is done the moment its board is found: the crawl never reached
    # /join-us, even though it was in the frontier and scored well.
    assert c.hosts["acme.com"].exhausted
    assert c.pages == 2


def test_robots_disallow_is_obeyed_even_when_the_board_is_behind_it():
    c = crawl({
        "/robots.txt": (200, "text/plain", "User-agent: *\nDisallow: /about/\n"),
        "/": (200, HTML, page("homepage")),
        "/about/careers": (200, HTML, page("greenhouse_embed")),
    }, max_pages=20)
    assert c.findings == []
    assert c.stats["robots_disallowed"] >= 1


def test_well_known_paths_are_probed_when_a_homepage_hides_its_careers_link():
    """JS-rendered sites give up no links at all. The conventions are the
    fallback — and they are conventions, not guesses at a private URL."""
    c = crawl({
        "/": (200, HTML, "<html><body><a href='/pricing'>Pricing</a></body></html>"),
        "/careers": (200, HTML, page("lever_embed")),
    }, max_pages=20)
    assert [(f.vendor, f.token) for f in c.findings] == [("lever", "Zeller")]


def test_the_page_budget_is_a_hard_stop():
    big = "".join(f"<a href='/careers/team-{i}'>Careers team {i}</a>" for i in range(50))
    c = crawl({"/": (200, HTML, big),
               **{f"/careers/team-{i}": (200, HTML, big) for i in range(50)}},
              max_pages=5, max_per_host=100)
    assert c.pages == 5


def test_non_html_responses_are_dropped_at_the_header():
    c = crawl({"/": (200, "application/json", '{"careers": "boards.greenhouse.io/acme"}')},
              max_pages=5)
    assert c.findings == []
    assert c.stats["skipped_content_type"] >= 1


def test_two_employers_resolving_to_one_token_keep_separate_provenance():
    """A careers page that links to somebody else's board is how
    `smartrecruiters:ni` got attributed to KPMG. Deduping on (vendor, token)
    alone would drop the second employer's row and hide that entirely — the
    provenance is the only way to tell "one employer, two hosts" from "this
    page points at a board that isn't theirs"."""
    board = page("greenhouse_embed")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": HTML}, text=board)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = Crawler(client, delay=0, use_sitemaps=False, max_pages=10)
            c.seed_from("acme.com")
            c.seed_from("beta.com")
            await c.run()
            return c

    c = asyncio.run(go())
    assert {(f.token, f.seed) for f in c.findings} == \
        {("acmelabs", "acme.com"), ("acmelabs", "beta.com")}


def test_a_resumed_crawl_continues_instead_of_refetching():
    """The seen-set is the part of the state that matters. Without it a resumed
    crawl re-fetches everything it already has, which is rude twice over: once
    to the host, once to the budget."""
    fetched: list[str] = []
    routes = {"/": (200, HTML, page("homepage")),
              "/about/careers": (200, HTML, "<html><a href='/x'>x</a></html>"),
              "/join-us": (200, HTML, "<html>nothing</html>")}

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(request.url.path)
        status, ctype, body = routes.get(request.url.path, (404, HTML, ""))
        return httpx.Response(status, headers={"content-type": ctype}, text=body)

    async def go(state=None, budget=2):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = Crawler(client, delay=0, use_sitemaps=False, max_pages=budget)
            if state:
                c.load_state(state)
            c.seed_from("acme.com")
            await c.run()
            return c

    first = asyncio.run(go(budget=2))
    assert first.pages == 2
    after_first = list(fetched)

    second = asyncio.run(go(first.state(), budget=10))
    # robots.txt is deliberately not persisted — it can change between runs, and
    # a fresh process re-reading it is the polite behaviour, not a regression.
    resumed_paths = [p for p in fetched[len(after_first):] if p != "/robots.txt"]
    assert set(after_first).isdisjoint(resumed_paths), "refetched an already-seen page"
    assert second.pages, "the resumed crawl had frontier left and did nothing with it"
