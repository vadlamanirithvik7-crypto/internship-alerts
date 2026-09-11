"""Distinguish original employer applications from aggregator listings."""
from urllib.parse import urlsplit
from functools import lru_cache

AGGREGATORS = {"jobright.ai", "simplify.jobs", "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com", "adzuna.com", "arbeitnow.com", "remoteok.com", "zapply.jobs", "joinhandshake.com", "wellfound.com", "dice.com", "talent.com", "zippia.com", "lensa.com", "jobgether.com", "tealhq.com"}


def is_aggregator(url):
    try:
        host = (urlsplit(url or "").hostname or "").lower().rstrip(".")
    except (ValueError, TypeError):
        return False
    return any(host == domain or host.endswith("." + domain) for domain in AGGREGATORS)


def employer_url(urls):
    """Prefer a provided employer link; never guess a destination or requisition."""
    valid = []
    for url in urls:
        parsed = urlsplit(url or "")
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            continue
        valid.append(url)
    return next((url for url in valid if not is_aggregator(url)), valid[0] if valid else "")


# Discovery URLs remain immutable so repairs cannot orphan application history,
# alert receipts, or sheet rows. These helpers choose a separate destination.
def direct_url(url):
    import ipaddress
    try:
        p = urlsplit(url or "")
        host = (p.hostname or "").lower().rstrip(".")
        if p.scheme not in {"http", "https"} or not host or p.username or p.password or is_aggregator(url):
            return ""
        if "." not in host or host.endswith((".local", ".internal", ".localhost")) or "\\" in url:
            return ""
        try:
            if not ipaddress.ip_address(host).is_global:
                return ""
        except ValueError:
            pass
        return url
    except (ValueError, TypeError):
        return ""


def application_url(posting):
    return direct_url(posting.url) or direct_url(getattr(posting, "application_url", None))


def radar_url(posting):
    import os
    origin = os.environ.get("APP_BASE_URL", "https://internship-alerts-1412.onrender.com").rstrip("/")
    return f"{origin}/jobs/{posting.id}"


def notification_url(posting):
    # Unknown links lead back to Radar, where the destination can be repaired.
    return application_url(posting) or radar_url(posting)


def employer_search_url(posting):
    from urllib.parse import urlencode
    query = f'"{posting.company_name}" "{posting.title}" careers -site:jobright.ai -site:linkedin.com -site:indeed.com'
    return "https://www.google.com/search?" + urlencode({"q": query})


def _company_key(value):
    import re
    value = re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()
    return re.sub(r"\s+(incorporated|inc|llc|ltd|corporation|corp)$", "", value)


def _title_key(value):
    import re
    value = re.sub(r"[^a-z0-9+#]+", " ", (value or "").lower())
    return " ".join("intern" if word == "internship" else word for word in value.split())


def _match_title_key(value):
    # Both records must independently pass Summer 2027 eligibility; ATS titles
    # may leave the season to their description. Preserve all other role words.
    return " ".join(w for w in _title_key(value).split() if w not in {"summer", "2027"})


def _same_location(a, b):
    import re
    def norm(value):
        return re.sub(r"[^a-z0-9]+", " ", re.sub(r"\b(united states(?: of america)?|usa|us)\b", "", (value or "").lower())).strip()
    left, right = norm(a), norm(b)
    if not left or not right:
        return False
    if left == right:
        return True
    # A single verified employer requisition can cover multiple cities. Require
    # the complete city/state phrase, not a loose city-name or fuzzy match.
    return ((len(left.split()) >= 2 and f" {left} " in f" {right} ")
            or (len(right.split()) >= 2 and f" {right} " in f" {left} "))


def match_employer(posting, candidates):
    """Only a unique same-company, same-title, same-location requisition wins."""
    from poller.normalize import canonical_url
    urls = {}
    for other in candidates:
        if other.closed_at or not other.target_eligible or not direct_url(other.url):
            continue
        if (_company_key(posting.company_name) == _company_key(other.company_name)
                and _match_title_key(posting.title) == _match_title_key(other.title)
                and _same_location(posting.location, other.location)):
            urls[canonical_url(other.url)] = other.url
    return next(iter(urls.values())) if len(urls) == 1 else ""


def company_careers_url(company, title=""):
    """Build a careers landing page only from an already resolved board."""
    import re
    from urllib.parse import quote, urlencode
    if not company or not company.resolved:
        return ""
    slug = company.slug or ""
    if not re.fullmatch(r"[\w-]+", slug):
        return ""
    bases = {
        "greenhouse": "https://job-boards.greenhouse.io/",
        "lever": "https://jobs.lever.co/", "ashby": "https://jobs.ashbyhq.com/",
        "smartrecruiters": "https://careers.smartrecruiters.com/",
        "workable": "https://apply.workable.com/",
    }
    if company.ats_type in bases:
        return bases[company.ats_type] + quote(slug)
    if company.ats_type == "recruitee":
        return f"https://{slug}.recruitee.com/"
    if company.ats_type == "workday":
        tenant, host, site = company.workday_tenant, company.workday_host, company.workday_site
        if (re.fullmatch(r"[a-zA-Z0-9-]+", tenant or "") and re.fullmatch(r"wd\d+", host or "")
                and re.fullmatch(r"[\w-]+", site or "")):
            return f"https://{tenant}.{host}.myworkdayjobs.com/{quote(site)}?" + urlencode({"q": title})
    return ""


def jobright_destinations(url, company_name, title):
    """Read public share metadata; never sign into Jobright or submit applications."""
    import re
    from poller.net import get_json, get_text
    p = urlsplit(url or "")
    match = re.fullmatch(r"/jobs/info/([a-fA-F0-9]{24})/?", p.path)
    if (p.hostname or "").lower() not in {"jobright.ai", "www.jobright.ai"} or not match:
        return {}
    job_id = match.group(1)
    data = get_json(f"https://jobright.ai/swan/share/job/{job_id}", timeout=5, retries=0)
    detail = ((data.get("result") or {}).get("jobDetail") or {}) if isinstance(data, dict) and data.get("success") is True else {}
    if not detail:
        # Older public shares can fail at the API while their public HTML still
        # includes company metadata. Read only that page's structured job data.
        import json
        from html.parser import HTMLParser
        class PageData(HTMLParser):
            def __init__(self):
                super().__init__()
                self.capture, self.parts = False, []
            def handle_starttag(self, tag, attrs):
                if tag == "script" and dict(attrs).get("id") == "__NEXT_DATA__":
                    self.capture = True
            def handle_endtag(self, tag):
                if tag == "script":
                    self.capture = False
            def handle_data(self, value):
                if self.capture:
                    self.parts.append(value)
        page = PageData()
        page.feed(get_text(f"https://jobright.ai/jobs/info/{job_id}", timeout=5, retries=0) or "")
        try:
            detail = json.loads("".join(page.parts))["props"]["pageProps"]["dataSource"]
        except (ValueError, KeyError, TypeError):
            return {}
    job, company = detail.get("jobResult") or {}, detail.get("companyResult") or {}
    if (job.get("jobId") != job_id or _company_key(company.get("companyName")) != _company_key(company_name)
            or _title_key(job.get("jobTitle")) != _title_key(title)):
        return {}
    exact = ""
    if job.get("isCompanySiteLink") is True:
        exact = direct_url(job.get("originalUrl")) or direct_url(job.get("applyLink"))
    return {"application_url": exact, "employer_site_url": direct_url(company.get("companyURL"))}


@lru_cache(maxsize=1)
def _employer_sites():
    import json
    from pathlib import Path
    # Public company-site evidence, never applicant or account data.
    return json.loads(Path(__file__).with_name("employer_sites.json").read_text())


def known_employer_site(name):
    entry = _employer_sites().get(_company_key(name), {})
    return direct_url(entry.get("url"))


def repair_links(db, *, limit=60, posting_id=None, fetch=True):
    from collections import defaultdict
    from concurrent.futures import ThreadPoolExecutor
    from datetime import timedelta
    from sqlalchemy import select, or_
    from sqlalchemy.orm import load_only
    from shared.db import Posting, Company, utcnow
    rows = list(db.scalars(select(Posting).where(or_(Posting.target_eligible.is_(True), Posting.applied_at.is_not(None), Posting.id == posting_id)).options(
        load_only(Posting.id, Posting.url, Posting.company_name, Posting.company_id, Posting.title, Posting.location,
                  Posting.target_eligible, Posting.closed_at, Posting.application_url, Posting.employer_site_url,
                  Posting.application_link_checked_at, Posting.applied_at, Posting.status))))
    catalog = defaultdict(list)
    for row in rows:
        if direct_url(row.url):
            catalog[(_company_key(row.company_name), _match_title_key(row.title))].append(row)
    pending = [p for p in rows if is_aggregator(p.url) and (posting_id is None or p.id == posting_id)]
    companies = {c.id: c for c in db.scalars(select(Company).where(Company.id.in_({p.company_id for p in pending if p.company_id})))}
    resolved = 0
    for p in pending:
        matched = match_employer(p, catalog[(_company_key(p.company_name), _match_title_key(p.title))])
        if matched and p.application_url != matched:
            p.application_url = matched
            resolved += 1
        careers = company_careers_url(companies.get(p.company_id), p.title)
        if careers:
            p.employer_site_url = careers
        elif not p.employer_site_url:
            p.employer_site_url = known_employer_site(p.company_name)
    cutoff = utcnow() - timedelta(days=1)
    due = sorted((p for p in pending if not application_url(p) and not p.employer_site_url
                  and (not p.application_link_checked_at or p.application_link_checked_at < cutoff)),
                 key=lambda p: (p.application_link_checked_at or cutoff - timedelta(days=1), -p.id))[:limit]
    if fetch and due:
        args = [(p.url, p.company_name, p.title) for p in due]
        def resolve(args):
            try:
                return jobright_destinations(*args)
            except Exception:
                # Bad public source data must not stop ingestion or the app page.
                return {}
        with ThreadPoolExecutor(max_workers=6) as pool:
            for p, result in zip(due, pool.map(resolve, args)):
                p.application_link_checked_at = utcnow()
                if result.get("application_url"):
                    p.application_url = result["application_url"]
                    resolved += 1
                if result.get("employer_site_url") and not p.employer_site_url:
                    p.employer_site_url = result["employer_site_url"]
    db.flush()
    return {"aggregator_listings": len(pending), "new_exact_links": resolved,
            "exact_links": sum(bool(application_url(p)) for p in pending),
            "company_site_fallback": sum(not application_url(p) and bool(direct_url(p.employer_site_url)) for p in pending),
            "unresolved": sum(not application_url(p) and not direct_url(p.employer_site_url) for p in pending)}


if __name__ == "__main__":
    import argparse
    from shared.db import get_engine, init_db, get_session_factory
    from poller.locking import poller_lock
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=2000)
    parser.add_argument("--scan-boards", action="store_true")
    parser.add_argument("--audit", action="store_true")
    args = parser.parse_args()
    engine = init_db(get_engine())
    if args.audit:
        import json
        from pathlib import Path
        from sqlalchemy import select
        from shared.db import Posting
        with get_session_factory(engine)() as db:
            rows = [{"company_name": p.company_name, "title": p.title, "url": p.url, "location": p.location}
                    for p in db.scalars(select(Posting).where(Posting.target_eligible.is_(True)))
                    if is_aggregator(p.url) and not application_url(p)]
        Path("link-candidates.json").write_text(json.dumps(rows))
        print({"public_listings_to_resolve": len(rows)})
        raise SystemExit(0)
    with poller_lock(engine), get_session_factory(engine)() as db:
        if args.scan_boards:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            from sqlalchemy import select
            from shared.db import Posting, Company
            from poller.sources import ats
            from poller.store import upsert_postings
            from types import SimpleNamespace
            ids = {p.company_id for p in db.scalars(select(Posting).where(Posting.target_eligible.is_(True)))
                   if is_aggregator(p.url) and p.company_id}
            companies = list(db.scalars(select(Company).where(Company.id.in_(ids), Company.resolved.is_(True), Company.ats_type.in_(ats.FETCHERS))))
            # Snapshot before threads; no ORM access outside the owning thread.
            snapshots = [SimpleNamespace(**{k:getattr(c,k) for k in ("name", "ats_type", "slug", "workday_tenant", "workday_host", "workday_site")}) for c in companies]
            count = 0
            with ThreadPoolExecutor(max_workers=8) as pool:
                for future in as_completed([pool.submit(ats.fetch_for_company, c) for c in snapshots]):
                    try:
                        count += len(upsert_postings(db, future.result(), target_only=True))
                        db.commit()
                    except Exception:
                        db.rollback()
                        print("An employer board could not be refreshed; existing links retained.")
            print({"employer_boards_refreshed": len(companies), "new_direct_postings": count})
        print(repair_links(db, limit=args.limit))
        db.commit()
