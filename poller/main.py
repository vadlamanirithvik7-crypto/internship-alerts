"""Poller entry point. Run by GitHub Actions on a schedule, or locally.

Runtime budget matters: the watchlist is well over a thousand companies and polling
every board every run would take far too long. Instead the cheap, high-yield sources
(tracker feeds) run every time, while company boards are polled in a rotating slice
ordered by least-recently-checked, so the whole list is covered over several runs
without any single run dragging.
"""

import argparse
import hashlib
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from types import SimpleNamespace

from sqlalchemy import func, select, or_
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from poller import resolver, net
from poller.locking import poller_lock
from poller.matcher import process_new_postings
from poller.sources import (
    bigtech,
    ats,
    discovery,
    hackernews,
    reddit,
    search,
    simplify,
    trackers,
)
from poller.store import upsert_companies, upsert_postings, reconcile_board
from poller import health
from shared.db import (
    Company,
    Posting,
    AICache,
    PollRun,
    get_engine,
    get_session_factory,
    init_db,
    utcnow,
)

log = logging.getLogger("poller")

DEFAULT_BOARD_SLICE = 250
DEFAULT_RESOLVE_SLICE = 40
MAX_WORKERS = 12
# Companies polled between database commits, so a long sweep is crash-safe.
BATCH_SIZE = 10


def select_board_companies(session, limit):
    """Reserve half of each sweep for oldest boards so new employers aren't starved."""
    if limit <= 0:
        return []
    base = select(Company).where(Company.ats_type.in_(list(ats.FETCHERS)))
    priority = list(session.scalars(base.where(Company.priority.is_(True))))
    ordinary = base.where(Company.priority.is_(False))
    oldest = (Company.last_checked_at.is_(None).desc(), Company.last_checked_at.asc(), Company.id)
    active = select(Posting.company_id).where(Posting.target_eligible.is_(True), Posting.closed_at.is_(None))
    hot = list(session.scalars(ordinary.where(
        Company.id.in_(active),
        or_(Company.last_checked_at.is_(None), Company.last_checked_at < utcnow() - timedelta(minutes=15)),
    ).order_by(*oldest).limit(limit // 2)))
    rotation = list(session.scalars(ordinary.where(
        Company.id.not_in([c.id for c in hot])
    ).order_by(*oldest).limit(limit - len(hot))))
    return priority + hot + rotation


def poll_boards(
    session, limit, on_batch=None, batch_size=BATCH_SIZE, run_id=None, notify=True
):
    """Poll a rotating slice of company boards, least-recently-checked first.

    Results are handed to `on_batch` every `batch_size` companies rather than
    accumulated and returned in one lump. A full sweep of the watchlist pulls
    >130k raw postings and takes ~15 minutes, so committing only at the end meant
    a crash or timeout near the finish discarded the entire run.
    """
    if limit <= 0:
        return []
    companies = select_board_companies(session, limit)
    if not companies:
        return []

    collected, batch, done, total_raw = [], [], 0, 0
    reconciliations = []
    board_counts = {}
    board_failures = {}

    def flush_batch():
        nonlocal batch
        if not batch and not reconciliations:
            return
        if on_batch is not None:
            collected.extend(on_batch(batch) or [])
        else:
            collected.extend(batch)
        for company, result in reconciliations:
            reconcile_board(
                session, company, result, complete=getattr(result, "complete", False)
            )
        reconciliations.clear()
        session.commit()
        batch = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(
                ats.fetch_for_company,
                SimpleNamespace(
                    **{
                        name: getattr(c, name)
                        for name in (
                            "name",
                            "ats_type",
                            "slug",
                            "workday_tenant",
                            "workday_site",
                            "workday_host",
                        )
                    }
                ),
            ): c
            for c in companies
        }
        for future in as_completed(futures):
            company = futures[future]
            try:
                result = future.result()
                board_counts[company.ats_type] = board_counts.get(
                    company.ats_type, 0
                ) + len(result)
                if not getattr(result, "complete", False):
                    board_failures[company.ats_type] = (
                        board_failures.get(company.ats_type, 0) + 1
                    )
                batch.extend(result)
                reconciliations.append((company, result))
                if getattr(result, "host", None):
                    company.workday_host = result.host
                if run_id:
                    health.record(
                        session,
                        run_id,
                        f"board:{company.name}"[:80],
                        len(result),
                        state="ok" if getattr(result, "complete", False) else "error",
                        notify=False,
                    )
            except Exception as exc:
                log.warning("boards: %s failed: %s", company.name, exc)
            company.last_checked_at = utcnow()
            done += 1

            if done % batch_size == 0:
                total_raw += len(batch)
                flush_batch()
                log.info(
                    "boards: %s/%s companies, %s raw so far",
                    done,
                    len(companies),
                    total_raw,
                )

    total_raw += len(batch)
    flush_batch()
    if run_id:
        for source, count in board_counts.items():
            health.record(
                session,
                run_id,
                source,
                count,
                state="error" if not count and board_failures.get(source) else "ok",
                detail=f"{board_failures.get(source, 0)} incomplete boards",
                notify=notify,
            )
        session.commit()
    log.info(
        "boards: polled %s companies -> %s raw postings", len(companies), total_raw
    )
    return collected


def _run(
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
    if not skip_alerts:
        from shared.google_sheet import sync_pending
        sync_pending(engine)

    with Session() as session:
        # The lock proves previous "running" rows no longer have an active runner.
        for unfinished in session.scalars(
            select(PollRun).where(PollRun.state == "running")
        ):
            unfinished.state = "interrupted"
        run_row = PollRun()
        session.add(run_row)
        session.commit()
        harvested_count = 0
        created = []
        summary = {}

        def deliver():
            for key, count in process_new_postings(session, target_only=True).items():
                summary[key] = summary.get(key, 0) + count

        def relevant_company_rows(rows):
            from shared.eligibility import confirmed_us, is_coop
            from shared.role_search import role_tags
            from shared.sectors import is_internship
            # Learn employer boards even before they post their summer 2027 roles.
            return [p for p in rows if confirmed_us(p.get("location")) and role_tags(p.get("title"))
                    and is_internship(p.get("title", ""), "", p.get("term", ""))
                    and not is_coop(p.get("title"), p.get("term"))]

        def persist(rows):
            nonlocal harvested_count
            harvested_count += len(rows)
            upsert_companies(session, relevant_company_rows(rows))
            new_rows = upsert_postings(session, rows, alert_eligible=not skip_alerts, target_only=True)
            session.commit()
            if new_rows and not skip_alerts:
                deliver()
                session.commit()
            return new_rows

        def accept_feed(name, rows, failures, elapsed):
            version = hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()
            key = f"feed-version:v3:{name}"
            previous = session.get(AICache, key)
            unchanged = previous is not None and previous.payload == version
            if not unchanged:
                created.extend(persist(rows))
            if not failures:
                session.merge(AICache(key=key, kind="feed-version", payload=version))
            health.record(session, run_row.id, name, len(rows),
                          state="error" if failures and not rows else "ok",
                          detail=f"{failures} requests failed" if failures else ("Unchanged feed" if unchanged else ""),
                          elapsed=elapsed, notify=not skip_alerts)
            session.commit()

        # Employer boards come first. Each small completed batch is visible and
        # eligible for alerts while slower companies are still being fetched.
        created.extend(poll_boards(session, board_slice, on_batch=persist,
                                   run_id=run_row.id, notify=not skip_alerts))

        # Network-only work runs concurrently; all ORM writes stay on this thread.
        cache = session.get(AICache, "tracker-feeds")
        try:
            cached_feeds = json.loads(cache.payload) if cache else []
        except (ValueError, TypeError):
            cached_feeds = []
        harvesters = [("simplify", simplify.fetch),
                      ("trackers", lambda: trackers.fetch(discover=False, feeds=cached_feeds))]
        for name, fetcher, hours in [
            ("amazon", bigtech.fetch_amazon, 0.25),
            ("microsoft", bigtech.fetch_microsoft, 0.25),
            ("hackernews", hackernews.fetch, 6),
            ("reddit", reddit.fetch, 6),
            ("search", search.fetch_all, 1),
        ]:
            if name == "search" and skip_search:
                continue
            if name == "reddit" and not (os.environ.get("REDDIT_CLIENT_ID") and os.environ.get("REDDIT_CLIENT_SECRET")):
                continue
            if health.due(session, name, hours):
                harvesters.append((name, fetcher))

        def fetch_observed(fetcher):
            net.reset_observation()
            tick = time.monotonic()
            rows = fetcher() or []
            return rows, net.failed_requests(), time.monotonic() - tick

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(fetch_observed, fetcher): name for name, fetcher in harvesters}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    accept_feed(name, *future.result())
                except Exception:
                    log.exception("%s failed", name)
                    session.rollback()
                    health.record(session, run_row.id, name, 0, state="error",
                                  detail="Harvester failed; inspect runner logs", notify=not skip_alerts)
                    session.commit()

        # Repository search is maintenance, not part of every discovery pass.
        # Retain previously found feeds and fetch them on subsequent scans.
        if health.due(session, "tracker-discovery", 6):
            tick = time.monotonic()
            try:
                feeds = trackers.discover_feeds(max_repos=10)
                combined = {row[2]: row for row in cached_feeds}
                combined.update({row[2]: row for row in feeds})
                session.merge(AICache(key="tracker-feeds", kind="source-config", payload=json.dumps(list(combined.values())[-60:])))
                health.record(session, run_row.id, "tracker-discovery", len(feeds), elapsed=time.monotonic()-tick, notify=False)
                session.commit()
            except Exception:
                session.rollback()
                log.exception("Tracker discovery failed; cached feeds retained")

        if not skip_alerts:
            deliver()
            session.commit()

        # 5. Expand coverage: discover new sector companies, resolve unresolved ones.
        if run_discovery:
            candidates = discovery.discover()
            known = {
                name.lower() for name in session.execute(select(Company.name)).scalars()
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
                .order_by(
                    Company.last_checked_at.is_(None).desc(),
                    Company.last_checked_at.asc(),
                )
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
        run_row.finished_at, run_row.state = utcnow(), "complete"
        run_row.harvested, run_row.new_postings, run_row.elapsed = (
            harvested_count,
            len(created),
            int(elapsed),
        )
        session.commit()
        log.info(
            "run complete in %.0fs | %s harvested | %s new postings | %s companies | alerts: %s",
            elapsed,
            harvested_count,
            len(created),
            total_companies,
            summary or "none",
        )
        return {"harvested": harvested_count, "new": len(created), "alerts": summary}


def run(**kwargs):
    engine = init_db(get_engine())
    with poller_lock(engine):
        try:
            return _run(**kwargs)
        except Exception:
            with get_session_factory(engine)() as session:
                row = session.scalar(
                    select(PollRun)
                    .where(PollRun.state == "running")
                    .order_by(PollRun.id.desc())
                    .limit(1)
                )
                if row:
                    row.state, row.finished_at = "failed", utcnow()
                    session.commit()
            raise


def main():
    parser = argparse.ArgumentParser(description="Poll job sources for new internships")
    parser.add_argument(
        "--board-slice",
        type=int,
        default=DEFAULT_BOARD_SLICE,
        help="how many company boards to poll this run",
    )
    parser.add_argument(
        "--resolve-slice",
        type=int,
        default=DEFAULT_RESOLVE_SLICE,
        help="how many unresolved companies to probe this run",
    )
    parser.add_argument(
        "--discovery",
        action="store_true",
        help="run SEC sector discovery (slow; weekly is plenty)",
    )
    parser.add_argument(
        "--skip-alerts",
        action="store_true",
        help="store postings without sending alerts",
    )
    parser.add_argument(
        "--skip-search", action="store_true", help="skip keyword-search sources"
    )
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
    print(
        f"harvested={result['harvested']} new={result['new']} alerts={result['alerts']}"
    )


if __name__ == "__main__":
    main()
