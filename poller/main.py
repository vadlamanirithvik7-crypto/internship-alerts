"""Poller entry point. Run by GitHub Actions on a schedule, or locally.

Runtime budget matters: the watchlist is well over a thousand companies and polling
every board every run would take far too long. Instead the cheap, high-yield sources
(tracker feeds) run every time, while company boards are polled in a rotating slice
ordered by least-recently-checked, so the whole list is covered over several runs
without any single run dragging.
"""

import argparse
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from sqlalchemy import func, select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from poller import resolver
from poller.matcher import process_new_postings
from poller.sources import ats, discovery, search, simplify
from poller.store import upsert_companies, upsert_postings
from shared.db import Company, get_engine, get_session_factory, init_db, utcnow

log = logging.getLogger("poller")

DEFAULT_BOARD_SLICE = 250
DEFAULT_RESOLVE_SLICE = 40
MAX_WORKERS = 8
# Companies polled between database commits, so a long sweep is crash-safe.
BATCH_SIZE = 50


def poll_boards(session, limit, on_batch=None, batch_size=BATCH_SIZE):
    """Poll a rotating slice of company boards, least-recently-checked first.

    Results are handed to `on_batch` every `batch_size` companies rather than
    accumulated and returned in one lump. A full sweep of the watchlist pulls
    >130k raw postings and takes ~15 minutes, so committing only at the end meant
    a crash or timeout near the finish discarded the entire run.
    """
    companies = (
        session.execute(
            select(Company)
            .where(Company.ats_type.in_(list(ats.FETCHERS)))
            .order_by(Company.last_checked_at.is_(None).desc(), Company.last_checked_at.asc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    if not companies:
        return []

    collected, batch, done, total_raw = [], [], 0, 0

    def flush_batch():
        nonlocal batch
        if not batch:
            return
        if on_batch is not None:
            collected.extend(on_batch(batch) or [])
        else:
            collected.extend(batch)
        batch = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(ats.fetch_for_company, c): c for c in companies}
        for future in as_completed(futures):
            company = futures[future]
            try:
                batch.extend(future.result() or [])
            except Exception as exc:
                log.warning("boards: %s failed: %s", company.name, exc)
            company.last_checked_at = utcnow()
            done += 1

            if done % batch_size == 0:
                total_raw += len(batch)
                flush_batch()
                log.info("boards: %s/%s companies, %s raw so far", done, len(companies), total_raw)

    total_raw += len(batch)
    flush_batch()
    log.info("boards: polled %s companies -> %s raw postings", len(companies), total_raw)
    return collected


def run(
    *,
    board_slice=DEFAULT_BOARD_SLICE,
    resolve_slice=DEFAULT_RESOLVE_SLICE,
    run_discovery=False,
    skip_alerts=False,
    skip_search=False,
):
    engine = init_db(get_engine())
    Session = get_session_factory(engine)
    started = utcnow()

    with Session() as session:
        harvested_count = 0
        created = []

        # 1. Tracker feeds - cheap (3 requests) and the highest-yield source.
        feed_postings = []
        try:
            feed_postings.extend(simplify.fetch())
        except Exception as exc:
            log.error("simplify failed: %s", exc)

        # 2. Broad keyword search - not bounded by the watchlist.
        if not skip_search:
            feed_postings.extend(search.fetch_all())

        # 3. Learn companies from everything seen so far, then store it.
        upsert_companies(session, feed_postings)
        created.extend(upsert_postings(session, feed_postings))
        session.commit()
        harvested_count += len(feed_postings)

        # 4. Poll company boards, persisting each batch as it completes so a
        #    long sweep survives a crash or a runner timeout.
        def persist(batch):
            nonlocal harvested_count
            harvested_count += len(batch)
            upsert_companies(session, batch)
            new_rows = upsert_postings(session, batch)
            session.commit()
            return new_rows

        created.extend(poll_boards(session, board_slice, on_batch=persist))
        session.commit()

        summary = {}
        if created and not skip_alerts:
            summary = process_new_postings(session, created)
            session.commit()

        # 5. Expand coverage: discover new sector companies, resolve unresolved ones.
        if run_discovery:
            candidates = discovery.discover()
            known = {
                name.lower()
                for name in session.execute(select(Company.name)).scalars()
            }
            added = 0
            for name, sector in candidates.items():
                if name.lower() not in known:
                    session.add(Company(name=name, source_hint=f"sec:{sector}"))
                    added += 1
            session.commit()
            log.info("discovery: added %s new candidate companies", added)

        unresolved = (
            session.execute(
                select(Company)
                .where(Company.resolved.is_(False))
                .order_by(Company.last_checked_at.is_(None).desc(), Company.last_checked_at.asc())
                .limit(resolve_slice)
            )
            .scalars()
            .all()
        )
        if unresolved:
            found = resolver.resolve_companies(session, unresolved)
            session.commit()
            log.info("resolver: %s/%s resolved this run", found, len(unresolved))

        total_companies = session.scalar(select(func.count()).select_from(Company))

        elapsed = (utcnow() - started).total_seconds()
        log.info(
            "run complete in %.0fs | %s harvested | %s new postings | %s companies | alerts: %s",
            elapsed,
            harvested_count,
            len(created),
            total_companies,
            summary or "none",
        )
        return {"harvested": harvested_count, "new": len(created), "alerts": summary}


def main():
    parser = argparse.ArgumentParser(description="Poll job sources for new internships")
    parser.add_argument("--board-slice", type=int, default=DEFAULT_BOARD_SLICE,
                        help="how many company boards to poll this run")
    parser.add_argument("--resolve-slice", type=int, default=DEFAULT_RESOLVE_SLICE,
                        help="how many unresolved companies to probe this run")
    parser.add_argument("--discovery", action="store_true",
                        help="run SEC sector discovery (slow; weekly is plenty)")
    parser.add_argument("--skip-alerts", action="store_true",
                        help="store postings without sending alerts")
    parser.add_argument("--skip-search", action="store_true",
                        help="skip keyword-search sources")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logging.getLogger("poller").setLevel(logging.INFO)

    result = run(
        board_slice=args.board_slice,
        resolve_slice=args.resolve_slice,
        run_discovery=args.discovery,
        skip_alerts=args.skip_alerts,
        skip_search=args.skip_search,
    )
    print(f"harvested={result['harvested']} new={result['new']} alerts={result['alerts']}")


if __name__ == "__main__":
    main()
