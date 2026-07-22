"""Harvester for the monthly "Ask HN: Who is hiring?" thread.

HN's hiring thread is where a lot of startups post before they ever reach a
tracker repo - and many never reach one at all. Each top-level comment is roughly
one company, conventionally formatted "Company | Role | Location | ... | <link>",
which is structured enough to mine for the intern/co-op roles.
"""

import html
import logging
import re

from poller.net import get_json
from poller.normalize import make_posting, text_is_non_us
from shared.sectors import is_internship

log = logging.getLogger(__name__)

HN_SEARCH = "https://hn.algolia.com/api/v1/search"
HN_SEARCH_BY_DATE = "https://hn.algolia.com/api/v1/search_by_date"

_TAG = re.compile(r"<[^>]+>")
_HREF = re.compile(r'href=["\'](https?://[^"\']+)["\']', re.I)
_URL = re.compile(r"https?://[^\s<>\")]+")


def _latest_thread_id():
    """objectID of the most recent 'Ask HN: Who is hiring?' story, or None."""
    data = get_json(
        HN_SEARCH_BY_DATE,
        params={"tags": "story,author_whoishiring", "query": "hiring", "hitsPerPage": 10},
    )
    for hit in (data or {}).get("hits") or []:
        if "who is hiring" in (hit.get("title") or "").lower():
            return hit.get("objectID")
    return None


def _comment_text(raw_html: str) -> str:
    """HN comment HTML -> plain text, with paragraph breaks preserved."""
    text = (raw_html or "").replace("<p>", "\n").replace("</p>", "\n")
    return html.unescape(_TAG.sub(" ", text)).strip()


def _apply_url(raw_html: str, text: str):
    """Best application link in a comment: a real href beats a bare URL."""
    for candidate in _HREF.findall(raw_html or "") + _URL.findall(text or ""):
        if "news.ycombinator.com" not in candidate and "ycombinator.com/item" not in candidate:
            return candidate.rstrip(").,")
    return ""


def fetch(max_comments=1000):
    thread_id = _latest_thread_id()
    if not thread_id:
        log.info("hackernews: no current 'who is hiring' thread found")
        return []

    data = get_json(
        HN_SEARCH,
        params={"tags": f"comment,story_{thread_id}", "hitsPerPage": max_comments},
    )
    hits = (data or {}).get("hits") or []

    postings = []
    for hit in hits:
        raw = hit.get("comment_text") or ""
        text = _comment_text(raw)
        if not text or not is_internship(text):
            continue

        lines = [l.strip() for l in text.splitlines() if l.strip()]
        first_line = lines[0]
        # "Who is hiring" posts follow "Company | Role | Location | ...". Requiring
        # that header both keeps company/location parseable and filters out the
        # prose comments where "intern" only appears in passing.
        if "|" not in first_line:
            continue
        # The header carries the location - veto a clearly-foreign role there.
        if text_is_non_us(first_line):
            continue

        url = _apply_url(raw, text)
        if not url:
            continue  # nothing to apply to / dedupe on

        segments = [s.strip() for s in first_line.split("|") if s.strip()]
        company = (segments[0] if segments else "via Hacker News")[:200]
        # Prefer a header segment that names the internship, then a short role
        # line elsewhere, then a generic fallback so the alert still reads sensibly.
        role = next((s for s in segments[1:] if is_internship(s)), None)
        role = role or next((l for l in lines if is_internship(l) and len(l) < 80), None)
        title = (role or f"{company} internship")[:300]

        postings.append(
            make_posting(
                external_id=hit.get("objectID"),
                company_name=company,
                title=title,
                url=url,
                location="",
                description=text[:2000],
                source="hackernews",
                posted_at=hit.get("created_at_i"),
            )
        )

    log.info("hackernews: %s comments -> %s intern postings", len(hits), len(postings))
    return postings
