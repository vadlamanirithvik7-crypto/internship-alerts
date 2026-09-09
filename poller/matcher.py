"""Durable alert outbox with retry, scoped ledger reads, and digest consolidation."""

import logging
from datetime import timedelta
from urllib.parse import urlparse
from sqlalchemy import exists, select
from sqlalchemy.orm import defer
from poller.alerts import send_email, send_ntfy_deliveries
from shared.db import AlertSent, Delivery, Filter, Posting, unpack_list, utcnow

log = logging.getLogger(__name__)


def posting_matches(posting, filter_row):
    haystack = (
        f"{posting.title} {posting.company_name} {posting.location or ''}".lower()
    )
    if any(term in haystack for term in unpack_list(filter_row.exclude_keywords)):
        return False
    if filter_row.remote_only and not posting.remote:
        return False
    locations = unpack_list(filter_row.locations)
    if (
        locations
        and not posting.remote
        and not any(loc in (posting.location or "").lower() for loc in locations)
    ):
        return False
    sectors, keywords = (
        unpack_list(filter_row.sectors),
        unpack_list(filter_row.keywords),
    )
    return (
        (not sectors and not keywords)
        or bool(set(sectors) & set(unpack_list(posting.sector_tags)))
        or any(k in haystack for k in keywords)
    )


def _wrapper(p):
    return (urlparse(p.url).hostname or "").lower() in {
        "jobright.ai",
        "www.jobright.ai",
    }


def process_new_postings(session, postings=None, *, lookback_days=3, target_only=False):
    """Compatibility name; candidates now come from DB even with no new inserts.

    Failed deliveries remain pending beyond the discovery lookback. Single poller
    execution is required (workflow concurrency + PostgreSQL advisory lock).
    SMTP/ntfy lack idempotency keys: a crash after send but before commit can repeat.
    """
    filters = list(session.scalars(select(Filter).where(Filter.active.is_(True))))
    for f in filters:
        for channel in set(unpack_list(f.channels) or ["email"]) & {"email", "ntfy"}:
            query = (
                select(Posting)
                .options(defer(Posting.description))
                .where(
                    Posting.first_seen_at >= utcnow() - timedelta(days=lookback_days),
                    Posting.closed_at.is_(None),
                    Posting.alert_eligible.is_(True),
                    Posting.applied_at.is_(None),
                    Posting.status.in_(["new", "interested"]),
                    ~exists().where(
                        AlertSent.posting_id == Posting.id,
                        AlertSent.filter_id == f.id,
                        AlertSent.channel == channel,
                    ),
                    ~exists().where(
                        Delivery.posting_id == Posting.id,
                        Delivery.filter_id == f.id,
                        Delivery.channel == channel,
                    ),
                )
            )
            if target_only:
                query = query.where(Posting.target_eligible.is_(True))
            for p in session.scalars(query):
                if posting_matches(p, f):
                    session.add(
                        Delivery(posting_id=p.id, filter_id=f.id, channel=channel)
                    )
    session.commit()  # outbox survives process/transport failure
    work = list(
        session.execute(
            select(Delivery, Posting, Filter)
            .options(defer(Posting.description))
            .join(Posting, Delivery.posting_id == Posting.id)
            .join(Filter, Delivery.filter_id == Filter.id)
            .where(Delivery.state == "pending", Filter.active.is_(True))
            .order_by(Delivery.id)
        )
    )
    grouped = {"email": [], "ntfy": []}
    seen = {}
    deferred = []
    for d, p, f in sorted(work, key=lambda row: _wrapper(row[1])):
        if (
            p.closed_at
            or p.applied_at is not None
            or p.status not in ("new", "interested")
            or (target_only and not p.target_eligible)
            or not posting_matches(p, f)
            or d.channel not in (unpack_list(f.channels) or ["email"])
        ):
            d.state = "cancelled"
            continue
        # Suppress only wrapper/direct pairs with an exact soft key. Distinct ATS
        # requisitions keep separate alerts even when titles and locations match.
        key = (f.id, d.channel, p.soft_key)
        peers = []
        if p.soft_key:
            peers = list(
                session.scalars(
                    select(Posting)
                    .join(AlertSent, AlertSent.posting_id == Posting.id)
                    .where(
                        Posting.soft_key == p.soft_key,
                        AlertSent.filter_id == f.id,
                        AlertSent.channel == d.channel,
                    )
                )
            )
        if any(_wrapper(peer) != _wrapper(p) for peer in peers):
            d.state = "suppressed"
            continue
        if p.soft_key and key in seen and _wrapper(seen[key][1]) != _wrapper(p):
            deferred.append((d, seen[key][0]))
            continue
        seen[key] = (d, p)
        grouped[d.channel].append((d, p, f))
    summary = {}
    for channel, rows in grouped.items():
        if not rows:
            continue
        unique = {p.id: p for _, p, _ in rows}
        for d, _, _ in rows:
            d.attempts += 1
            d.attempted_at = utcnow()
        session.commit()
        try:
            if channel == "email":
                groups = {}
                for _, p, f in rows:
                    groups.setdefault(f.name, {})[p.id] = p
                ok = send_email(
                    list(unique.values()),
                    groups={name: list(ps.values()) for name, ps in groups.items()},
                )
                delivered = set(unique) if ok else set()
            else:
                delivered = send_ntfy_deliveries(list(unique.values()))
        except Exception:
            log.exception("delivery failed; outbox remains pending")
            delivered = set()
        for d, p, f in rows:
            if p.id in delivered:
                d.state = "sent"
                session.add(
                    AlertSent(
                        posting_id=p.id,
                        filter_id=f.id,
                        channel=channel,
                        sent_at=utcnow(),
                    )
                )
                name = f"{f.name}/{channel}"
                summary[name] = summary.get(name, 0) + 1
        session.commit()
    for duplicate, original in deferred:
        if original.state == "sent":
            duplicate.state = "suppressed"
    session.commit()
    return summary
