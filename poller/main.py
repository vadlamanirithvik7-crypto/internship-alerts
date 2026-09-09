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
from datetime import datetime
import time
from types import SimpleNamespace

from sqlalchemy import func, select, case, or_
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
MAX_WORKERS = 8
# Companies polled between database commits, so a long sweep is crash-safe.
BATCH_SIZE = 50


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
    companies = (
        session.execute(
            select(Company)
            .where(
                Company.ats_type.in_(list(ats.FETCHERS)), Company.priority.is_(False)
            )
            .order_by(
                case(
                    (
                        Company.id.in_(
                            select(Posting.company_id).where(
                                Posting.target_eligible.is_(True),
                                Posting.closed_at.is_(None),
                            )
                        )
                        & or_(
                            Company.last_checked_at.is_(None),
                            Company.last_checked_at < utcnow() - timedelta(minutes=15),
                        ),
                        0,
                    ),
                    else_=1,
                ),
                Company.last_checked_at.is_(None).desc(),
                Company.last_checked_at.asc(),
            )
            .limit(limit)
        )
        .scalars()
        .all()
    )
    companies = (
        list(
            session.scalars(
                select(Company).where(
                    Company.priority.is_(True), Company.ats_type.in_(list(ats.FETCHERS))
                )
            )
        )
        + companies
    )
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

        # 1. Cross-company sources - trackers, HN, and Reddit list a direct apply
        #    URL regardless of which ATS a company uses, so this is how we cover
        #    the big employers (Google, Meta, ...) that aren't on the four ATS
        #    APIs we poll directly. All cheap, and dedupe collapses the overlap.
        feed_postings = []
        feed_versions = {}
        for name, harvester in [
            ("simplify", simplify.fetch),
            ("trackers", trackers.fetch),
            ("hackernews", hackernews.fetch),
            ("reddit", reddit.fetch),
            ("amazon", bigtech.fetch_amazon),
            ("microsoft", bigtech.fetch_microsoft),
        ]:
            if name in (
                "hackernews",
                "reddit",
                "amazon",
                "microsoft",
            ) and not health.due(session, name, 6):
                health.record(
                    session,
                    run_row.id,
                    name,
                    0,
                    state="skipped",
                    detail="Six-hour schedule",
                    notify=False,
                )
                continue
            if name == "reddit" and not (
                os.environ.get("REDDIT_CLIENT_ID")
                and os.environ.get("REDDIT_CLIENT_SECRET")
            ):
                health.record(
                    session,
                    run_row.id,
                    name,
                    0,
                    state="skipped",
                    detail="Credentials not configured",
                    notify=False,
                )
                continue
            net.reset_observation()
            tick = time.monotonic()
            try:
                rows = harvester() or []
                version = hashlib.sha256(
                    json.dumps(rows, sort_keys=True, default=str).encode()
                ).hexdigest()
                cache_key = f"feed-version:{name}"
                previous = session.get(AICache, cache_key)
                unchanged = previous is not None and previous.payload == version
                if not unchanged:
                    feed_postings.extend(rows)
                if not net.failed_requests():
                    feed_versions[cache_key] = version
                health.record(
                    session,
                    run_row.id,
                    name,
                    len(rows),
                    state="error" if not rows and net.failed_requests() else "ok",
                    detail=f"{net.failed_requests()} requests failed"
                    if net.failed_requests()
                    else (
                        "Unchanged feed; no duplicate database transfer"
                        if unchanged
                        else ""
                    ),
                    elapsed=time.monotonic() - tick,
                    notify=not skip_alerts,
                )
            except Exception:
                log.exception("%s failed", name)
                health.record(
                    session,
                    run_row.id,
                    name,
                    0,
                    state="error",
                    detail="Harvester failed; inspect runner logs",
                    notify=not skip_alerts,
                )
            session.commit()

        # 2. Broad keyword search - not bounded by the watchlist.
        if not skip_search:
            try:
                rows = search.fetch_all()
                feed_postings.extend(rows)
                health.record(
                    session, run_row.id, "search", len(rows), notify=not skip_alerts
                )
            except Exception:
                health.record(
                    session,
                    run_row.id,
                    "search",
                    0,
                    state="error",
                    notify=not skip_alerts,
                )

        # 3. Learn companies from everything seen so far, then store it.
        from shared.eligibility import eligible

        def relevant_company_rows(rows):
            return [
                p
                for p in rows
                if eligible(
                    p.get("title"),
                    p.get("location"),
                    p.get("term"),
                    p.get("description"),
                )
            ]

        upsert_companies(session, relevant_company_rows(feed_postings))
        created.extend(
            upsert_postings(
                session, feed_postings, alert_eligible=not skip_alerts, target_only=True
            )
        )
        # Commit versions with the postings, never before them: an interrupted
        # ingest must retry the same feed on its next run.
        for key, version in feed_versions.items():
            session.merge(AICache(key=key, kind="feed-version", payload=version))
        session.commit()
        harvested_count += len(feed_postings)
        if not skip_alerts:
            deliver()

        # 4. Poll company boards, persisting each batch as it completes so a
        #    long sweep survives a crash or a runner timeout.
        def persist(batch):
            nonlocal harvested_count
            harvested_count += len(batch)
            upsert_companies(session, relevant_company_rows(batch))
            new_rows = upsert_postings(
                session, batch, alert_eligible=not skip_alerts, target_only=True
            )
            session.commit()
            if new_rows and not skip_alerts:
                deliver()
            return new_rows

        created.extend(
            poll_boards(
                session,
                board_slice,
                on_batch=persist,
                run_id=run_row.id,
                notify=not skip_alerts,
            )
        )
        session.commit()

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
