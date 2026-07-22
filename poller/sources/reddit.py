"""Harvester for subreddit internship megathreads.

The broadest, noisiest source: r/csMajors and r/internships run recurring threads
where roles get posted as comments, often before (or instead of) anywhere else.
Comments are unstructured, so we mine each one for an application link plus an
internship signal and lean on the shared filters + URL dedupe to keep the bar high.

Reddit blocks unauthenticated API access, so this needs a (free) script-app
credential pair - set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET. Without them the
source skips itself, exactly like the other keyed search sources.
"""

import logging
import os
import re

import requests

from poller.normalize import detect_ats, make_posting, text_is_non_us
from shared.sectors import is_internship

log = logging.getLogger(__name__)

# Reddit requires a unique, descriptive User-Agent or it hard-blocks the request.
USER_AGENT = os.environ.get(
    "REDDIT_USER_AGENT", "python:internship-alert:v1 (github cross-company tracker)"
)

# Where to look, and how to find each subreddit's current megathread.
SUBREDDIT_QUERIES = [
    ("csMajors", "internship megathread"),
    ("internships", "megathread"),
    ("cscareerquestions", "internship thread"),
]

_URL = re.compile(r"https?://[^\s<>\")\]]+")
# Hosts that are never a job application (link shorteners/social/media).
_SKIP_HOSTS = ("reddit.com", "redd.it", "imgur", "youtube", "youtu.be", "twitter", "x.com")


def _access_token():
    """App-only OAuth token, or None when unconfigured/unreachable."""
    client_id = os.environ.get("REDDIT_CLIENT_ID")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET")
    if not client_id or not client_secret:
        log.info("reddit: no REDDIT_CLIENT_ID/SECRET configured, skipping")
        return None
    try:
        resp = requests.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=(client_id, client_secret),
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("access_token")
    except (requests.RequestException, ValueError) as exc:
        log.warning("reddit: auth failed: %s", exc)
        return None


def _get(url, token, params=None):
    try:
        resp = requests.get(
            url,
            params=params,
            headers={"Authorization": f"bearer {token}", "User-Agent": USER_AGENT},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as exc:
        log.warning("reddit: GET %s failed: %s", url, exc)
        return None


def _thread_urls(subreddit, query, token, limit=3):
    data = _get(
        f"https://oauth.reddit.com/r/{subreddit}/search",
        token,
        params={"q": query, "restrict_sr": 1, "sort": "new", "limit": limit, "raw_json": 1},
    )
    urls = []
    for child in (((data or {}).get("data") or {}).get("children")) or []:
        permalink = (child.get("data") or {}).get("permalink")
        if permalink:
            urls.append(f"https://oauth.reddit.com{permalink}")
    return urls


def _walk_comments(node):
    """Yield every comment body in a Reddit listing tree."""
    for child in (((node or {}).get("data") or {}).get("children")) or []:
        data = child.get("data") or {}
        if data.get("body"):
            yield data["body"]
        replies = data.get("replies")
        if isinstance(replies, dict):
            yield from _walk_comments(replies)


def _apply_url(text):
    for candidate in _URL.findall(text or ""):
        candidate = candidate.rstrip(").,")
        if not any(host in candidate for host in _SKIP_HOSTS):
            return candidate
    return ""


def _company_from(url):
    """Derive a company name from an application URL."""
    _, slug = detect_ats(url)
    if slug:
        return slug.replace("-", " ").title()
    host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
    return host.split(".")[0].replace("-", " ").title() or "via Reddit"


def fetch():
    token = _access_token()
    if not token:
        return []

    thread_urls = []
    for subreddit, query in SUBREDDIT_QUERIES:
        thread_urls.extend(_thread_urls(subreddit, query, token))

    postings = []
    seen_urls = set()
    for thread_url in thread_urls:
        listing = _get(thread_url, token, params={"limit": 500, "raw_json": 1})
        if not isinstance(listing, list):
            continue
        for node in listing:
            for body in _walk_comments(node):
                if not is_internship(body) or text_is_non_us(body):
                    continue
                url = _apply_url(body)
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                postings.append(
                    make_posting(
                        company_name=_company_from(url),
                        title=body.strip().splitlines()[0][:300],
                        url=url,
                        location="",
                        description=body[:2000],
                        source="reddit",
                    )
                )

    log.info("reddit: %s threads -> %s intern postings", len(thread_urls), len(postings))
    return postings
