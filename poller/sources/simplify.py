"""Harvester for community-maintained internship tracker repos.

These repos publish a listings.json updated continuously by their own bots, each
entry carrying a direct application URL on whatever ATS the company actually uses.
That makes this both our largest posting source and our best company-discovery
source - we learn real, currently-valid board slugs instead of guessing them.
"""

import logging

from poller.net import get_json
from poller.normalize import make_posting

log = logging.getLogger(__name__)

SOURCE = "simplify"

FEEDS = [
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/vanshb03/Summer2026-Internships/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
]

# We want internships and co-ops of any season, but not new-grad/full-time rows
# that share the same feed format.
WANTED_TERM_MARKERS = ("summer", "fall", "spring", "winter", "co-op", "coop")


def _wanted_term(terms) -> bool:
    if not terms:
        return True  # no term info - let the internship keyword check decide
    joined = " ".join(str(t) for t in terms).lower()
    return any(marker in joined for marker in WANTED_TERM_MARKERS)


def fetch():
    """Return normalized postings from every configured feed."""
    postings = []
    seen_ids = set()

    for feed_url in FEEDS:
        data = get_json(feed_url, timeout=60)
        if not isinstance(data, list):
            log.warning("simplify: no usable data from %s", feed_url)
            continue

        kept = 0
        for row in data:
            if not isinstance(row, dict):
                continue
            if not row.get("active", True):
                continue
            if row.get("is_visible") is False:
                continue
            if not _wanted_term(row.get("terms")):
                continue

            url = row.get("url") or ""
            listing_id = row.get("id") or url
            if not url or listing_id in seen_ids:
                continue
            seen_ids.add(listing_id)

            terms = row.get("terms") or []
            postings.append(
                make_posting(
                    external_id=listing_id,
                    company_name=row.get("company_name") or "",
                    title=row.get("title") or "",
                    url=url,
                    location=row.get("locations") or [],
                    source=SOURCE,
                    category_hint=row.get("category") or "",
                    term="; ".join(str(t) for t in terms),
                    posted_at=row.get("date_posted") or row.get("date_updated"),
                )
            )
            kept += 1

        log.info("simplify: %s rows -> %s kept from %s", len(data), kept, feed_url)

    return postings
