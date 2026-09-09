"""Public career-site search integrations, verified against the sites' own APIs.

Capped searches are discovery sources, never authoritative closure snapshots.
"""

from urllib.parse import urljoin
from poller.net import get_json
from poller.normalize import make_posting, is_us_location
from shared.sectors import is_internship


def fetch_amazon(max_results=500):
    rows = []
    for offset in range(0, max_results, 100):
        data = get_json(
            "https://www.amazon.jobs/en/search.json",
            params={
                "base_query": "intern",
                "offset": offset,
                "result_limit": 100,
                "sort": "recent",
                "normalized_country_code[]": "USA",
            },
        )
        if not isinstance(data, dict) or "jobs" not in data:
            break
        jobs = data["jobs"]
        for j in jobs:
            rows.append(
                make_posting(
                    company_name="Amazon",
                    title=j.get("title", ""),
                    external_id=j.get("id"),
                    url=urljoin(
                        "https://www.amazon.jobs",
                        j.get("job_path") or f"/en/jobs/{j.get('id', '')}",
                    ),
                    source="amazon",
                    location=j.get("location", ""),
                    description="\n".join(
                        j.get(k) or ""
                        for k in [
                            "description",
                            "basic_qualifications",
                            "preferred_qualifications",
                        ]
                    ),
                    posted_at=j.get("posted_date"),
                    term="internship" if j.get("is_intern") else "",
                )
            )
        if len(jobs) < 100 or offset + len(jobs) >= data.get("hits", max_results):
            break
    return rows


def fetch_microsoft(max_results=500):
    rows = []
    start = 0
    seen = set()
    while start < max_results:
        data = get_json(
            "https://apply.careers.microsoft.com/api/pcsx/search",
            params={
                "domain": "microsoft.com",
                "query": "intern",
                "start": start,
                "sort_by": "timestamp",
            },
        )
        payload = (data or {}).get("data") or {}
        jobs = payload.get("positions") or []
        if not jobs:
            break
        added = 0
        for j in jobs:
            id = j.get("id")
            if not id or id in seen:
                continue
            seen.add(id)
            added += 1
            locations = "; ".join(j.get("locations") or [])
            if not is_us_location(locations) or not is_internship(j.get("name", "")):
                continue
            detail = get_json(
                "https://apply.careers.microsoft.com/api/pcsx/position_details",
                params={"domain": "microsoft.com", "position_id": id},
            )
            body = ((detail or {}).get("data") or {}).get("jobDescription", "")
            rows.append(
                make_posting(
                    company_name="Microsoft",
                    title=j.get("name", ""),
                    external_id=id,
                    url=urljoin(
                        "https://apply.careers.microsoft.com",
                        j.get("positionUrl") or f"/careers/job/{id}",
                    ),
                    source="microsoft",
                    location=locations,
                    description=body,
                    posted_at=j.get("postedTs"),
                    remote=j.get("workLocationOption") == "remote",
                )
            )
        start += len(jobs)
        if not added or start >= payload.get("count", max_results):
            break
    return rows
