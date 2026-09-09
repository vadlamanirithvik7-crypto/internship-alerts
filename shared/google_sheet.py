"""Durable one-way application sync to an owner-controlled Apps Script webhook."""

import json
import os
import re
import requests
from sqlalchemy import select, update, func
from shared.db import ApplicationSync, get_engine, get_session_factory, utcnow


def configured():
    return bool(
        re.fullmatch(
            r"https://script\.google\.com/macros/s/[A-Za-z0-9_-]+/exec",
            os.environ.get("GOOGLE_SHEETS_WEBHOOK_URL", ""),
        )
        and os.environ.get("GOOGLE_SHEETS_SYNC_TOKEN")
    )


def enqueue(db, p):
    if not p.applied_at and p.status not in ("applied", "interview", "offer", "rejected"):
        return
    version = (p.status_updated_at or utcnow()).isoformat(timespec="microseconds") + "Z"
    row = [
        str(p.id), p.company_name, p.title, p.location or "", p.term or "", p.status,
        p.applied_at.isoformat() + "Z" if p.applied_at else "", version,
        p.url, p.notes or "", version,
    ]
    item = db.get(ApplicationSync, p.id)
    if item is None:
        item = ApplicationSync(posting_id=p.id)
        db.add(item)
    item.version, item.payload, item.state = version, json.dumps(row), "pending"
    item.attempts = 0
    db.flush()


def sync_pending(engine=None, limit=30):
    """Acknowledge only the version sent; later edits remain queued.

    The receiver serializes requests, upserts stable IDs, and ignores stale
    versions, so retries and concurrent senders cannot duplicate rows or regress
    a newer stage. Never log tokens, webhook URLs, personal notes, or HTTP bodies.
    """
    if not configured():
        return 0
    Session = get_session_factory(engine if engine is not None else get_engine())
    with Session() as db:
        items = list(db.scalars(select(ApplicationSync).where(
            ApplicationSync.state == "pending"
        ).order_by(ApplicationSync.posting_id).limit(limit)))
        snapshots = [(i.posting_id, i.version, json.loads(i.payload)) for i in items]
        if not snapshots:
            return 0
        for item in items:
            item.attempts += 1
        db.commit()
        try:
            response = requests.post(
                os.environ["GOOGLE_SHEETS_WEBHOOK_URL"],
                json={"token": os.environ["GOOGLE_SHEETS_SYNC_TOKEN"],
                      "rows": [row for _, _, row in snapshots]},
                timeout=(5, 20),
            )
            response.raise_for_status()
            result = response.json()
            if result.get("ok") is not True:
                return 0
            acknowledged = {(str(r[0]), str(r[1])) for r in result.get("acknowledged", [])
                            if isinstance(r, list) and len(r) == 2}
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            return 0
        sent = 0
        for posting_id, version, _ in snapshots:
            if (str(posting_id), version) in acknowledged:
                sent += db.execute(update(ApplicationSync).where(
                    ApplicationSync.posting_id == posting_id,
                    ApplicationSync.version == version,
                    ApplicationSync.state == "pending",
                ).values(state="synced", synced_at=utcnow())).rowcount
        db.commit()
        return sent


def status(db):
    url = os.environ.get("GOOGLE_SHEET_URL", "")
    if not re.fullmatch(r"https://docs\.google\.com/spreadsheets/d/[A-Za-z0-9_-]+(?:/edit)?", url):
        url = ""
    return {"configured": configured(), "url": url,
            "pending": db.scalar(select(func.count()).select_from(ApplicationSync)
                                 .where(ApplicationSync.state == "pending")) or 0}
