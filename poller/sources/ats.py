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


class BoardResult(list):
    def __init__(self, rows=(), *, complete=True, host=None):
        super().__init__(rows)
        self.complete = complete
        self.host = host


INTERN_SEARCH_TERMS = ["intern", "co-op"]

# Deep enough for the largest employers without paging a whole career site.
WORKDAY_MAX_RESULTS = 1000


# --------------------------------------------------------------------------
# Greenhouse
# --------------------------------------------------------------------------
def fetch_greenhouse(slug: str, company_name: str = None):
    data = get_json(
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    )
    if not isinstance(data, dict) or "jobs" not in data:
        return BoardResult(complete=False)

    return BoardResult(
        [
            make_posting(
                external_id=job.get("id"),
                company_name=company_name or job.get("company_name") or slug,
                title=job.get("title") or "",
                url=job.get("absolute_url") or "",
                location=(job.get("location") or {}).get("name", ""),
                source="greenhouse",
                description=job.get("content") or "",
                posted_at=job.get("first_published") or job.get("updated_at"),
            )
            for job in data.get("jobs") or []
        ]
    )


# --------------------------------------------------------------------------
# Lever
# --------------------------------------------------------------------------
def fetch_lever(slug: str, company_name: str = None):
    data = get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if not isinstance(data, list):
        return BoardResult(complete=False)

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
                description=job.get("descriptionPlain") or job.get("description") or "",
                category_hint=categories.get("team")
                or categories.get("department")
                or "",
                term=job.get("workplaceType") or "",
                posted_at=job.get("createdAt"),
            )
        )
    return BoardResult(postings)


# --------------------------------------------------------------------------
# Ashby
# --------------------------------------------------------------------------
def fetch_ashby(slug: str, company_name: str = None):
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if not isinstance(data, dict) or "jobs" not in data:
        return BoardResult(complete=False)

    postings = []
    for job in data.get("jobs") or []:
        locations = [job.get("location") or ""]
        locations += [
            (sec or {}).get("location", "")
            for sec in job.get("secondaryLocations") or []
        ]
        postings.append(
            make_posting(
                external_id=job.get("id"),
                company_name=company_name or job.get("companyName") or slug,
                title=job.get("title") or "",
                url=job.get("jobUrl") or job.get("applyUrl") or "",
                location=[loc for loc in locations if loc],
                source="ashby",
                description=job.get("descriptionPlain")
                or job.get("descriptionHtml")
                or "",
                category_hint=job.get("department") or job.get("team") or "",
                # Ashby types intern roles explicitly - a strong internship signal.
                term=job.get("employmentType") or "",
                posted_at=job.get("publishedAt") or job.get("updatedAt"),
                remote=job.get("isRemote"),
            )
        )
    return BoardResult(postings)


# --------------------------------------------------------------------------
# Workday
# --------------------------------------------------------------------------
def fetch_workday(tenant: str, site: str, company_name: str = None, wd_num: str = None):
    """Workday needs a POST search per tenant/site, paginated 20 at a time.

    The tenant's numbered host (wd1/wd3/wd5/...) varies per customer, so when it
    isn't known we probe the common ones and keep the first that answers.
    """
    if not tenant or not site:
        return BoardResult(complete=False)

    hosts = ([wd_num] if wd_num else []) + [
        h for h in ["wd1", "wd3", "wd5", "wd2", "wd101", "wd12"] if h != wd_num
    ]
    for host in hosts:
        base = f"https://{tenant}.{host}.myworkdayjobs.com"
        endpoint = f"{base}/wday/cxs/{tenant}/{site}/jobs"
        postings, seen_paths = [], set()
        found_host, complete = False, True
        for term in INTERN_SEARCH_TERMS:
            offset = 0
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
                        timeout=30,
                    )
                    resp.raise_for_status()
                    payload = resp.json()
                    if not isinstance(payload, dict) or "jobPostings" not in payload:
                        raise ValueError("Invalid board response")
                except (requests.RequestException, ValueError):
                    complete = False
                    break
                found_host = True  # empty valid boards also establish the host
                jobs = payload.get("jobPostings") or []
                for job in jobs:
                    path = job.get("externalPath") or ""
                    if not path or path in seen_paths:
                        continue
                    seen_paths.add(path)
                    postings.append(
                        make_posting(
                            external_id=(job.get("bulletFields") or [None])[0],
                            company_name=company_name or tenant,
                            title=job.get("title") or "",
                            url=f"{base}/en-US/{site}{path}",
                            location=job.get("locationsText") or "",
                            source="workday",
                        )
                    )
                offset += len(jobs)
                if len(jobs) < 20 or offset >= payload.get(
                    "total", WORKDAY_MAX_RESULTS + 1
                ):
                    break
            else:
                complete = False  # cap reached; absence is not closure evidence
            if not found_host:
                break
        if found_host:
            return BoardResult(postings, complete=complete, host=host)
    return BoardResult(complete=False)


def fetch_smartrecruiters(slug, company_name=None):
    postings, offset = [], 0
    while offset < 10000:
        data = get_json(
            f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
            params={"limit": 100, "offset": offset},
        )
        if not isinstance(data, dict) or "content" not in data:
            return BoardResult(postings, complete=False)
        jobs = data["content"]
        for j in jobs:
            loc = j.get("location") or {}
            # Listing payload omits the body; detail API only for intern candidates.
            body = ""
            from shared.sectors import is_internship

            if is_internship(j.get("name", "")):
                detail = (
                    get_json(
                        f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{j['id']}"
                    )
                    or {}
                )
                sections = (detail.get("jobAd") or {}).get("sections") or {}
                body = " ".join(
                    v.get("text", "") for v in sections.values() if isinstance(v, dict)
                )
            postings.append(
                make_posting(
                    company_name=company_name or slug,
                    title=j.get("name", ""),
                    external_id=j.get("id"),
                    url=f"https://jobs.smartrecruiters.com/{slug}/{j.get('id', '')}",
                    location=", ".join(
                        str(loc[k]) for k in ("city", "region", "country") if loc.get(k)
                    ),
                    source="smartrecruiters",
                    description=body,
                    posted_at=j.get("releasedDate"),
                )
            )
        offset += len(jobs)
        if not jobs or offset >= data.get("totalFound", offset):
            return BoardResult(postings)
    return BoardResult(postings, complete=False)


def fetch_workable(slug, company_name=None):
    data = get_json(
        f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"
    )
    if not isinstance(data, dict) or "jobs" not in data:
        return BoardResult(complete=False)
    return BoardResult(
        [
            make_posting(
                company_name=company_name or slug,
                title=j.get("title", ""),
                external_id=j.get("shortcode"),
                url=j.get("url") or j.get("application_url", ""),
                location=", ".join(
                    str(j[k]) for k in ("city", "state", "country") if j.get(k)
                ),
                source="workable",
                description=j.get("description", ""),
                remote=j.get("telecommuting"),
            )
            for j in data["jobs"]
        ]
    )


def fetch_recruitee(slug, company_name=None):
    data = get_json(f"https://{slug}.recruitee.com/api/offers/")
    if not isinstance(data, dict) or "offers" not in data:
        return BoardResult(complete=False)
    return BoardResult(
        [
            make_posting(
                company_name=company_name or slug,
                title=j.get("title", ""),
                external_id=j.get("id"),
                url=j.get("careers_url")
                or f"https://{slug}.recruitee.com/o/{j.get('slug', '')}",
                location=j.get("location", ""),
                source="recruitee",
                description=(j.get("description") or "")
                + " "
                + (j.get("requirements") or ""),
            )
            for j in data["offers"]
        ]
    )


FETCHERS = {
    "smartrecruiters": lambda c: fetch_smartrecruiters(c.slug, c.name),
    "workable": lambda c: fetch_workable(c.slug, c.name),
    "recruitee": lambda c: fetch_recruitee(c.slug, c.name),
    "greenhouse": lambda c: fetch_greenhouse(c.slug, c.name),
    "lever": lambda c: fetch_lever(c.slug, c.name),
    "ashby": lambda c: fetch_ashby(c.slug, c.name),
    "workday": lambda c: fetch_workday(
        c.workday_tenant, c.workday_site, c.name, c.workday_host
    ),
}


def fetch_for_company(company):
    """Poll one company's board. Returns [] for companies with no usable ATS."""
    fetcher = FETCHERS.get(company.ats_type)
    if not fetcher:
        return BoardResult(complete=False)
    try:
        return fetcher(company)
    except Exception as exc:  # one bad board must never abort the whole run
        log.warning("ats: %s (%s) failed: %s", company.name, company.ats_type, exc)
        return BoardResult(complete=False)
