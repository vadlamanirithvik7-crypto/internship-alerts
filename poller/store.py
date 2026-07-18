"""Persistence layer: tag, dedupe and upsert harvested postings.

Also grows the company watchlist automatically - any posting whose URL points at a
recognizable ATS teaches us a real, currently-valid board slug for that company.
"""

import logging
from datetime import datetime

from sqlalchemy import select

from poller.normalize import detect_ats, workday_parts
from shared.db import Company, Posting, pack_list, raw_hash, utcnow
from shared.sectors import is_internship, tag_posting

log = logging.getLogger(__name__)


def _identity(posting: dict) -> str:
    """Dedupe key for a posting.

    Keyed on the canonical URL alone, because that is the only field the sources
    agree on. The same job arrives with different titles (aggregators shorten
    "Product Analyst Intern (Spring/Summer 2026)" to "Product Analyst Intern") and
    different company names ("Aquatic" vs "Aquatic Capital Management"), so
    including either in the key produces duplicate rows and duplicate alerts.

    Only when a source gives us no URL do we fall back to company + title.
    """
    canonical = posting.get("canonical_url") or posting.get("url") or ""
    if canonical:
        return raw_hash(canonical)
    return raw_hash(posting.get("company_name", ""), posting.get("title", ""))


def upsert_companies(session, postings):
    """Learn company -> ATS mappings from posting URLs. Returns count of new rows."""
    existing = {
        name.lower(): company
        for name, company in (
            (c.name, c) for c in session.execute(select(Company)).scalars()
        )
    }
    added = 0

    for posting in postings:
        name = posting.get("company_name", "").strip()
        if not name:
            continue

        ats, slug = detect_ats(posting.get("url", ""))
        company = existing.get(name.lower())

        if company is None:
            company = Company(
                name=name,
                ats_type=ats,
                slug=slug,
                resolved=ats in ("greenhouse", "lever", "ashby") and bool(slug),
                source_hint=posting.get("source"),
            )
            if ats == "workday":
                tenant, _, site = workday_parts(posting.get("url", ""))
                company.workday_tenant = tenant
                company.workday_site = site
            session.add(company)
            existing[name.lower()] = company
            added += 1
        elif company.ats_type in ("unresolved", "other") and ats not in ("other",):
            # Upgrade a company we only knew by name once we see a real ATS link.
            company.ats_type = ats
            company.slug = slug
            company.resolved = ats in ("greenhouse", "lever", "ashby") and bool(slug)
            if ats == "workday":
                tenant, _, site = workday_parts(posting.get("url", ""))
                company.workday_tenant = tenant
                company.workday_site = site

    session.flush()
    return added


def upsert_postings(session, postings, *, internships_only=True):
    """Insert postings not already stored. Returns the list of newly created rows."""
    if not postings:
        return []

    # Filter to internships/co-ops and tag sectors before touching the database.
    candidates = {}
    for posting in postings:
        if internships_only and not is_internship(
            posting.get("title", ""), posting.get("description", ""), posting.get("term", "")
        ):
            continue
        if not posting.get("company_name") or not posting.get("title"):
            continue
        candidates[_identity(posting)] = posting

    if not candidates:
        return []

    known = set(
        session.execute(
            select(Posting.raw_hash).where(Posting.raw_hash.in_(list(candidates)))
        ).scalars()
    )

    company_ids = {
        c.name.lower(): c.id for c in session.execute(select(Company)).scalars()
    }

    now = utcnow()
    created = []
    for identity, posting in candidates.items():
        if identity in known:
            continue
        tags = tag_posting(
            posting.get("title", ""),
            posting.get("description", ""),
            posting.get("category_hint", ""),
        )
        row = Posting(
            external_id=posting.get("external_id"),
            company_id=company_ids.get(posting["company_name"].lower()),
            company_name=posting["company_name"][:300],
            title=posting["title"][:500],
            url=posting["url"],
            location=posting.get("location") or "",
            remote=posting.get("remote", False),
            source=posting["source"],
            category_hint=(posting.get("category_hint") or "")[:120],
            sector_tags=pack_list(tags),
            term=(posting.get("term") or "")[:120],
            posted_at=posting.get("posted_at"),
            first_seen_at=now,
            raw_hash=identity,
        )
        session.add(row)
        created.append(row)

    session.flush()
    log.info(
        "store: %s candidates, %s already known, %s new",
        len(candidates),
        len(candidates) - len(created),
        len(created),
    )
    return created
