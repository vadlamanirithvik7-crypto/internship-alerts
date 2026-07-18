"""Resolve a company name to a real, live-validated job board.

Nothing here guesses and keeps. Every candidate slug is probed against the actual
ATS API and only retained if the API returns a real board with real jobs. A name we
can't resolve stays in the watchlist as an unresolved company - still useful, since
keyword search sources match on company name.
"""

import logging
import re
import time

from poller.net import get_json
from shared.db import utcnow

log = logging.getLogger(__name__)

# Probing every name against every ATS is the slow part of a poll, so keep the
# candidate set tight and ordered by how likely each form is to be the real slug.
MAX_CANDIDATES = 4


def slug_candidates(name: str):
    """Generate plausible board slugs for a company name, best guess first."""
    base = re.sub(r"[^\w\s-]", "", (name or "").lower()).strip()
    base = re.sub(r"\s+", " ", base)
    if not base:
        return []

    words = base.split()
    candidates = [
        "".join(words),              # advancedmicrodevices
        "-".join(words),             # advanced-micro-devices
        words[0] if words else "",   # advanced
    ]
    if len(words) > 1:
        candidates.append("".join(words[:2]))  # advancedmicro

    seen, ordered = set(), []
    for candidate in candidates:
        if candidate and candidate not in seen and len(candidate) > 2:
            seen.add(candidate)
            ordered.append(candidate)
    return ordered[:MAX_CANDIDATES]


def _probe_greenhouse(slug):
    data = get_json(
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", retries=0, timeout=15
    )
    return isinstance(data, dict) and bool(data.get("jobs"))


def _probe_lever(slug):
    data = get_json(
        f"https://api.lever.co/v0/postings/{slug}?mode=json", retries=0, timeout=15
    )
    return isinstance(data, list) and len(data) > 0


def _probe_ashby(slug):
    data = get_json(
        f"https://api.ashbyhq.com/posting-api/job-board/{slug}", retries=0, timeout=15
    )
    return isinstance(data, dict) and bool(data.get("jobs"))


PROBES = [
    ("greenhouse", _probe_greenhouse),
    ("lever", _probe_lever),
    ("ashby", _probe_ashby),
]


def resolve(name: str, *, pause: float = 0.1):
    """Return (ats_type, slug) if a live board is confirmed, else (None, None)."""
    for slug in slug_candidates(name):
        for ats, probe in PROBES:
            try:
                if probe(slug):
                    log.info("resolver: %s -> %s/%s", name, ats, slug)
                    return ats, slug
            except Exception:
                continue
            if pause:
                time.sleep(pause)
    return None, None


def resolve_companies(session_db, companies, *, limit=None):
    """Attempt resolution for unresolved companies. Returns count resolved."""
    resolved = 0
    for company in companies[: limit or len(companies)]:
        ats, slug = resolve(company.name)
        company.last_checked_at = utcnow()
        if ats:
            company.ats_type = ats
            company.slug = slug
            company.resolved = True
            resolved += 1
    session_db.flush()
    return resolved
