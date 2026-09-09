"""Normalization helpers shared by every harvester."""

import re
from datetime import datetime, timezone
from urllib.parse import urlparse

REMOTE_PATTERN = re.compile(
    r"\b(remote|work from home|wfh|anywhere|distributed)\b", re.I
)

# Recognizes which applicant-tracking system a job URL belongs to. Used both to
# route direct polling and to auto-discover new company boards from any source.
ATS_PATTERNS = [
    ("workable", re.compile(r"apply\.workable\.com/([^/?#]+)", re.I)),
    ("recruitee", re.compile(r"([a-z0-9-]+)\.recruitee\.com", re.I)),
    (
        "greenhouse",
        re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([^/?#]+)", re.I),
    ),
    ("greenhouse", re.compile(r"([a-z0-9-]+)\.greenhouse\.io", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/([^/?#]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([^/?#]+)", re.I)),
    ("smartrecruiters", re.compile(r"jobs\.smartrecruiters\.com/([^/?#]+)", re.I)),
    ("workday", re.compile(r"([a-z0-9-]+)\.(?:wd\d+)\.myworkdayjobs\.com", re.I)),
]


def detect_ats(url: str):
    """Return (ats_type, slug) for a job URL, or ('other', None) if unrecognized."""
    if not url:
        return "other", None
    for ats, pattern in ATS_PATTERNS:
        match = pattern.search(url)
        if match:
            return ats, match.group(1)
    return "other", None


def workday_parts(url: str):
    """Extract (tenant, site) from a Workday job URL.

    e.g. https://kla.wd1.myworkdayjobs.com/Search/job/... -> ('kla', 'Search')
    """
    match = re.search(
        r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)",
        url or "",
        re.I,
    )
    if not match:
        return None, None, None
    return match.group(1), match.group(2), match.group(3)


def is_remote(location: str, title: str = "") -> bool:
    return bool(REMOTE_PATTERN.search(f"{location or ''}"))


# --------------------------------------------------------------------------
# US-location filtering
#
# We can only surface roles based in the United States, so postings whose
# location clearly names a foreign country/region are dropped before storage.
# The policy is deliberately lenient: a posting is rejected only when it carries
# a *positive* non-US signal and no US signal. Empty, "Remote", or otherwise
# ambiguous locations are kept - a missing location is far more often a US role
# with a vague board entry than a hidden foreign one.
# --------------------------------------------------------------------------

# Two-letter USPS abbreviations, matched only in the "City, ST" position so a
# stray token (e.g. the "in" in "Berlin") can't be mistaken for a state.
US_STATE_ABBREVS = {
    "al",
    "ak",
    "az",
    "ar",
    "ca",
    "co",
    "ct",
    "de",
    "fl",
    "ga",
    "hi",
    "id",
    "il",
    "in",
    "ia",
    "ks",
    "ky",
    "la",
    "me",
    "md",
    "ma",
    "mi",
    "mn",
    "ms",
    "mo",
    "mt",
    "ne",
    "nv",
    "nh",
    "nj",
    "nm",
    "ny",
    "nc",
    "nd",
    "oh",
    "ok",
    "or",
    "pa",
    "ri",
    "sc",
    "sd",
    "tn",
    "tx",
    "ut",
    "vt",
    "va",
    "wa",
    "wv",
    "wi",
    "wy",
    "dc",
}

US_STATE_NAMES = {
    "alabama",
    "alaska",
    "arizona",
    "arkansas",
    "california",
    "colorado",
    "connecticut",
    "delaware",
    "florida",
    "georgia",
    "hawaii",
    "idaho",
    "illinois",
    "indiana",
    "iowa",
    "kansas",
    "kentucky",
    "louisiana",
    "maine",
    "maryland",
    "massachusetts",
    "michigan",
    "minnesota",
    "mississippi",
    "missouri",
    "montana",
    "nebraska",
    "nevada",
    "new hampshire",
    "new jersey",
    "new mexico",
    "new york",
    "north carolina",
    "north dakota",
    "ohio",
    "oklahoma",
    "oregon",
    "pennsylvania",
    "rhode island",
    "south carolina",
    "south dakota",
    "tennessee",
    "texas",
    "utah",
    "vermont",
    "virginia",
    "washington",
    "west virginia",
    "wisconsin",
    "wyoming",
    "district of columbia",
    "washington dc",
    "washington d.c.",
    # US soil reachable without a passport.
    "puerto rico",
    "guam",
    "us virgin islands",
    "u.s. virgin islands",
}

# Foreign countries, adjectives, subnational regions, and continents. Curated to
# avoid substrings of US place names (e.g. no bare "mexico" that would swallow
# "new mexico" - that state name is checked first and wins).
NON_US_TERMS = {
    # United Kingdom & Ireland
    "united kingdom",
    "uk",
    "u.k.",
    "england",
    "scotland",
    "wales",
    "northern ireland",
    "ireland",
    "britain",
    "british",
    # Canada
    "canada",
    "canadian",
    "ontario",
    "quebec",
    "québec",
    "alberta",
    "british columbia",
    "manitoba",
    "saskatchewan",
    "nova scotia",
    # Europe
    "germany",
    "german",
    "deutschland",
    "france",
    "french",
    "spain",
    "spanish",
    "italy",
    "italian",
    "netherlands",
    "dutch",
    "belgium",
    "switzerland",
    "swiss",
    "austria",
    "poland",
    "polish",
    "portugal",
    "sweden",
    "swedish",
    "norway",
    "denmark",
    "danish",
    "finland",
    "czech",
    "czechia",
    "romania",
    "hungary",
    "greece",
    "turkey",
    "türkiye",
    "russia",
    "ukraine",
    "europe",
    "european union",
    "eu",
    "emea",
    "great britain",
    # Middle East & Africa
    "israel",
    "united arab emirates",
    "uae",
    "saudi arabia",
    "qatar",
    "egypt",
    "south africa",
    "nigeria",
    "kenya",
    "africa",
    # Asia-Pacific
    "india",
    "indian",
    "china",
    "chinese",
    "hong kong",
    "taiwan",
    "japan",
    "japanese",
    "south korea",
    "korea",
    "singapore",
    "malaysia",
    "indonesia",
    "thailand",
    "vietnam",
    "philippines",
    "pakistan",
    "bangladesh",
    "sri lanka",
    "australia",
    "australian",
    "new zealand",
    "apac",
    "asia pacific",
    "asia",
    # Latin America
    "mexico city",
    "brazil",
    "brazilian",
    "argentina",
    "chile",
    "colombia",
    "peru",
    "uruguay",
    "costa rica",
    "latin america",
    "latam",
    # High-confidence foreign cities that often appear with no country. US
    # namesakes (Berlin CT, Dublin OH, ...) carry a ", ST" tag and are caught
    # by the US check, which runs first.
    "london",
    "berlin",
    "munich",
    "münchen",
    "hamburg",
    "frankfurt",
    "cologne",
    "köln",
    "stuttgart",
    "amsterdam",
    "rotterdam",
    "dublin",
    "toronto",
    "ottawa",
    "montreal",
    "montréal",
    "vancouver",
    "calgary",
    "bengaluru",
    "bangalore",
    "hyderabad",
    "mumbai",
    "pune",
    "chennai",
    "gurgaon",
    "gurugram",
    "noida",
    "tokyo",
    "shanghai",
    "beijing",
    "shenzhen",
    "seoul",
    "sydney",
    "melbourne",
    "tel aviv",
    "warsaw",
    "krakow",
    "kraków",
    "wrocław",
    "prague",
    "bucharest",
    "budapest",
    "barcelona",
    "madrid",
    "lisbon",
    "porto",
    "milan",
    "milano",
    "zurich",
    "zürich",
    "vienna",
    "copenhagen",
    "stockholm",
    "oslo",
    "helsinki",
    "brussels",
    "istanbul",
    "dubai",
    "são paulo",
    "sao paulo",
    "bogota",
    "bogotá",
    "buenos aires",
    "kuala lumpur",
    "jakarta",
    "bangkok",
    "manila",
    "karachi",
    "lahore",
    "dhaka",
    "cairo",
    "lagos",
    "nairobi",
    "johannesburg",
    "cape town",
}

# Splits a location into independent place candidates. Sources join multiple
# locations with ";", "/", "|", or newlines.
_LOCATION_PART = re.compile(r"[;/|\n]+")
# "City, ST" or "City, ST 94105" - captures the two-letter state.
_STATE_ABBREV = re.compile(r",\s*([a-z]{2})(?:[\s,]|\d|$)")
# Standalone US country markers: "US", "U.S.", "USA", "U.S.A.".
_US_MARKER = re.compile(r"(?:^|[\s,(])u\.?\s?s\.?a?\.?(?:$|[\s,)])")


def _part_is_us(part: str) -> bool:
    if "united states" in part or _US_MARKER.search(part):
        return True
    if any(name in part for name in US_STATE_NAMES):
        return True
    match = _STATE_ABBREV.search(part)
    return bool(match and match.group(1) in US_STATE_ABBREVS)


def _part_is_non_us(part: str) -> bool:
    return any(re.search(rf"\b{re.escape(term)}\b", part) for term in NON_US_TERMS)


def text_is_non_us(text: str) -> bool:
    """True if free text (an HN/Reddit post) clearly names a non-US place and no US one.

    Unlike is_us_location, this is for unstructured blurbs with no location field.
    It is intentionally conservative - it only vetoes when a foreign signal is
    present *and* nothing US-based is, so a US role is never dropped by accident.
    """
    if not text:
        return False
    text = text.lower()
    if _part_is_us(text):
        return False
    return _part_is_non_us(text)


def is_us_location(location) -> bool:
    """True if a posting's location is acceptable as US-based.

    Returns False only when the location has at least one place and every place
    named is clearly outside the US. Empty and ambiguous locations pass, and a
    single US-based option (e.g. "Remote, US; London") keeps the whole posting.
    """
    text = clean_location(location).lower()
    if not text:
        return True

    verdict_seen = False
    for part in _LOCATION_PART.split(text):
        part = part.strip()
        if not part:
            continue
        if _part_is_us(part):
            return True
        if not _part_is_non_us(part):
            # Ambiguous option (e.g. "Remote") - benefit of the doubt.
            return True
        verdict_seen = True

    # Every named place was foreign.
    return not verdict_seen


def clean_location(value) -> str:
    if not value:
        return ""
    if isinstance(value, (list, tuple)):
        return "; ".join(str(v).strip() for v in value if v)
    if isinstance(value, dict):
        return str(value.get("name") or value.get("city") or "").strip()
    return str(value).strip()


def to_datetime(value):
    """Best-effort parse of the many timestamp shapes these APIs return."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, (int, float)):
        # Both seconds and milliseconds appear in the wild.
        seconds = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip().replace("Z", "+00:00")
    for parser in (
        lambda t: datetime.fromisoformat(t),
        lambda t: datetime.strptime(t, "%Y-%m-%d"),
        lambda t: datetime.strptime(t, "%Y-%m-%dT%H:%M:%S"),
        lambda t: datetime.strptime(t, "%a, %d %b %Y %H:%M:%S %z"),
    ):
        try:
            parsed = parser(text)
            return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
        except (ValueError, TypeError):
            continue
    return None


# Suffixes some sources append to the same underlying job link.
_URL_TAIL = re.compile(r"/(application|apply|apply-now)/?$", re.I)


def canonical_url(url: str) -> str:
    """Reduce a job URL to a stable identity shared by every source that lists it.

    The same posting reaches us with different casing (`/Etched/` vs `/etched/`),
    with or without an `/application` suffix, and with tracking params. Lowercasing
    is safe here because this value is only ever used as a dedupe key - the original
    URL is stored separately for display and linking.
    """
    if not url:
        return ""
    parsed = urlparse(url.strip())
    path = _URL_TAIL.sub("", parsed.path).rstrip("/")
    return f"{parsed.netloc}{path}".lower()


def make_posting(
    *,
    company_name,
    title,
    url,
    source,
    external_id=None,
    location="",
    description="",
    category_hint="",
    term="",
    posted_at=None,
    remote=None,
):
    """Build the normalized dict every harvester returns."""
    location = clean_location(location)
    return {
        "external_id": str(external_id) if external_id is not None else None,
        "company_name": (company_name or "").strip(),
        "title": (title or "").strip(),
        "url": (url or "").strip(),
        "canonical_url": canonical_url(url),
        "location": location,
        "description": description or "",
        "remote": is_remote(location, title) if remote is None else bool(remote),
        "source": source,
        "category_hint": (category_hint or "").strip(),
        "term": (term or "").strip(),
        "posted_at": to_datetime(posted_at),
    }
