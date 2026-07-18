"""Direct polling of company job boards on the major free ATS APIs.

All four are public, keyless JSON endpoints. Polling a company's own board is the
fastest path from "company posted a job" to "you get an alert" - usually faster
than aggregators pick it up.
"""

import logging

import requests

from poller.net import USER_AGENT, get_json, session
from poller.normalize import make_posting

log = logging.getLogger(__name__)

INTERN_SEARCH_TERMS = ["intern", "co-op"]

# Deep enough for the largest employers without paging a whole career site.
WORKDAY_MAX_RESULTS = 1000


# --------------------------------------------------------------------------
# Greenhouse
# --------------------------------------------------------------------------
def fetch_greenhouse(slug: str, company_name: str = None):
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    if not isinstance(data, dict) or "jobs" not in data:
        return []

    return [
        make_posting(
            external_id=job.get("id"),
            company_name=company_name or job.get("company_name") or slug,
            title=job.get("title") or "",
            url=job.get("absolute_url") or "",
            location=(job.get("location") or {}).get("name", ""),
            source="greenhouse",
            posted_at=job.get("first_published") or job.get("updated_at"),
        )
        for job in data.get("jobs") or []
    ]


# --------------------------------------------------------------------------
# Lever
# --------------------------------------------------------------------------
def fetch_lever(slug: str, company_name: str = None):
    data = get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if not isinstance(data, list):
        return []

    postings = []
    for job in data:
        categories = job.get("categories") or {}
        postings.append(
            make_posting(
                external_id=job.get("id"),
                company_name=company_name or slug,
                title=job.get("text") or "",
                url=job.get("hostedUrl") or job.get("applyUrl") or "",
                location=categories.get("location") or "",
                source="lever",
                category_hint=categories.get("team") or categories.get("department") or "",
                term=job.get("workplaceType") or "",
                posted_at=job.get("createdAt"),
            )
        )
    return postings


# --------------------------------------------------------------------------
# Ashby
# --------------------------------------------------------------------------
def fetch_ashby(slug: str, company_name: str = None):
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if not isinstance(data, dict) or "jobs" not in data:
        return []

    postings = []
    for job in data.get("jobs") or []:
        locations = [job.get("location") or ""]
        locations += [
            (sec or {}).get("location", "") for sec in job.get("secondaryLocations") or []
        ]
        postings.append(
            make_posting(
                external_id=job.get("id"),
                company_name=company_name or job.get("companyName") or slug,
                title=job.get("title") or "",
                url=job.get("jobUrl") or job.get("applyUrl") or "",
                location=[loc for loc in locations if loc],
                source="ashby",
                category_hint=job.get("department") or job.get("team") or "",
                # Ashby types intern roles explicitly - a strong internship signal.
                term=job.get("employmentType") or "",
                posted_at=job.get("publishedAt") or job.get("updatedAt"),
                remote=job.get("isRemote"),
            )
        )
    return postings


# --------------------------------------------------------------------------
# Workday
# --------------------------------------------------------------------------
def fetch_workday(tenant: str, site: str, company_name: str = None, wd_num: str = None):
    """Workday needs a POST search per tenant/site, paginated 20 at a time.

    The tenant's numbered host (wd1/wd3/wd5/...) varies per customer, so when it
    isn't known we probe the common ones and keep the first that answers.
    """
    if not tenant or not site:
        return []

    hosts = [wd_num] if wd_num else ["wd1", "wd3", "wd5", "wd2", "wd101", "wd12"]
    postings = []
    seen_paths = set()  # the two search terms overlap heavily

    for host in hosts:
        base = f"https://{tenant}.{host}.myworkdayjobs.com"
        endpoint = f"{base}/wday/cxs/{tenant}/{site}/jobs"
        found_host = False

        for term in INTERN_SEARCH_TERMS:
            offset = 0
            # Large employers legitimately post hundreds of intern roles (NVIDIA
            # alone returns ~900), so page deep enough not to truncate them.
            while offset < WORKDAY_MAX_RESULTS:
                try:
                    resp = session().post(
                        endpoint,
                        json={
                            "appliedFacets": {},
                            "limit": 20,
                            "offset": offset,
                            "searchText": term,
                        },
                        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
                        timeout=30,
                    )
                    if resp.status_code != 200:
                        break
                    payload = resp.json()
                except (requests.RequestException, ValueError):
                    break

                jobs = payload.get("jobPostings") or []
                if not jobs:
                    break
                found_host = True

                for job in jobs:
                    path = job.get("externalPath") or ""
                    if path in seen_paths:
                        continue
                    seen_paths.add(path)
                    postings.append(
                        make_posting(
                            external_id=(job.get("bulletFields") or [None])[0],
                            company_name=company_name or tenant,
                            title=job.get("title") or "",
                            url=f"{base}/en-US/{site}{path}" if path else base,
                            location=job.get("locationsText") or "",
                            source="workday",
                            # postedOn is relative text ("Posted 30+ Days Ago"), not a
                            # date - first_seen_at is what drives new-posting alerts.
                            posted_at=None,
                        )
                    )

                if len(jobs) < 20:
                    break
                offset += 20

        if found_host:
            break

    return postings


FETCHERS = {
    "greenhouse": lambda c: fetch_greenhouse(c.slug, c.name),
    "lever": lambda c: fetch_lever(c.slug, c.name),
    "ashby": lambda c: fetch_ashby(c.slug, c.name),
    "workday": lambda c: fetch_workday(c.workday_tenant, c.workday_site, c.name),
}


def fetch_for_company(company):
    """Poll one company's board. Returns [] for companies with no usable ATS."""
    fetcher = FETCHERS.get(company.ats_type)
    if not fetcher:
        return []
    try:
        return fetcher(company) or []
    except Exception as exc:  # one bad board must never abort the whole run
        log.warning("ats: %s (%s) failed: %s", company.name, company.ats_type, exc)
        return []
