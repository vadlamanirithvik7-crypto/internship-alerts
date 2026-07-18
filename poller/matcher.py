"""Match new postings against saved filters and dispatch alerts exactly once."""

import logging
from datetime import datetime

from sqlalchemy import select

from poller.alerts import send_email, send_ntfy
from shared.db import AlertSent, Filter, unpack_list, utcnow

log = logging.getLogger(__name__)


def posting_matches(posting, filter_row) -> bool:
    """Evaluate one posting against one saved filter.

    Semantics: sectors OR keywords must hit (if either is specified), exclusions
    always veto, and location/remote constraints must be satisfied.
    """
    haystack = f"{posting.title} {posting.company_name} {posting.location or ''}".lower()

    excludes = unpack_list(filter_row.exclude_keywords)
    if any(term in haystack for term in excludes):
        return False

    if filter_row.remote_only and not posting.remote:
        return False

    locations = unpack_list(filter_row.locations)
    if locations:
        location_text = (posting.location or "").lower()
        if not any(loc in location_text for loc in locations):
            # A remote role satisfies any location constraint.
            if not posting.remote:
                return False

    sectors = unpack_list(filter_row.sectors)
    keywords = unpack_list(filter_row.keywords)

    # A filter with neither sectors nor keywords matches everything that got
    # past the exclusions - useful as a catch-all "any internship" filter.
    if not sectors and not keywords:
        return True

    posting_sectors = set(unpack_list(posting.sector_tags))
    if sectors and posting_sectors.intersection(sectors):
        return True
    if keywords and any(term in haystack for term in keywords):
        return True

    return False


def process_new_postings(session, postings):
    """Alert on newly-seen postings. Returns {filter_name: count} actually alerted."""
    if not postings:
        return {}

    filters = session.execute(select(Filter).where(Filter.active.is_(True))).scalars().all()
    if not filters:
        log.info("matcher: no active filters configured")
        return {}

    summary = {}
    now = utcnow()

    for filter_row in filters:
        channels = unpack_list(filter_row.channels) or ["email"]
        matched = [p for p in postings if posting_matches(p, filter_row)]
        if not matched:
            continue

        # Skip anything already alerted for this filter/channel in an earlier run.
        already = {
            (posting_id, channel)
            for posting_id, channel in session.execute(
                select(AlertSent.posting_id, AlertSent.channel).where(
                    AlertSent.filter_id == filter_row.id
                )
            )
        }

        for channel in channels:
            pending = [p for p in matched if (p.id, channel) not in already]
            if not pending:
                continue

            if channel == "email":
                ok = send_email(
                    pending,
                    subject=f"{len(pending)} new internship match{'es' if len(pending) != 1 else ''} - {filter_row.name}",
                )
            elif channel == "ntfy":
                ok = send_ntfy(pending)
            else:
                log.warning("matcher: unknown channel %r", channel)
                continue

            if not ok:
                # Don't record a send that didn't happen - retry next run instead.
                continue

            for posting in pending:
                session.add(
                    AlertSent(
                        posting_id=posting.id,
                        filter_id=filter_row.id,
                        channel=channel,
                        sent_at=now,
                    )
                )
            summary[f"{filter_row.name}/{channel}"] = len(pending)

    session.flush()
    return summary
