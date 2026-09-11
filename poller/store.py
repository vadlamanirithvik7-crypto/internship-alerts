"""Idempotent ingestion, retained tagging evidence, and conservative availability."""

import logging
import re
from html import unescape
from sqlalchemy import func, select, or_
from sqlalchemy.orm import load_only
from poller.normalize import canonical_url, detect_ats, is_us_location, workday_parts
from shared.db import Company, Posting, pack_list, raw_hash, utcnow
from shared.sectors import is_internship, tag_posting

log = logging.getLogger(__name__)
DIRECT = {
    "greenhouse",
    "lever",
    "ashby",
    "workday",
    "smartrecruiters",
    "workable",
    "recruitee",
    "amazon",
    "microsoft",
}


def _identity(posting):
    canonical = posting.get("canonical_url") or canonical_url(posting.get("url", ""))
    return (
        raw_hash(canonical)
        if canonical
        else raw_hash(posting.get("company_name", ""), posting.get("title", ""))
    )


def soft_key(posting):
    """Conservative alert-only key; never merges different URLs or requisitions."""

    def norm(s):
        return re.sub(r"[^a-z0-9+#]+", " ", (s or "").lower()).strip()

    values = [norm(posting.get(k, "")) for k in ("company_name", "title", "location")]
    return raw_hash(*values) if all(values) else None


def clean_description(value):
    return re.sub(r"<[^>]+>", " ", unescape(unescape(value or ""))).strip()[:30000]


def upsert_companies(session, postings):
    names = {p.get("company_name", "").strip()[:300].lower() for p in postings} - {""}
    existing = {
        c.name.lower(): c
        for c in session.scalars(
            select(Company).where(func.lower(Company.name).in_(names))
        )
    }
    added = 0
    for posting in postings:
        name = posting.get("company_name", "").strip()[:300]
        if not name:
            continue
        ats, slug = detect_ats(posting.get("url", ""))
        company = existing.get(name.lower())
        if company is None:
            company = Company(
                name=name, source_hint=posting.get("source"), ats_type="other"
            )
            session.add(company)
            existing[name.lower()] = company
            added += 1
        if ats in DIRECT and (company.ats_type in (None, "unresolved", "other", ats)):
            company.ats_type, company.slug = ats, slug
            if ats == "workday":
                tenant, host, site = workday_parts(posting.get("url", ""))
                company.workday_tenant, company.workday_site = tenant, site
                company.workday_host = host
                company.resolved = bool(tenant and site)
            else:
                company.resolved = bool(slug)
    session.flush()
    return added


def upsert_postings(
    session,
    postings,
    *,
    internships_only=True,
    us_only=True,
    alert_eligible=True,
    target_only=False,
):
    candidates = {}
    for posting in postings:
        # Explicit closure can update a known record even when metadata is absent.
        if posting.get("active") is not False:
            if internships_only and not is_internship(
                posting.get("title", ""),
                ""
                if posting.get("source") in DIRECT
                else posting.get("description", ""),
                posting.get("term", ""),
            ):
                continue
            if us_only and not is_us_location(posting.get("location", "")):
                continue
            if not posting.get("company_name") or not posting.get("title"):
                continue
        identity = _identity(posting)
        prev = candidates.get(identity)
        if (
            prev is None
            or posting.get("source") in DIRECT
            or (prev.get("active") is False and posting.get("active") is not False)
        ):
            candidates[identity] = posting
    created = []
    # Bounded IN clauses work with SQLite variable limits as well as Postgres.
    items = list(candidates.items())
    now = utcnow()
    for offset in range(0, len(items), 400):
        chunk = items[offset : offset + 400]
        from shared.eligibility import eligible, SEARCH_VERSION

        existing_query = select(Posting).where(
            Posting.raw_hash.in_([k for k, _ in chunk])
        )
        if target_only:
            eligible_keys = [
                key
                for key, p in chunk
                if eligible(
                    p.get("title"),
                    p.get("location"),
                    p.get("term"),
                    p.get("description"),
                )
                or p.get("active") is False
            ]
            existing_query = existing_query.where(
                or_(
                    Posting.target_eligible.is_(True),
                    Posting.raw_hash.in_(eligible_keys),
                )
            )
        existing = {p.raw_hash: p for p in session.scalars(existing_query)}
        names = {p.get("company_name", "").lower()[:300] for _, p in chunk}
        companies = {
            name.lower(): id
            for name, id in session.execute(
                select(Company.name, Company.id).where(
                    func.lower(Company.name).in_(names)
                )
            )
        }
        for identity, posting in chunk:
            row = existing.get(identity)
            if posting.get("active") is False:
                # Direct-board observations take precedence over stale aggregators.
                if row and row.source not in DIRECT:
                    row.closed_at = row.closed_at or now
                continue
            from shared.eligibility import eligible, SEARCH_VERSION

            if (
                target_only
                and not (row and row.target_eligible)
                and not eligible(
                    posting.get("title"),
                    posting.get("location"),
                    posting.get("term"),
                    posting.get("description"),
                )
            ):
                continue
            if row is None:
                row = Posting(
                    raw_hash=identity,
                    company_name=posting["company_name"][:300],
                    company_id=companies.get(posting["company_name"].lower()[:300]),
                    title=posting["title"][:500],
                    url=posting.get("url", ""),
                    source=posting["source"],
                    first_seen_at=now,
                    alert_eligible=alert_eligible,
                )
                session.add(row)
                created.append(row)
            # A tracker sighting must not resurrect a role closed by its direct board.
            if row.source not in DIRECT or posting["source"] in DIRECT:
                row.closed_at = None
                row.missing_sweeps = 0
            row.last_seen_at = now
            desc = clean_description(posting.get("description"))
            if row.description is None and desc:
                row.description = desc
            elif desc and (
                posting["source"] in DIRECT or len(desc) > len(row.description or "")
            ):
                row.description = desc
            elif row in created:
                row.description = desc
            if posting["source"] in DIRECT or row in created:
                row.source = posting["source"]
                row.title = posting["title"][:500]
                row.url = posting.get("url", "")
                row.location = posting.get("location") or ""
                row.remote = posting.get("remote", False)
                row.external_id = posting.get("external_id")
            row.category_hint = (
                posting.get("category_hint") or row.category_hint or ""
            )[:120]
            row.term = (posting.get("term") or row.term or "")[:120]
            row.posted_at = posting.get("posted_at") or row.posted_at
            if row.description is not None:
                row.sector_tags = pack_list(
                    tag_posting(row.title, row.description, row.category_hint)
                )
            row.soft_key = soft_key(
                {
                    "company_name": row.company_name,
                    "title": row.title,
                    "location": row.location,
                }
            )
            from shared.eligibility import eligible, SEARCH_VERSION

            row.target_eligible = eligible(
                row.title, row.location, row.term, row.description
            )
            from shared.role_search import role_tags
            row.search_roles = pack_list(role_tags(row.title))
            row.search_version = SEARCH_VERSION
        session.flush()
    return created


def reconcile_board(session, company, postings, *, complete):
    """Only successful, exhaustive board scans count as evidence of absence."""
    if not complete:
        return
    seen = {_identity(p) for p in postings}
    for row in session.scalars(
        select(Posting)
        .where(Posting.company_id == company.id, Posting.source == company.ats_type)
        .options(
            load_only(
                Posting.id, Posting.raw_hash, Posting.missing_sweeps, Posting.closed_at
            )
        )
    ):
        if row.raw_hash in seen:
            row.missing_sweeps = 0
            row.closed_at = None
        else:
            row.missing_sweeps = (row.missing_sweeps or 0) + 1
            if row.missing_sweeps >= 3:
                row.closed_at = row.closed_at or utcnow()
