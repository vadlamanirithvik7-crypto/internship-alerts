"""Collapse duplicate postings created before the URL-only dedupe key.

Earlier the dedupe key hashed URL + company + title, so the same job stored once
per source whenever a title or company name differed slightly. This rewrites every
posting's raw_hash with the current identity rule and merges the collisions.

    python3 poller/dedupe.py --dry-run
    python3 poller/dedupe.py
"""

import argparse
import os
import sys
from collections import defaultdict

from sqlalchemy import select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from poller.normalize import canonical_url  # noqa: E402
from poller.store import _identity  # noqa: E402
from shared.db import (
    AlertSent,
    Delivery,
    Posting,
    get_engine,
    get_session_factory,
    init_db,
    raw_hash,
)  # noqa: E402


def _score(posting):
    """Prefer the richest record: a real ATS source over an aggregator, then the
    longer (less truncated) title, then the earliest sighting."""
    source_rank = (
        1 if posting.source in ("greenhouse", "lever", "ashby", "workday") else 0
    )
    return (source_rank, len(posting.title or ""), -(posting.id or 0))


def dedupe(dry_run=False):
    Session = get_session_factory(init_db(get_engine()))

    with Session() as session:
        postings = session.execute(select(Posting)).scalars().all()

        groups = defaultdict(list)
        for posting in postings:
            key = _identity(
                {
                    "canonical_url": canonical_url(posting.url),
                    "url": posting.url,
                    "company_name": posting.company_name,
                    "title": posting.title,
                }
            )
            groups[key].append(posting)

        collisions = {k: v for k, v in groups.items() if len(v) > 1}
        removing = sum(len(v) - 1 for v in collisions.values())

        print(
            f"{len(postings)} postings, {len(collisions)} duplicate groups, "
            f"{removing} rows to remove"
        )

        for key, rows in list(collisions.items())[:8]:
            keeper = max(rows, key=_score)
            print(f"\n  keep : [{keeper.source}] {keeper.title[:58]}")
            for row in rows:
                if row is not keeper:
                    print(f"  drop : [{row.source}] {row.title[:58]}")

        if dry_run:
            return removing

        # Vacate old identities before choosing survivors; otherwise an autoflush
        # can hit the unique hash owned by the soon-to-be-deleted duplicate.
        for posting in postings:
            posting.raw_hash = raw_hash("dedupe-temporary", str(posting.id))
        session.flush()
        removed = 0
        for key, rows in collisions.items():
            keeper = max(rows, key=_score)
            for row in rows:
                if row is keeper:
                    continue
                # Re-point any alert history at the survivor so a merged posting
                # is never re-alerted, then drop the duplicate.
                for alert in session.execute(
                    select(AlertSent).where(AlertSent.posting_id == row.id)
                ).scalars():
                    exists = session.execute(
                        select(AlertSent).where(
                            AlertSent.posting_id == keeper.id,
                            AlertSent.filter_id == alert.filter_id,
                            AlertSent.channel == alert.channel,
                        )
                    ).scalar()
                    if exists:
                        session.delete(alert)
                    else:
                        alert.posting_id = keeper.id
                for delivery in list(
                    session.scalars(
                        select(Delivery).where(Delivery.posting_id == row.id)
                    )
                ):
                    existing = session.scalar(
                        select(Delivery).where(
                            Delivery.posting_id == keeper.id,
                            Delivery.filter_id == delivery.filter_id,
                            Delivery.channel == delivery.channel,
                        )
                    )
                    if existing:
                        if delivery.state == "sent":
                            existing.state = "sent"
                        session.delete(delivery)
                    else:
                        delivery.posting_id = keeper.id
                if row.notes and row.notes not in (keeper.notes or ""):
                    keeper.notes = "\n".join(filter(None, [keeper.notes, row.notes]))
                if keeper.status == "new" and row.status != "new":
                    keeper.status = row.status
                keeper.first_seen_at = min(keeper.first_seen_at, row.first_seen_at)
                if row.last_seen_at and (
                    not keeper.last_seen_at or row.last_seen_at > keeper.last_seen_at
                ):
                    keeper.last_seen_at = row.last_seen_at
                if len(row.description or "") > len(keeper.description or ""):
                    keeper.description = row.description
                session.flush()
                session.delete(row)
                removed += 1

        session.flush()
        # Bring the survivors (and everything else) onto the new key scheme.
        for key, rows in groups.items():
            max(rows, key=_score).raw_hash = key

        session.commit()
        print(f"\nremoved {removed} duplicate postings")
        return removed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Merge duplicate postings")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    dedupe(dry_run=args.dry_run)
