"""The database-backed frontier, and the expansion rules that feed it.

The properties tested here are the ones an always-on crawler depends on and a
single-shot crawl never exercised: that a restart does not re-fetch, that a
crash does not strand work, that a per-host budget survives across laps, and
that expansion cannot wander off the employer web into a news site.

SQLite in a tmp_path throughout — `Frontier` takes a `Store`, so it inherits
the same backend detection everything else in the suite runs on.
"""

import pytest

from reqtrace.crawl import (
    Finding, harvest_seeds, looks_like_a_directory, seedable_domain,
)
from reqtrace.frontier import Frontier
from reqtrace.store import Store


@pytest.fixture()
def f(tmp_path):
    store = Store(sqlite_path=tmp_path / "t.db")
    store.init_schema()
    fr = Frontier(store)
    fr.init_schema()
    yield fr
    store.close()


def rows(*urls, score=100, depth=0, seed="acme.com"):
    return [(u, u.split("/")[2], score, depth, seed) for u in urls]


# -- the queue -------------------------------------------------------------

def test_add_dedupes_on_url(f):
    assert f.add(rows("https://a.com/x")) == 1
    assert f.add(rows("https://a.com/x")) == 0
    assert f.count() == 1


def test_urls_are_stored_canonical_so_one_page_is_one_row(f):
    """The regression that made the daemon re-fetch everything, every lap.

    The frontier held whatever it was handed (`/careers/`, tracking params and
    all) while `Crawler.enqueue` canonicalises internally, so the URL the
    crawler reported visiting never matched the row it came from. Nothing was
    ever marked done: every claimed row was released, re-claimed next lap, and
    re-fetched — forever, against somebody else's site.
    """
    f.add([("https://a.com/careers/", "a.com", 100, 0, "a.com")])
    claimed = f.claim(10)
    assert claimed[0].url == "https://a.com/careers"

    # The same page under three more spellings is still one row.
    f.release([p.url for p in claimed])
    assert f.add([("https://a.com/careers", "a.com", 100, 0, "a.com")]) == 0
    assert f.add([("https://a.com/careers?utm_source=x", "a.com", 100, 0, "a.com")]) == 0
    assert f.add([("https://a.com/careers#top", "a.com", 100, 0, "a.com")]) == 0
    assert f.count() == 1


def test_add_drops_what_can_never_be_fetched(f):
    assert f.add([("mailto:jobs@a.com", "a.com", 100, 0, "a.com"),
                  ("https://a.com/brochure.pdf", "a.com", 100, 0, "a.com")]) == 0
    assert f.count() == 0


def test_a_crawled_url_is_never_queued_again(f):
    """The seen-set property, across what would be a process restart.

    This is the whole reason the queue moved into the database: the old
    JSON state file held the seen-set in memory, so anything lost between
    checkpoints came back as a re-fetch of somebody's site.
    """
    f.add(rows("https://a.com/x"))
    claimed = f.claim(10)
    f.finish([p.url for p in claimed])

    # A later lap rediscovers the same link from another page.
    assert f.add(rows("https://a.com/x")) == 0
    assert f.claim(10) == []


def test_claim_marks_rows_so_a_second_crawler_takes_other_work(f):
    f.add(rows("https://a.com/1", "https://a.com/2"))
    first = f.claim(1)
    second = f.claim(1)
    assert len(first) == len(second) == 1
    assert first[0].url != second[0].url


def test_claim_is_best_first(f):
    f.add([("https://a.com/low", "a.com", 10, 0, "a.com"),
           ("https://a.com/high", "a.com", 900, 0, "a.com")])
    assert f.claim(1)[0].url.endswith("/high")


def test_release_returns_unspent_work(f):
    f.add(rows("https://a.com/1"))
    claimed = f.claim(1)
    f.release([p.url for p in claimed])
    assert len(f.claim(1)) == 1


def test_stale_claims_are_reclaimed(f):
    """A SIGKILL leaves rows claimed forever; without this the frontier leaks
    until it looks empty while holding thousands of stranded URLs."""
    f.add(rows("https://a.com/1"))
    f.claim(1)
    assert f.claim(1) == []                  # still held
    assert f.requeue_stale_claims(hours=0) == 1
    assert len(f.claim(1)) == 1


def test_exhausted_hosts_are_not_served(f):
    """One board per employer: a host that yielded a token is done for good."""
    f.add(rows("https://a.com/1", "https://a.com/2"))
    f.save_hosts({"a.com": (1, True, 1.5)})
    assert f.claim(10) == []


def test_per_host_budget_carries_across_laps(f):
    """Without persisted page counts, `max_per_host` degrades from a lifetime
    budget into a per-lap rate limit — the daemon would crawl the same site
    forever, a few pages at a time."""
    f.add(rows(*[f"https://a.com/{i}" for i in range(10)]))
    f.save_hosts({"a.com": (12, False, 1.5)})
    assert f.claim(10, max_per_host=12) == []
    assert len(f.claim(10, max_per_host=20)) == 10


def test_host_state_round_trips(f):
    f.save_hosts({"a.com": (3, False, 2.0), "b.com": (7, True, 1.5)})
    assert f.host_state(["a.com", "b.com"]) == {"a.com": (3, False),
                                                "b.com": (7, True)}


# -- findings and adoption -------------------------------------------------

def make(vendor="greenhouse", token="acme", seed="acme.com", ingestable=True):
    return Finding(vendor, token, "https://acme.com/careers", seed, ingestable)


def test_findings_dedupe_on_vendor_token_seed(f):
    assert f.record([make()]) == 1
    assert f.record([make()]) == 0


def test_the_same_token_from_two_seeds_is_kept(f):
    """Provenance is the signal that separates one employer reached under two
    hosts from a careers page linking to somebody else's board."""
    f.record([make(seed="acme.com"), make(seed="other.com")])
    assert len(f.pending_adoption()) == 2


def test_only_ingestable_findings_are_offered_for_adoption(f):
    f.record([make(), make(vendor="avature", token="nab", ingestable=False)])
    tokens = [r["board_token"] for r in f.pending_adoption()]
    assert tokens == ["acme"]
    assert len(f.pending_adoption(ingestable_only=False)) == 2


def test_adopted_findings_are_still_offered(f):
    """The flag must not be the gate, and this is the failure it prevents.

    `adopted_at` lands in Postgres; the board lands in a CSV that has to be
    committed and pushed. A rebase conflict or a dead runner between the two
    leaves the flag saying "handled" and the file not listing the board. If
    the flag gated adoption, that board would never be offered again and never
    be swept — silently, forever.

    So the caller filters against the CSV instead, and adoption is idempotent:
    a failed push just means the same boards come round again.
    """
    f.record([make()])
    f.mark_adopted([("greenhouse", "acme", "acme.com")])
    assert len(f.pending_adoption()) == 1

    # The flag is still readable, for anyone who wants the audit trail.
    assert f.pending_adoption(unadopted_only=True) == []


# -- expansion -------------------------------------------------------------

def test_seedable_domain_takes_offsite_homepages():
    assert seedable_domain("https://canva.com/",
                           origin="https://vc.com/portfolio") == "canva.com"
    assert seedable_domain("https://www.zeller.co",
                           origin="https://vc.com/portfolio") == "zeller.co"


def test_seedable_domain_rejects_the_footer_of_every_page_on_the_web():
    for url in ("https://www.facebook.com/", "https://twitter.com/",
                "https://github.com", "https://cloudflare.com/",
                "https://boards.greenhouse.io/", "https://www.seek.com.au/"):
        assert seedable_domain(url, origin="https://vc.com/portfolio") is None


def test_seedable_domain_rejects_articles_and_same_site():
    # An article is a sink, not an employer.
    assert seedable_domain("https://news.com/2024/05/why-we-hire",
                           origin="https://vc.com/portfolio") is None
    # Same site is the focused crawler's job, not expansion's.
    assert seedable_domain("https://vc.com/about",
                           origin="https://vc.com/portfolio") is None


def test_harvest_seeds_dedupes_and_caps():
    html = "".join(f'<a href="https://co{i}.com/">Co {i}</a>' for i in range(40))
    html += '<a href="https://co1.com/">Co 1 again</a>'
    got = harvest_seeds(html, "https://vc.com/portfolio", limit=10)
    assert len(got) == 10 == len(set(got))


def test_directory_detection_reads_the_url_as_well_as_the_page():
    assert looks_like_a_directory("<html><body>hi</body></html>",
                                  "https://vc.com/portfolio")
    assert looks_like_a_directory("<h1>Our customers</h1>", "https://x.com/a")
    assert not looks_like_a_directory("<h1>Privacy policy</h1>",
                                      "https://x.com/privacy")


def test_retire_can_be_narrowed_to_the_hosts_a_lap_touched(f):
    """The daemon passes the hosts it just crawled. Unnarrowed, this is an
    `UPDATE ... WHERE host IN (SELECT ...)` over an unbounded table on every
    lap, forever."""
    f.add(rows("https://a.com/1", "https://b.com/1"))
    f.save_hosts({"a.com": (3, True, 1.5), "b.com": (3, True, 1.5)})
    assert f.retire_exhausted(["a.com"]) == 1      # b.com untouched this lap
    assert f.count("pending") == 1
    assert f.retire_exhausted() == 1               # the full sweep catches it
    assert f.count("pending") == 0


def test_add_counts_only_genuinely_new_urls(f):
    """`rowcount` off one batched statement, replacing two full-table COUNT
    scans that grew with a table designed never to stop growing."""
    assert f.add(rows("https://a.com/1", "https://a.com/2")) == 2
    assert f.add(rows("https://a.com/2", "https://a.com/3")) == 1
    assert f.count() == 3


def test_links_on_a_resolved_host_are_retired(f):
    """A host that yielded a board is finished, but its already-queued links
    remain. They can never be claimed, so left pending they inflate the queue
    forever — which for a process meant to run unattended for weeks is the
    difference between a status line that means something and one that does
    not."""
    f.add(rows("https://a.com/1", "https://a.com/2", "https://b.com/1"))
    f.save_hosts({"a.com": (3, True, 1.5)})
    assert f.retire_exhausted() == 2
    assert f.count("pending") == 1
    assert f.claim(10)[0].url.startswith("https://b.com")


def test_adoption_folds_case_only_where_the_vendor_does(f, tmp_path, monkeypatch):
    """Lever resolves `Zeller` and 404s `zeller`, so two Lever tokens differing
    only in case are two boards. Workday resolves either, so they are one.
    Adoption used to lowercase every vendor's token, which silently dropped the
    second Lever board."""
    import scripts.crawl_forever as CF  # noqa: PLC0415

    csv_path = tmp_path / "discovered.csv"
    monkeypatch.setattr(CF, "DISCOVERED", csv_path)
    f.record([
        make(vendor="lever", token="Zeller", seed="zeller.com"),
        make(vendor="lever", token="zeller", seed="other.com"),
        make(vendor="workday", token="cba.wd3/CommBank_Careers", seed="cba.com.au"),
        make(vendor="workday", token="cba.wd3/commbank_careers", seed="cba.com.au"),
    ])
    CF.adopt(f)

    import csv as _csv
    got = {(r["ats_vendor"], r["board_token"])
           for r in _csv.DictReader(csv_path.open())}
    assert ("lever", "Zeller") in got and ("lever", "zeller") in got
    assert len([t for v, t in got if v == "workday"]) == 1
