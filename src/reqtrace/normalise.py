"""Location, remote-type and description normalisation.

Location is hard problem #2: "Sydney, NSW" / "AU-NSW-Sydney" / "Remote — ANZ" /
"Sydney or Melbourne" all have to become something filterable. This is a
deliberately rule-based first pass over the shapes actually present in the
recorded fixtures — not a general geocoder.
"""

from __future__ import annotations

import html
import re

import nh3

# nh3 defaults strip scripts/styles; job bodies only need basic formatting.
ALLOWED_TAGS = {
    "p", "br", "b", "strong", "i", "em", "u", "ul", "ol", "li",
    "h1", "h2", "h3", "h4", "h5", "h6", "a", "blockquote", "code", "pre", "hr", "span", "div",
}

AU_CITIES = {
    "sydney": "Sydney", "melbourne": "Melbourne", "brisbane": "Brisbane",
    "perth": "Perth", "adelaide": "Adelaide", "canberra": "Canberra",
    "hobart": "Hobart", "darwin": "Darwin", "gold coast": "Gold Coast",
    "newcastle": "Newcastle", "wollongong": "Wollongong", "geelong": "Geelong",
    "parramatta": "Parramatta", "north sydney": "Sydney", "surry hills": "Sydney",
    "richmond": "Melbourne", "cremorne": "Melbourne", "st leonards": "Sydney",
}
# Cities whose name alone does NOT imply Australia: Perth is also Scotland,
# Newcastle also England, Richmond also the US/UK, Hamilton also NZ/Canada.
# They still resolve as cities, but only an explicit AU marker makes them AU —
# otherwise a UK board looks Australian and pollutes the whole index.
AMBIGUOUS_CITIES = {"perth", "newcastle", "richmond", "hamilton"}

AU_STATES = {"nsw", "vic", "qld", "wa", "sa", "tas", "act", "nt"}
# Bare state codes are NOT evidence of Australia on their own: "SA" is South
# Africa as often as South Australia, and "WA" is Washington. Only an explicit
# country marker or a known AU city implies AU.
AU_MARKERS = {"australia", "aus", "au", "anz", "aunz"}

COUNTRY_HINTS = {
    "australia": "AU", "new zealand": "NZ", "united states": "US", "usa": "US",
    "united kingdom": "GB", "uk": "GB", "canada": "CA", "singapore": "SG",
    "philippines": "PH", "india": "IN", "ireland": "IE", "germany": "DE",
    "japan": "JP", "china": "CN", "france": "FR", "spain": "ES", "vietnam": "VN",
    "hong kong": "HK", "netherlands": "NL", "south korea": "KR", "korea": "KR",
    "malaysia": "MY", "indonesia": "ID", "thailand": "TH", "taiwan": "TW",
    "brazil": "BR", "mexico": "MX", "poland": "PL", "israel": "IL",
    "united arab emirates": "AE", "uae": "AE", "sweden": "SE", "switzerland": "CH",
    "italy": "IT", "portugal": "PT", "south africa": "ZA", "denmark": "DK",
    "norway": "NO", "finland": "FI", "austria": "AT", "belgium": "BE",
    "romania": "RO", "hungary": "HU", "greece": "GR", "ukraine": "UA",
    "turkey": "TR", "argentina": "AR", "colombia": "CO", "chile": "CL",
    "pakistan": "PK", "bangladesh": "BD", "sri lanka": "LK", "egypt": "EG",
}

REMOTE_RE = re.compile(r"\bremote\b|\bwork from home\b|\bwfh\b|\banywhere\b", re.I)
HYBRID_RE = re.compile(r"\bhybrid\b|\bflexible\b", re.I)
ONSITE_RE = re.compile(r"\bon[- ]?site\b|\bin[- ]?office\b", re.I)

_SPLIT = re.compile(r"\s*(?:,|;|/|\||\bor\b|\band\b|—|–|-{1,2}\s)\s*", re.I)
# Workday's placeholder for a multi-site posting, e.g. "2 Locations".
_MULTI_LOCATION = re.compile(r"^\s*\d+\s+locations?\s*$", re.I)


def sanitise_html(raw: str) -> str:
    """Vendor descriptions arrive as HTML (often entity-encoded). Unescape first,
    or nh3 sees `&lt;p&gt;` as text and the markup survives as literal angle
    brackets in the stored 'HTML'."""
    if not raw:
        return ""
    if "<" not in raw and "&lt;" in raw:
        raw = html.unescape(raw)
    return nh3.clean(raw, tags=ALLOWED_TAGS)


def html_to_text(raw: str) -> str:
    """Plain-text extraction for search and for the local classifier."""
    if not raw:
        return ""
    if "<" not in raw and "&lt;" in raw:
        raw = html.unescape(raw)
    txt = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6])>", "\n", raw)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = html.unescape(txt)
    txt = re.sub(r"[ \t\xa0]+", " ", txt)
    return re.sub(r"\n\s*\n+", "\n\n", txt).strip()


def parse_location(raw: str) -> tuple[str | None, str | None, str]:
    """-> (city, country, remote_type). Country is ISO-2 where confident."""
    if not raw or not raw.strip():
        return None, None, "unknown"

    low = raw.lower().strip()

    # Workday uses "2 Locations" as a placeholder for a multi-site posting.
    # It is not a city, and letting it through puts it in the UI as one.
    if _MULTI_LOCATION.match(low):
        return None, None, "unknown"

    remote = "unknown"
    if REMOTE_RE.search(low):
        remote = "hybrid" if HYBRID_RE.search(low) else "remote"
    elif HYBRID_RE.search(low):
        remote = "hybrid"
    elif ONSITE_RE.search(low):
        remote = "onsite"

    # "AU-NSW-Sydney" style codes
    m = re.match(r"^([a-z]{2})[- ]([a-z]{2,3})[- ](.+)$", low)
    if m and m.group(1) in {"au", "us", "nz", "gb"}:
        city = AU_CITIES.get(m.group(3).strip(), m.group(3).strip().title())
        return city, m.group(1).upper(), remote

    parts = [p.strip() for p in _SPLIT.split(low) if p.strip()]

    city = None
    for p in parts:
        if p in AU_CITIES:
            city = AU_CITIES[p]
            break
    if city is None:
        for p in parts:
            for name, canon in AU_CITIES.items():
                if name in p:
                    city = canon
                    break
            if city:
                break

    country = None
    for p in parts:
        if p in COUNTRY_HINTS:
            country = COUNTRY_HINTS[p]
            break
        for name, iso in COUNTRY_HINTS.items():
            if re.search(rf"\b{re.escape(name)}\b", p):
                country = iso
                break
        if country:
            break

    explicit_au = any(p in AU_MARKERS for p in parts)
    unambiguous_city = city is not None and not any(
        a in low for a in AMBIGUOUS_CITIES)
    if country is None and (unambiguous_city or explicit_au):
        country = "AU"

    if city is None and country is None and remote == "remote":
        return None, None, "remote"

    if city is None and parts:
        head = parts[0]
        # A state code is not a city. CommBank's Workday board lists roles as
        # "NSW/ ACT Region", which was landing in the UI as the city "Nsw".
        if (head not in AU_MARKERS and head not in AU_STATES
                and not REMOTE_RE.search(head)
                and len(head) > 2 and not head[0].isdigit()):
            city = head.title()

    return city, country, remote


_AU_HINT = re.compile(
    r"\baustralia\b|\banz\b|\baus\b|\bnsw\b|\bvic\b|\bqld\b|\bwa\b|\bsa\b"
    r"|\btas\b|\bact\b|\bnt\b|" + "|".join(re.escape(c) for c in sorted(AU_CITIES)),
    re.I)


def maybe_australian(raw: str | None) -> bool:
    """Permissive counterpart to `is_australian`, used only to decide whether a
    job is worth spending an extra detail request on.

    `is_australian` is deliberately strict because a false positive there is a
    wrong row in the index. This one is deliberately loose because a false
    positive here costs one HTTP request. Workday boards need it: CommBank
    lists roles as "NSW/ ACT Region" with no mention of Australia, and a
    multi-location posting says only "2 Locations" — neither is provably
    Australian from the listing, but both are worth opening."""
    if not raw or not raw.strip():
        # No location at all is not evidence of "not Australian". Accenture's
        # Workday board returns an empty locationsText for all ~2,000 postings,
        # and treating that as a negative silently hid every Australian role it
        # has. Unknown means "worth opening".
        return True
    if _MULTI_LOCATION.match(raw):
        return True
    return bool(_AU_HINT.search(raw))


def to_iso2(name: str | None) -> str | None:
    """Country name -> ISO-2. Vendors are inconsistent: Greenhouse locations
    yield 'United Kingdom' while Ashby's postal address says 'United States',
    and the store filters on a two-letter code."""
    if not name:
        return None
    n = name.strip()
    if len(n) == 2 and n.isalpha():
        return n.upper()
    return COUNTRY_HINTS.get(n.lower(), n)


def is_australian(city: str | None, country: str | None, raw: str) -> bool:
    """Deliberately conservative: used to decide whether a newly discovered
    board is worth keeping, where a false positive costs more than a miss."""
    if country == "AU":
        return True
    if country is not None:
        return False  # some other country was positively identified
    low = (raw or "").lower()
    if city and city in AU_CITIES.values() and not any(a in low for a in AMBIGUOUS_CITIES):
        return True
    return bool(re.search(r"\baustralia\b|\banz\b", low))
