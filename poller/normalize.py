"""Normalization helpers shared by every harvester."""

import re
from datetime import datetime, timezone
from urllib.parse import urlparse

REMOTE_PATTERN = re.compile(r"\b(remote|work from home|wfh|anywhere|distributed)\b", re.I)

# Recognizes which applicant-tracking system a job URL belongs to. Used both to
# route direct polling and to auto-discover new company boards from any source.
ATS_PATTERNS = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([^/?#]+)", re.I)),
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
    return bool(REMOTE_PATTERN.search(f"{location or ''} {title or ''}"))


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
