"""Read-only mail events. Message text is data, never application instructions."""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, or_
from shared.db import MailEvent, Posting, ApplicationTask, utcnow


def classify(text):
    # Ignore quoted history; old rejection text must not override a new interview.
    text = re.split(r"\n(?:On .+wrote:|From:|[- ]*Original Message)", text, maxsplit=1, flags=re.I)[0]
    text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))
    if re.search(r"not (?:be )?(?:moving|proceeding) forward|will not be (?:moving|proceeding)|unfortunately.{0,120}(?:other candidates|not selected|unable to offer)|decided (?:to pursue|to move forward with) other", text, re.I | re.S):
        return "rejected"
    if re.search(r"(?:invite|invitation|schedule|scheduling).{0,100}interview|interview.{0,60}(?:invitation|availability|schedule)", text, re.I | re.S):
        return "interview"
    if re.search(r"(?:complete|take|invited).{0,100}(?:assessment|coding challenge)|next steps|action required", text, re.I | re.S):
        return "next_steps"
    if re.search(r"(?:received|submitted) your application|your application (?:has been|was) (?:successfully )?(?:received|submitted)|thank you for applying", text, re.I):
        return "applied"
    return "update"


def normalized(text):
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def candidates(db):
    return list(db.scalars(select(Posting).outerjoin(ApplicationTask, ApplicationTask.posting_id == Posting.id).where(
        or_(Posting.applied_at >= utcnow() - timedelta(days=365), ApplicationTask.state.in_(["submitting", "uncertain"])))
        .order_by(Posting.id.desc()).limit(500)))


def apply_event(db, event, posting):
    """Called for a unique strong match, or an explicit owner-selected match."""
    event.posting_id, event.state = posting.id, "linked"
    if event.stage == "applied":
        task = db.scalar(select(ApplicationTask).where(ApplicationTask.posting_id == posting.id))
        if task and task.state in ("submitting", "uncertain"):
            from shared.applying import mark_confirmed
            mark_confirmed(db, task, "Application receipt linked from email: " + event.subject)
    # Old messages cannot overwrite a newer manual update or a terminal outcome.
    if (posting.status_updated_at and event.received_at <= posting.status_updated_at) or posting.status in ("offer", "rejected"):
        return
    if event.stage not in ("interview", "rejected", "next_steps"):
        return
    if event.stage in ("interview", "rejected"):
        posting.status = event.stage
    if not posting.applied_at:
        posting.applied_at = event.received_at
    posting.status_updated_at = utcnow()
    note = f"Email {event.received_at:%Y-%m-%d}: {event.stage.replace('_', ' ')} — {event.subject}"
    posting.notes = ((posting.notes or "") + "\n" + note).strip()[-5000:]
    from shared.google_sheet import enqueue
    from shared.application_sheet import update_workbook
    db.flush()
    enqueue(db, posting)
    update_workbook(db)


def ingest(db, email, messages):
    rows = candidates(db)
    pending = {t.posting_id: t.submission_started_at for t in db.scalars(select(ApplicationTask).where(ApplicationTask.state.in_(["submitting", "uncertain"])))}
    accepted = 0
    for msg in messages:
        mid = str(msg.get("id", ""))
        if not re.fullmatch(r"[a-fA-F0-9]{8,64}", mid):
            raise ValueError("Invalid Gmail message ID.")
        key = hashlib.sha256((email.lower() + ":" + mid).encode()).hexdigest()
        if db.scalar(select(MailEvent.id).where(MailEvent.message_key == key)):
            continue
        subject, sender, body = (str(msg.get(k, "")) for k in ("subject", "sender", "text"))
        if len(subject) > 300 or len(sender) > 300 or len(body) > 6000:
            raise ValueError("Mail item is too large.")
        try:
            received = datetime.fromtimestamp(int(msg["timestamp"]) / 1000, tz=timezone.utc).replace(tzinfo=None)
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            raise ValueError("Invalid message timestamp.") from None
        if received > utcnow() + timedelta(minutes=5) or received < utcnow() - timedelta(days=35):
            continue
        text = subject + "\n" + body
        content = " " + normalized(text) + " "
        matches = []
        strong = []
        for p in rows:
            since = p.applied_at or pending.get(p.id)
            if not since or received < since - timedelta(minutes=5):
                continue
            company, role = normalized(p.company_name), normalized(p.title)
            if len(company) < 3 or " " + company + " " not in content:
                continue
            matches.append(p)
            if len(role) >= 8 and " " + role + " " in content:
                strong.append(p)
        if not matches:
            continue  # Do not retain unrelated mailbox content.
        event = MailEvent(message_key=key, message_id=mid, candidates=json.dumps([p.id for p in matches]),
                          stage=classify(text), subject=subject, sender=sender,
                          snippet=body[:1000], received_at=received)
        db.add(event)
        db.flush()
        if len(strong) == 1 and len(matches) == 1:
            apply_event(db, event, strong[0])
        accepted += 1
    return accepted
