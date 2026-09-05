"""A focused careers-page crawler — hard problem #1, from the other side.

`scripts/discover_boards.py` reads Common Crawl's URL index, so it finds a
board only if Common Crawl already fetched a URL on the *ATS's own* domain.
Three gaps follow, and this crawler exists to close them:

  * **Lever.** Common Crawl barely indexes `jobs.lever.co` — a full sweep
    returns essentially just `robots.txt`, which is why `data/discovery/` has
    a `validated_greenhouse.json` and no `validated_lever.json`.
  * **Embeds.** An employer who iframes their board
    (`boards.greenhouse.io/embed/job_board/js?for=<token>`) or serves it under
    their own domain never produces a crawlable ATS URL at all. The token
    exists only inside *their* HTML.
  * **Two-part tokens.** Workday, Oracle and Eightfold identities are
    `tenant.wdN/Site`, `host/CX_1` and `tenant/domain` — not a path segment,
    so a URL-index regex has nothing to lift out. `adapters/workday.py` already
    says these "come from careers-page crawling, never from slug guessing".
    This is that crawling.

It is *focused* in Chakrabarti's sense, and the focus is what makes it finite
rather than a web crawl that never ends: it starts from employer domains we
already know, scores every link on how careers-page-ish it looks, and **stops a
host the moment a board fingerprint hits**. One token per employer is the goal,
not a site map. The unit of discovery is still a board, never a job — crawling
job links would rebuild a job board, which is the thing this project exists not
to be.

Politeness is structural, not a setting to turn down: robots.txt is fetched and
obeyed per host (including `Crawl-delay`), requests to one host are serialised
behind a minimum delay, the User-Agent names the project, responses are
content-type filtered and byte-capped, and the retry rule is the adapters'
(5xx/429/transport only — a 404 is a settled answer). Public pages, declared
identity, no evasion: the same line `discover_boards.py` draws.

The output is `(vendor, token)` pairs for `discover_boards.py validate`, which
remains the only thing that decides a board is real.
"""

from __future__ import annotations

import asyncio
import heapq
import re
import time
import urllib.parse
import urllib.robotparser
from collections import Counter
from dataclasses import dataclass, field
from html import unescape
from typing import Callable, Iterable

import httpx

UA = "reqtrace/0.1 (+personal job-search index; contact via repo)"

MAX_BYTES = 2_000_000        # a careers page that needs more than this isn't one
MIN_HOST_DELAY = 1.5         # seconds between requests to the same host
MAX_SITEMAP_URLS = 2_000     # a sitemap index can be enormous; sample, don't drain


# ---------------------------------------------------------------------------
# URL canonicalisation
# ---------------------------------------------------------------------------
# Two URLs that differ only by a tracking parameter, a fragment, a default port
# or a trailing slash are one page. Getting this wrong is how a crawler spends
# its whole budget re-fetching the same page under different names.

_TRACKING_PREFIXES = ("utm_", "pk_", "mtm_", "_hs")
_TRACKING_EXACT = {
    "gclid", "fbclid", "msclkid", "yclid", "igshid", "mc_cid", "mc_eid",
    "ref", "referrer", "source", "src", "_ga", "_gl", "cmpid", "campaign",
}

# Extensions that are never a careers page. Cheaper to skip by name than to
# fetch and read the content type.
_BINARY_EXT = re.compile(
    r"\.(?:pdf|docx?|xlsx?|pptx?|zip|gz|tgz|rar|dmg|exe|pkg|csv|rss|ics"
    r"|jpe?g|png|gif|svg|webp|avif|ico|bmp|tiff?"
    r"|mp[34g]|m4[av]|mov|avi|webm|wav|ogg"
    r"|css|js|mjs|map|woff2?|ttf|eot|json)$", re.I)

_HTTP = ("http", "https")


def canonicalise(href: str, base: str | None = None) -> str | None:
    """Resolve `href` against `base` and reduce it to one canonical form.

    Returns None for anything not worth fetching: a non-HTTP scheme, a binary
    asset, a mailto/tel/javascript link, junk.
    """
    if not href:
        return None
    href = unescape(href.strip())
    if not href or href.startswith("#"):
        return None
    if base:
        href = urllib.parse.urljoin(base, href)
    try:
        p = urllib.parse.urlsplit(href)
    except ValueError:
        return None
    if p.scheme.lower() not in _HTTP or not p.hostname:
        return None

    host = p.hostname.lower().rstrip(".")
    port = p.port
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"

    path = urllib.parse.quote(urllib.parse.unquote(p.path), safe="/:@!$&'()*+,;=~-._")
    if _BINARY_EXT.search(path):
        return None
    if not path:
        path = "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    kept = [
        (k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_EXACT
        and not k.lower().startswith(_TRACKING_PREFIXES)
    ]
    query = urllib.parse.urlencode(sorted(kept))

    return urllib.parse.urlunsplit((p.scheme.lower(), netloc, path, query, ""))


# Second-level suffixes we actually meet. Not a public suffix list — a real one
# is a dependency and a monthly download, and getting `com.au` and `co.uk`
# right covers every seed in data/.
_TWO_LABEL_SUFFIXES = {
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "asn.au", "id.au",
    "co.uk", "org.uk", "ac.uk", "gov.uk", "co.nz", "org.nz", "net.nz",
    "co.in", "co.jp", "co.kr", "co.za", "com.sg", "com.br", "com.mx",
    "com.cn", "co.il", "com.hk", "com.my", "com.ph", "co.th",
}


def host_of(url: str) -> str:
    h = (urllib.parse.urlsplit(url).hostname or "").lower()
    return h.rstrip(".")


def registrable(host_or_url: str) -> str:
    """`www.foo.com.au/x` -> `foo.com.au`. The crawl's notion of "same employer".

    A careers page frequently lives on a sibling host (`careers.foo.com`,
    `jobs.foo.com`, `life.foo.com`), so the boundary has to be the registrable
    domain rather than the exact host — otherwise the crawler stops one link
    short of the thing it came for.
    """
    host = host_or_url if "/" not in host_or_url else host_of(host_or_url)
    host = host.lower().rstrip(".")
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if ".".join(labels[-2:]) in _TWO_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def same_site(a: str, b: str) -> bool:
    return bool(a) and registrable(a) == registrable(b)


# ---------------------------------------------------------------------------
# Link extraction
# ---------------------------------------------------------------------------
# Regex, not a parser. Adding lxml/BeautifulSoup to pull `href` out of anchors
# would be the project's first heavyweight dependency, and the fingerprints
# below have to run over the raw HTML anyway — the tokens we want live in
# script bodies, iframe srcs and data- attributes that a link parser drops.

_A_RE = re.compile(r"<a\b[^>]*?\bhref\s*=\s*[\"']([^\"']+)[\"']([^>]*)>(.*?)</a>",
                   re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)


def _text(fragment: str) -> str:
    return _WS_RE.sub(" ", unescape(_TAG_RE.sub(" ", fragment))).strip()


def extract_links(html: str, base: str) -> list[tuple[str, str]]:
    """-> [(canonical_url, anchor_text)], deduplicated, order preserved."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in _A_RE.finditer(html):
        href, attrs, inner = m.group(1), m.group(2), m.group(3)
        url = canonicalise(href, base)
        if not url or url in seen:
            continue
        seen.add(url)
        label = _text(inner)[:120]
        if not label:
            m = re.search(r"\b(?:aria-label|title)\s*=\s*[\"']([^\"']+)", attrs, re.I)
            label = _text(m.group(1))[:120] if m else ""
        out.append((url, label))
    return out


def sitemap_urls(xml: str, limit: int = MAX_SITEMAP_URLS) -> list[str]:
    """`<loc>` entries out of a sitemap or a sitemap index."""
    out, seen = [], set()
    for m in _LOC_RE.finditer(xml):
        u = canonicalise(m.group(1))
        if u and u not in seen:
            seen.add(u)
            out.append(u)
            if len(out) >= limit:
                break
    return out


# ---------------------------------------------------------------------------
# Link scoring — the "focused" in focused crawler
# ---------------------------------------------------------------------------
# Best-first, not breadth-first. The frontier is a priority queue and this
# function is the priority: it decides which of the 300 links on a homepage is
# worth one of a finite number of requests. Anything scoring <= 0 is dropped
# outright, which is what stops the crawl leaking into the whole site.

CAREER_WORDS = (
    "career", "careers", "job", "jobs", "vacancy", "vacancies", "opening",
    "openings", "position", "positions", "opportunity", "opportunities",
    "join-us", "joinus", "join", "work-with-us", "workwithus", "work-for-us",
    "working-at", "work-at", "life-at", "lifeat", "hiring", "recruitment",
    "recruiting", "employment", "apply", "talent", "people", "team",
)
# Words that mean "this branch of the site is not careers". A blog post about
# hiring is not a careers page, and /investors is a 400-page sink.
NEGATIVE_WORDS = (
    "blog", "news", "press", "media", "article", "story", "stories", "event",
    "webinar", "podcast", "privacy", "terms", "legal", "cookie", "policy",
    "login", "signin", "sign-in", "signup", "register", "account", "cart",
    "checkout", "pricing", "product", "docs", "documentation", "support",
    "help", "faq", "investor", "shareholder", "annual-report", "case-study",
    "customer", "partner", "download", "sitemap", "search", "tag", "category",
    "archive", "wp-content", "wp-json", "feed", "comment", "share",
)

# The canonical landing pages. Reaching one of these is usually one hop from a
# board fingerprint, so they outrank everything else in the frontier.
_CAREERS_PATH = re.compile(
    r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?(?:about/|company/|au/|en/)?"
    r"(?:careers?|jobs|join-us|work-with-us|vacancies|opportunities)/?$", re.I)

WELL_KNOWN_PATHS = (
    "/careers", "/careers/", "/jobs", "/job", "/join-us", "/work-with-us",
    "/about/careers", "/company/careers", "/en/careers", "/au/careers",
    "/careers/jobs", "/about-us/careers", "/careers/open-roles",
)

STRONG = 60  # at or above this, a link is a real careers candidate


def _segments(url: str) -> list[str]:
    return [s for s in re.split(r"[/_.-]+", urllib.parse.urlsplit(url).path.lower()) if s]


def score_link(url: str, anchor: str = "", *, origin: str = "") -> int:
    """How much this link looks like a step toward a job board. <= 0 means drop.

    `origin` is the page the link was found on; a link that leaves the
    employer's registrable domain only survives if it points at an ATS, and
    that is decided by the fingerprints, not here.
    """
    path = urllib.parse.urlsplit(url).path.lower()
    segs = _segments(url)
    words = set(segs)
    label = anchor.lower()

    if origin and not same_site(url, origin):
        return 0  # off-site: fingerprints already read it out of the HTML

    score = 0
    if _CAREERS_PATH.match(path or "/"):
        score += 100
    hits = words & set(CAREER_WORDS)
    if hits:
        # `/careers` beats `/about/team/leadership` — earlier and shallower wins.
        score += 45 + max(0, 15 - 5 * segs.index(next(s for s in segs if s in hits)))
    if any(w in label for w in ("career", "job", "join us", "work with us",
                                "vacanc", "hiring", "open role", "we're hiring",
                                "life at", "opportunit")):
        score += 40
    if words & set(NEGATIVE_WORDS):
        score -= 70
    if len(segs) > 4:
        score -= 10 * (len(segs) - 4)   # deep pages are article-shaped
    if urllib.parse.urlsplit(url).query:
        score -= 5
    return max(score, 0)


# ---------------------------------------------------------------------------
# ATS fingerprints — the payload
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Fingerprint:
    vendor: str
    pattern: re.Pattern[str]
    build: Callable[[re.Match[str], str], str | None] = lambda m, d: m.group(1)
    #: False = we can identify the vendor but have no adapter, so the hit is
    #: recorded as intelligence (which suite does NAB run?) rather than a token.
    ingestable: bool = True


def _workday(m: re.Match[str], _domain: str) -> str:
    """`cba.wd3.myworkdayjobs.com/en-US/CommBank_Careers` -> `cba.wd3/CommBank_Careers`,
    the composite identity adapters/workday.py parses."""
    return f"{m.group(1)}.{m.group(2)}/{m.group(3)}"


def _workday_site(m: re.Match[str], _domain: str) -> str:
    """The `myworkdaysite.com/recruiting/{tenant}/{site}` form of the same board."""
    return f"{m.group(2)}.{m.group(1)}/{m.group(3)}"


def _eightfold(m: re.Match[str], domain: str) -> str | None:
    """Eightfold's identity is `tenant/domain`, and the domain is the employer's
    — which a URL index never knows and a crawl always does, because we arrived
    from that employer's own site."""
    return f"{m.group(1)}/{domain}" if domain else None


FINGERPRINTS: tuple[Fingerprint, ...] = (
    # Greenhouse. The embed forms come first: the plain host regex would read
    # `embed` out of `boards.greenhouse.io/embed/job_board/js?for=acme` and
    # throw away the real token sitting in the query string. That is precisely
    # the shape Common Crawl's URL index cannot resolve.
    Fingerprint("greenhouse", re.compile(
        r"boards\.greenhouse\.io/embed/job_board(?:/js)?\?(?:[^\"'&]*&)*for=([A-Za-z0-9_-]+)", re.I)),
    Fingerprint("greenhouse", re.compile(
        r"Grnhse\.Settings[^{]*\{[^}]*?[\"']?for[\"']?\s*[:=]\s*[\"']([A-Za-z0-9_-]+)", re.I)),
    Fingerprint("greenhouse", re.compile(
        r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)", re.I)),
    Fingerprint("greenhouse", re.compile(
        r"(?:job-)?boards\.greenhouse\.io/([A-Za-z0-9_-]+)", re.I)),

    # Lever. The embed div carries the site in an attribute, and Lever tokens
    # are case-sensitive (jobs.lever.co/Zeller resolves, /zeller 404s), so
    # nothing downstream may lowercase these.
    Fingerprint("lever", re.compile(
        r"data-lever-site\s*=\s*[\"']([A-Za-z0-9_-]+)", re.I)),
    Fingerprint("lever", re.compile(
        r"api\.lever\.co/v0/postings/([A-Za-z0-9_-]+)", re.I)),
    Fingerprint("lever", re.compile(
        r"jobs(?:\.eu)?\.lever\.co/([A-Za-z0-9_-]+)", re.I)),

    Fingerprint("ashby", re.compile(
        r"api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9_.-]+)", re.I)),
    Fingerprint("ashby", re.compile(
        r"jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)", re.I)),

    Fingerprint("smartrecruiters", re.compile(
        r"api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9_-]+)", re.I)),
    Fingerprint("smartrecruiters", re.compile(
        r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.I)),

    # Fingerprinted but *not* ingestable: there is no adapters/workable.py yet,
    # so a hit here is intelligence like the suites below, not a token to adopt.
    # `fixtures/raw/workable/rokt.json` is the head start for writing that adapter;
    # flip this to ingestable when it exists.
    Fingerprint("workable", re.compile(
        r"apply\.workable\.com/(?:api/v\d+/widget/accounts/)?([A-Za-z0-9_-]+)", re.I),
        ingestable=False),

    # Two-part identities. No URL-index regex can produce these.
    Fingerprint("workday", re.compile(
        r"([A-Za-z0-9_-]+)\.(wd\d+)\.myworkdayjobs\.com/"
        r"(?:wday/cxs/[^/]+/)?(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)"), _workday),
    Fingerprint("workday", re.compile(
        r"[A-Za-z0-9_-]+\.(wd\d+)\.myworkdaysite\.com/(?:[a-z]{2}-[A-Z]{2}/)?"
        r"recruiting/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)"), _workday_site),
    Fingerprint("oracle", re.compile(
        r"([A-Za-z0-9-]+\.fa\.[A-Za-z0-9]+\.oraclecloud\.com)"
        r"(?:/hcmUI/CandidateExperience)?[^\"'\s]*?/sites/(CX_\d+)", re.I),
        lambda m, d: f"{m.group(1).lower()}/{m.group(2).upper()}"),
    Fingerprint("eightfold", re.compile(
        r"([A-Za-z0-9-]+)\.eightfold\.ai/careers", re.I), _eightfold),

    # Identified, not ingestable. These are the enterprise suites the Step 0
    # audit left unresolved — NAB on Avature, Macquarie and ANZ on
    # SuccessFactors. Knowing which suite an employer runs is the whole
    # precondition for ever writing that adapter, so record it.
    Fingerprint("teamtailor", re.compile(
        r"([A-Za-z0-9-]+)\.teamtailor\.com", re.I), ingestable=False),
    Fingerprint("successfactors", re.compile(
        r"(?:career\d*|performancemanager\d*)\.(?:successfactors|sapsf)\.(?:com|eu)"
        r"[^\"'\s]*?company=([A-Za-z0-9_]+)", re.I), ingestable=False),
    Fingerprint("successfactors", re.compile(
        r"([A-Za-z0-9-]+)\.jobs\.sap\.com", re.I), ingestable=False),
    Fingerprint("avature", re.compile(
        r"([A-Za-z0-9-]+)\.avature\.net", re.I), ingestable=False),
    Fingerprint("icims", re.compile(
        r"([A-Za-z0-9-]+)\.icims\.com", re.I), ingestable=False),
    Fingerprint("taleo", re.compile(
        r"([A-Za-z0-9-]+)\.taleo\.net", re.I), ingestable=False),
    Fingerprint("phenom", re.compile(
        r"(?:([A-Za-z0-9-]+)\.)?phenompeople\.com", re.I),
        lambda m, d: m.group(1) or d, ingestable=False),
    Fingerprint("recruitee", re.compile(
        r"([A-Za-z0-9-]+)\.recruitee\.com", re.I), ingestable=False),
    Fingerprint("personio", re.compile(
        r"([A-Za-z0-9-]+)\.jobs\.personio\.(?:com|de)", re.I), ingestable=False),
    Fingerprint("bamboohr", re.compile(
        r"([A-Za-z0-9-]+)\.bamboohr\.com", re.I), ingestable=False),
    Fingerprint("jobvite", re.compile(
        r"jobs\.jobvite\.com/([A-Za-z0-9-]+)", re.I), ingestable=False),
    Fingerprint("pinpoint", re.compile(
        r"([A-Za-z0-9-]+)\.pinpointhq\.com", re.I), ingestable=False),
)

#: Vendors `discover_boards.py validate` can check against a public JSON feed.
VALIDATABLE = ("greenhouse", "lever", "ashby", "smartrecruiters")

#: Vendors with an adapter, so a found token is directly ingestable.
INGESTABLE = {f.vendor for f in FINGERPRINTS if f.ingestable}

# Path noise that is not a board identifier. `discover_boards.py` keeps its own
# copy: that one is tuned for Common Crawl URL fragments, this one for the junk
# HTML produces (asset paths, embed scaffolding, framework chunks). They drift
# apart on purpose.
SKIP_TOKENS = {
    "embed", "embeds", "job_board", "job-board", "jobs", "job", "career",
    "careers", "api", "v0", "v1", "v2", "search", "static", "assets", "asset",
    "www", "boards", "board", "company", "companies", "apply", "accounts",
    "account", "en", "en-us", "us", "au", "uk", "postings", "posting", "js",
    "css", "img", "images", "image", "logo", "favicon", "robots", "sitemap",
    "index", "widget", "widgets", "iframe", "script", "scripts", "style",
    "styles", "main", "app", "bundle", "vendor", "runtime", "chunk", "share",
    "null", "undefined", "none", "true", "false", "default", "public", "cdn",
}
_HEXISH = re.compile(r"^[0-9a-f]{16,}$", re.I)


def plausible_token(vendor: str, token: str | None) -> bool:
    """Cheap junk filter. Validation against the vendor's feed is what really
    decides — this only avoids spending a request on an obvious asset path."""
    if not token:
        return False
    if vendor in ("workday", "oracle", "eightfold"):
        # Composite identities: shape is the check, and both halves matter.
        left, _, right = token.partition("/")
        return bool(left and right) and len(token) <= 120
    t = token.lower()
    return (
        t not in SKIP_TOKENS
        and not t.isdigit()
        and not _HEXISH.match(t)
        and 1 < len(token) <= 60
        and not t.endswith((".js", ".css", ".png", ".svg", ".json"))
    )


def find_boards(html: str, *, url: str = "", domain: str = "") -> list[tuple[str, str]]:
    """-> [(vendor, token)] found in this page. The page's own URL is scanned
    too, so a crawl that lands directly on a board still reports it."""
    haystack = f"{url}\n{html}" if url else html
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for fp in FINGERPRINTS:
        for m in fp.pattern.finditer(haystack):
            try:
                token = fp.build(m, domain)
            except (IndexError, AttributeError):
                continue
            if not plausible_token(fp.vendor, token):
                continue
            key = (fp.vendor, token)
            if key not in seen:
                seen.add(key)
                out.append(key)
    return out


# ---------------------------------------------------------------------------
# The crawl
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Finding:
    vendor: str
    token: str
    source_url: str
    seed: str          # the employer domain the crawl started from
    ingestable: bool

    def as_row(self) -> dict:
        return {
            "ats_vendor": self.vendor, "board_token": self.token,
            "found_on": self.source_url, "seed_domain": self.seed,
            "ingestable": "yes" if self.ingestable else "no",
        }


@dataclass(order=True)
class _Task:
    priority: int                      # negated score; heapq is a min-heap
    seq: int
    url: str = field(compare=False)
    depth: int = field(compare=False, default=0)
    seed: str = field(compare=False, default="")


@dataclass
class _Host:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    next_ok: float = 0.0
    delay: float = MIN_HOST_DELAY
    pages: int = 0
    robots: urllib.robotparser.RobotFileParser | None = None
    checked_robots: bool = False
    exhausted: bool = False            # budget spent, or a board already found
    probed_fallbacks: bool = False


class Crawler:
    """Best-first, robots-respecting, per-host-serialised, resumable.

    Terminates on any of: the global page budget, an empty frontier, or — per
    host — a board fingerprint hitting. The last one is the important one. A
    general crawler has no natural stopping point; this one is done with an
    employer the moment it has their board token, which is what keeps the whole
    sweep in the low thousands of requests rather than the millions.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        max_pages: int = 500,
        max_per_host: int = 12,
        max_depth: int = 3,
        concurrency: int = 4,
        delay: float = MIN_HOST_DELAY,
        obey_robots: bool = True,
        use_sitemaps: bool = True,
        on_event: Callable[[str, str], None] | None = None,
    ) -> None:
        self.client = client
        self.max_pages = max_pages
        self.max_per_host = max_per_host
        self.max_depth = max_depth
        self.concurrency = concurrency
        self.delay = delay
        self.obey_robots = obey_robots
        self.use_sitemaps = use_sitemaps
        self._log = on_event or (lambda kind, msg: None)

        self._frontier: list[_Task] = []
        self._seq = 0
        self._inflight = 0
        self.seen: set[str] = set()
        self.hosts: dict[str, _Host] = {}
        self.findings: list[Finding] = []
        # Keyed by (vendor, token, seed), not (vendor, token). A token is an
        # employer, so the same one arriving from two seeds is a *signal*: it
        # is either one employer reached under two hosts, or — the case that
        # matters — a careers page linking to somebody else's board, which is
        # how `smartrecruiters:ni` got attributed to KPMG. Deduping that away
        # would lose the provenance needed to tell those two apart.
        self._found_keys: set[tuple[str, str, str]] = set()
        self.pages = 0
        self.stats: Counter[str] = Counter()

    # -- frontier ----------------------------------------------------------

    def enqueue(self, url: str, *, score: int, depth: int, seed: str) -> bool:
        url = canonicalise(url) or ""
        if not url or url in self.seen or depth > self.max_depth or score <= 0:
            return False
        self.seen.add(url)
        self._seq += 1
        # Depth is a tie-breaker, not a wall: a strong link three hops in still
        # beats a weak one on the homepage.
        heapq.heappush(self._frontier, _Task(-(score - 8 * depth), self._seq,
                                             url, depth, seed))
        return True

    def seed_from(self, domain: str, careers_url: str = "") -> None:
        """One employer. The audit's `careers_url` column, where it exists, is
        a hand-verified head start — start there and at the homepage both."""
        domain = domain.strip().lower().lstrip("@")
        if not domain:
            return
        if careers_url:
            self.enqueue(careers_url, score=500, depth=0, seed=domain)
        self.enqueue(f"https://{domain}/", score=300, depth=0, seed=domain)

    def _pop_ready(self) -> _Task | None:
        """Highest-scoring task whose host is free. Scans past a few busy hosts
        so one slow site cannot stall workers that have other work to do."""
        deferred: list[_Task] = []
        picked: _Task | None = None
        while self._frontier and len(deferred) < 16:
            task = heapq.heappop(self._frontier)
            host = self.hosts.get(host_of(task.url))
            if host and (host.exhausted or host.pages >= self.max_per_host):
                self.stats["skipped_host_done"] += 1
                continue
            if host and host.lock.locked():
                deferred.append(task)
                continue
            picked = task
            break
        for t in deferred:
            heapq.heappush(self._frontier, t)
        return picked

    # -- fetching ----------------------------------------------------------

    async def _get(self, url: str) -> tuple[int, str]:
        """One polite GET. Streams so an unexpectedly enormous body is truncated
        rather than downloaded, and non-HTML is dropped at the header."""
        try:
            async with self.client.stream("GET", url) as r:
                if r.status_code != 200:
                    return r.status_code, ""
                ctype = r.headers.get("content-type", "").lower()
                if not any(t in ctype for t in ("html", "xml", "text/plain")):
                    self.stats["skipped_content_type"] += 1
                    return r.status_code, ""
                chunks, n = [], 0
                async for chunk in r.aiter_bytes():
                    chunks.append(chunk)
                    n += len(chunk)
                    if n >= MAX_BYTES:
                        self.stats["truncated"] += 1
                        break
                body = b"".join(chunks)
                enc = r.charset_encoding or "utf-8"
            return 200, body.decode(enc, errors="replace")
        except Exception as exc:  # noqa: BLE001 - one dead host is not a dead crawl
            self.stats[f"error_{type(exc).__name__}"] += 1
            self._log("error", f"{url} {type(exc).__name__}")
            return 0, ""

    async def _robots(self, host: _Host, hostname: str) -> None:
        """Fetch and honour robots.txt once per host, `Crawl-delay` included.

        An unreachable or malformed robots.txt is treated as allow-all, which
        is what the standard says and what every well-behaved crawler does.
        """
        host.checked_robots = True
        if not self.obey_robots:
            return
        status, text = await self._get(f"https://{hostname}/robots.txt")
        if status != 200 or not text:
            return
        rp = urllib.robotparser.RobotFileParser()
        try:
            rp.parse(text.splitlines())
        except Exception:  # noqa: BLE001
            return
        host.robots = rp
        cd = rp.crawl_delay(UA) or rp.crawl_delay("*")
        if cd:
            host.delay = max(host.delay, float(cd))
            self._log("robots", f"{hostname} crawl-delay {cd}s")

    def _allowed(self, host: _Host, url: str) -> bool:
        if not (self.obey_robots and host.robots):
            return True
        try:
            return host.robots.can_fetch(UA, url)
        except Exception:  # noqa: BLE001
            return True

    # -- one page ----------------------------------------------------------

    async def _visit(self, task: _Task) -> None:
        hostname = host_of(task.url)
        host = self.hosts.setdefault(hostname, _Host(delay=self.delay))
        async with host.lock:
            if host.exhausted or host.pages >= self.max_per_host:
                return
            if not host.checked_robots:
                await self._robots(host, hostname)
            if not self._allowed(host, task.url):
                self.stats["robots_disallowed"] += 1
                self._log("robots", f"disallowed {task.url}")
                return
            wait = host.next_ok - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            host.next_ok = time.monotonic() + host.delay
            host.pages += 1
            self.pages += 1
            status, html = await self._get(task.url)

        self.stats[f"status_{status}"] += 1
        if status != 200 or not html:
            return

        hits = find_boards(html, url=task.url, domain=task.seed or registrable(task.url))
        if hits:
            self._record(hits, task)
            # Done with this employer. A second token from the same host would
            # be the same board under a different URL nine times in ten.
            host.exhausted = True
            return

        if task.depth >= self.max_depth:
            return

        links = extract_links(html, task.url)
        strong = 0
        for url, anchor in links:
            score = score_link(url, anchor, origin=task.url)
            if score >= STRONG:
                strong += 1
            if score > 0:
                self.enqueue(url, score=score, depth=task.depth + 1, seed=task.seed)

        # Nothing on this page looked like a way in — the site is a JS shell, or
        # it hides careers in a footer image. Fall back to the conventions.
        if not strong and task.depth == 0 and not host.probed_fallbacks:
            host.probed_fallbacks = True
            await self._fallbacks(hostname, task)

    async def _fallbacks(self, hostname: str, task: _Task) -> None:
        for path in WELL_KNOWN_PATHS:
            self.enqueue(f"https://{hostname}{path}", score=STRONG + 10,
                         depth=1, seed=task.seed)
        if not self.use_sitemaps:
            return
        host = self.hosts[hostname]
        maps = []
        if host.robots:
            try:
                maps = list(host.robots.site_maps() or [])
            except Exception:  # noqa: BLE001
                maps = []
        for sm in (maps or [f"https://{hostname}/sitemap.xml"])[:2]:
            async with host.lock:
                wait = host.next_ok - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                host.next_ok = time.monotonic() + host.delay
                status, xml = await self._get(sm)
            if status != 200 or not xml:
                continue
            self.stats["sitemaps"] += 1
            added = 0
            for url in sitemap_urls(xml):
                score = score_link(url, origin=task.url)
                if score >= STRONG and self.enqueue(url, score=score, depth=1,
                                                    seed=task.seed):
                    added += 1
                    if added >= 20:
                        break
            self._log("sitemap", f"{sm} -> {added} careers-ish urls")

    def _record(self, hits: Iterable[tuple[str, str]], task: _Task) -> None:
        for vendor, token in hits:
            key = (vendor, token, task.seed)
            if key in self._found_keys:
                continue
            self._found_keys.add(key)
            f = Finding(vendor, token, task.url, task.seed, vendor in INGESTABLE)
            self.findings.append(f)
            self.stats[f"found_{vendor}"] += 1
            self._log("found", f"{vendor}:{token}  <- {task.url}")

    # -- driver ------------------------------------------------------------

    async def run(self) -> list[Finding]:
        async def worker() -> None:
            while True:
                if self.pages >= self.max_pages:
                    return
                task = self._pop_ready()
                if task is None:
                    if self._inflight == 0 and not self._frontier:
                        return
                    await asyncio.sleep(0.2)
                    continue
                self._inflight += 1
                try:
                    await self._visit(task)
                finally:
                    self._inflight -= 1

        await asyncio.gather(*(worker() for _ in range(self.concurrency)))
        return self.findings

    # -- resumable state ---------------------------------------------------

    def state(self) -> dict:
        return {
            "seen": sorted(self.seen),
            "frontier": [[t.priority, t.url, t.depth, t.seed]
                         for t in sorted(self._frontier)],
            "hosts": {h: {"pages": st.pages, "exhausted": st.exhausted}
                      for h, st in self.hosts.items()},
            "findings": [f.as_row() for f in self.findings],
            "stats": dict(self.stats),
        }

    def load_state(self, state: dict) -> None:
        """Resume. The seen-set is the part that matters: without it a resumed
        crawl re-fetches everything it already has, which is rude twice over."""
        self.seen = set(state.get("seen", []))
        for priority, url, depth, seed in state.get("frontier", []):
            self._seq += 1
            heapq.heappush(self._frontier, _Task(priority, self._seq, url, depth, seed))
        for hostname, st in state.get("hosts", {}).items():
            h = self.hosts.setdefault(hostname, _Host(delay=self.delay))
            h.pages = st.get("pages", 0)
            h.exhausted = bool(st.get("exhausted"))
        for row in state.get("findings", []):
            key = (row["ats_vendor"], row["board_token"], row.get("seed_domain", ""))
            if key in self._found_keys:
                continue
            self._found_keys.add(key)
            self.findings.append(Finding(
                row["ats_vendor"], row["board_token"], row.get("found_on", ""),
                row.get("seed_domain", ""), row.get("ingestable", "yes") == "yes"))
        self.stats.update(state.get("stats", {}))
