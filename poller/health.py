"""Persist source observations; alert only on deterioration or recovery."""

from datetime import timedelta
from sqlalchemy import select
from poller.alerts import publish_ntfy
from shared.db import SourceRun, utcnow


def due(session, source, hours):
    last = session.scalar(
        select(SourceRun.checked_at)
        .where(SourceRun.source == source, SourceRun.state != "skipped")
        .order_by(SourceRun.checked_at.desc())
        .limit(1)
    )
    return last is None or last < utcnow() - timedelta(hours=hours)


def record(
    session, run_id, source, count, *, state="ok", detail="", elapsed=0, notify=True
):
    prior = list(
        session.scalars(
            select(SourceRun)
            .where(SourceRun.source == source, SourceRun.state != "skipped")
            .order_by(SourceRun.checked_at.desc())
            .limit(10)
        )
    )
    if state == "ok" and count == 0 and any(r.count > 0 for r in prior):
        state = "empty"
    row = SourceRun(
        run_id=run_id,
        source=source,
        count=count,
        state=state,
        detail=detail[:500],
        elapsed=int(elapsed),
    )
    session.add(row)
    session.flush()
    if notify and state != "skipped":
        bad = state in {"error", "empty"}
        was_bad = prior and prior[0].state in {"error", "empty"}
        if (bad and not was_bad) or (not bad and was_bad):
            ok = publish_ntfy(
                {
                    "title": f"Internship Radar: {source}",
                    "message": "Source needs attention. Check the health page."
                    if bad
                    else "Source recovered.",
                }
            )
            if not ok:
                row.detail = (row.detail + " Health notification not delivered.")[:500]
    return row
