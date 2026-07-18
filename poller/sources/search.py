"""Broad keyword-search sources.

These are not bounded by our company watchlist, so they catch internships at small
or private companies that never appear in a tracker repo or SEC filing. Adzuna and
USAJOBS need free API keys; Arbeitnow and RemoteOK need none and are skipped
gracefully if unreachable.
"""

import logging
import os
import time

from poller.net import get_json
from poller.normalize import make_posting

log = logging.getLogger(__name__)

# Sector-specific phrases paired with intern/co-op when querying keyword APIs.
SECTOR_QUERIES = [
    "software engineering intern",
    "data science intern",
    "machine learning intern",
    "semiconductor intern",
    "asic design intern",
    "vlsi intern",
    "rtl design intern",
    "computer architecture intern",
    "fpga intern",
    "embedded systems intern",
    "firmware intern",
    "power electronics intern",
    "electrical engineering intern",
    "power systems intern",
    "battery intern",
    "robotics intern",
    "controls engineering intern",
    "autonomous systems intern",
    "mechatronics intern",
    "hardware engineering intern",
    "engineering co-op",
]


# --------------------------------------------------------------------------
# Adzuna - https://developer.adzuna.com/ (free tier)
# --------------------------------------------------------------------------
def fetch_adzuna(country="us", max_pages=2):
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        log.info("adzuna: no credentials configured, skipping")
        return []

    postings = []
    for query in SECTOR_QUERIES:
        for page in range(1, max_pages + 1):
            data = get_json(
                f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}",
                params={
                    "app_id": app_id,
                    "app_key": app_key,
                    "results_per_page": 50,
                    "what": query,
                    "max_days_old": 30,
                    "content-type": "application/json",
                },
            )
            results = (data or {}).get("results") or []
            if not results:
                break

            for job in results:
                postings.append(
                    make_posting(
                        external_id=job.get("id"),
                        company_name=(job.get("company") or {}).get("display_name", ""),
                        title=job.get("title") or "",
                        url=job.get("redirect_url") or "",
                        location=(job.get("location") or {}).get("display_name", ""),
                        description=job.get("description") or "",
                        source="adzuna",
                        category_hint=(job.get("category") or {}).get("label", ""),
                        posted_at=job.get("created"),
                    )
                )
            time.sleep(0.3)  # free tier is rate limited

    log.info("adzuna: %s raw postings", len(postings))
    return postings


# --------------------------------------------------------------------------
# Arbeitnow - free, no key
# --------------------------------------------------------------------------
def fetch_arbeitnow(max_pages=5):
    postings = []
    for page in range(1, max_pages + 1):
        data = get_json("https://www.arbeitnow.com/api/job-board-api", params={"page": page})
        jobs = (data or {}).get("data") or []
        if not jobs:
            break

        for job in jobs:
            tags = job.get("tags") or []
            postings.append(
                make_posting(
                    external_id=job.get("slug"),
                    company_name=job.get("company_name") or "",
                    title=job.get("title") or "",
                    url=job.get("url") or "",
                    location=job.get("location") or "",
                    description=" ".join(str(t) for t in tags),
                    source="arbeitnow",
                    category_hint=", ".join(str(t) for t in tags[:3]),
                    posted_at=job.get("created_at"),
                    remote=job.get("remote"),
                )
            )

    log.info("arbeitnow: %s raw postings", len(postings))
    return postings


# --------------------------------------------------------------------------
# RemoteOK - free, no key
# --------------------------------------------------------------------------
def fetch_remoteok():
    data = get_json("https://remoteok.com/api")
    if not isinstance(data, list):
        return []

    postings = []
    for job in data:
        # The first element is a legal/attribution notice, not a job.
        if not isinstance(job, dict) or not job.get("position"):
            continue
        tags = job.get("tags") or []
        postings.append(
            make_posting(
                external_id=job.get("id"),
                company_name=job.get("company") or "",
                title=job.get("position") or "",
                url=job.get("url") or job.get("apply_url") or "",
                location=job.get("location") or "Remote",
                description=" ".join(str(t) for t in tags),
                source="remoteok",
                category_hint=", ".join(str(t) for t in tags[:3]),
                posted_at=job.get("epoch") or job.get("date"),
                remote=True,
            )
        )

    log.info("remoteok: %s raw postings", len(postings))
    return postings


# --------------------------------------------------------------------------
# USAJOBS - free key, US federal roles (incl. national labs, NASA, DoD)
# --------------------------------------------------------------------------
def fetch_usajobs(max_pages=3):
    api_key = os.environ.get("USAJOBS_API_KEY")
    email = os.environ.get("USAJOBS_EMAIL")
    if not api_key or not email:
        log.info("usajobs: no credentials configured, skipping")
        return []

    headers = {"Host": "data.usajobs.gov", "User-Agent": email, "Authorization-Key": api_key}
    postings = []

    for query in ["intern", "student trainee", "pathways intern"]:
        for page in range(1, max_pages + 1):
            data = get_json(
                "https://data.usajobs.gov/api/search",
                params={"Keyword": query, "ResultsPerPage": 100, "Page": page},
                headers=headers,
            )
            items = ((data or {}).get("SearchResult") or {}).get("SearchResultItems") or []
            if not items:
                break

            for item in items:
                job = item.get("MatchedObjectDescriptor") or {}
                locations = [
                    loc.get("LocationName", "")
                    for loc in job.get("PositionLocation") or []
                ]
                postings.append(
                    make_posting(
                        external_id=item.get("MatchedObjectId"),
                        company_name=job.get("OrganizationName") or "US Government",
                        title=job.get("PositionTitle") or "",
                        url=job.get("PositionURI") or "",
                        location=locations[:5],
                        description=(job.get("QualificationSummary") or "")[:2000],
                        source="usajobs",
                        posted_at=job.get("PublicationStartDate"),
                    )
                )
            time.sleep(0.3)

    log.info("usajobs: %s raw postings", len(postings))
    return postings


def fetch_all():
    postings = []
    for name, fetcher in [
        ("adzuna", fetch_adzuna),
        ("arbeitnow", fetch_arbeitnow),
        ("remoteok", fetch_remoteok),
        ("usajobs", fetch_usajobs),
    ]:
        try:
            postings.extend(fetcher() or [])
        except Exception as exc:
            log.warning("search: %s failed: %s", name, exc)
    return postings
