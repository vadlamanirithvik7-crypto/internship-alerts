"""Application state machine. Nothing is Applied until there is confirmation."""
import hashlib
import json
import re
import secrets
from datetime import timedelta
from io import BytesIO
from urllib.parse import urlsplit, urlunsplit

from pypdf import PdfReader
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from shared.db import (ApplicationTask, ApplicantSettings, StoredResume, Posting,
                       ApplicationWorker, utcnow)

PROFILE_FIELDS = {
    "first_name": "First name", "last_name": "Last name", "email": "Application email",
    "phone": "Phone (including country code)", "location": "City, state, country",
    "linkedin": "LinkedIn URL", "github": "GitHub URL", "website": "Portfolio URL",
}
ACTIVE_STATES = {"queued", "running", "needs_info", "needs_review", "needs_action", "submitting", "uncertain"}


def json_data(value):
    return json.loads(value or "{}")


def validate_profile(values):
    data = {k: str(values.get(k, "")).strip() for k in PROFILE_FIELDS}
    if any(len(v) > 300 for v in data.values()):
        raise ValueError("Keep each answer under 300 characters.")
    if not data["first_name"] or not data["last_name"]:
        raise ValueError("Enter your first and last name.")
    if len(data["email"]) > 254 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", data["email"]):
        raise ValueError("Enter a valid application email address.")
    for key in ("linkedin", "github", "website"):
        if data[key] and (urlsplit(data[key]).scheme != "https" or not urlsplit(data[key]).hostname):
            raise ValueError("Profile links must start with https://.")
    return data


def store_resume(db, name, filename, content):
    if len(content) > 2_000_000:
        raise ValueError("Resume must be at most 2 MB.")
    if not filename.lower().endswith(".pdf") or not content.startswith(b"%PDF-"):
        raise ValueError("Upload a PDF resume.")
    try:
        pdf = PdfReader(BytesIO(content))
        if pdf.is_encrypted or not 1 <= len(pdf.pages) <= 10:
            raise ValueError()
    except Exception:
        raise ValueError("Use an unlocked PDF with 1–10 pages.") from None
    # Serialize slot allocation in PostgreSQL; old versions stay pinned to tasks.
    settings = db.scalar(select(ApplicantSettings).where(ApplicantSettings.id == 1).with_for_update())
    if settings is None:
        raise ValueError("Save your application details first.")
    active = list(db.scalars(select(StoredResume).where(StoredResume.active.is_(True))))
    if len(active) >= 3:
        raise ValueError("Three resumes are saved. Archive one before uploading a replacement.")
    filename = re.sub(r"[^A-Za-z0-9_.-]", "_", filename.split("/")[-1].split("\\")[-1])[:140]
    row = StoredResume(name=name.strip()[:120] or "My resume", filename=filename,
                       content=content, sha256=hashlib.sha256(content).hexdigest())
    db.add(row)
    db.flush()
    return row


def application_url(url):
    """Only direct, public ATS application routes are eligible for automation."""
    try:
        p = urlsplit(url)
        if p.scheme != "https" or p.username or p.password or p.port not in (None, 443):
            return None
        if p.hostname in ("jobs.lever.co", "jobs.eu.lever.co") and re.fullmatch(r"/[\w-]+/[a-fA-F0-9-]{32,36}(?:/apply)?/?", p.path):
            return "lever", urlunsplit(("https", p.hostname, p.path.rstrip("/").removesuffix("/apply") + "/apply", "", ""))
        if p.hostname in ("boards.greenhouse.io", "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io") and re.fullmatch(r"/[\w-]+/jobs/\d+/?", p.path):
            return "greenhouse", urlunsplit(("https", p.hostname, p.path, "", ""))
    except ValueError:
        pass
    return None


def queue_application(db, posting_id, resume_id):
    p = db.scalar(select(Posting).where(Posting.id == posting_id).with_for_update())
    if p is None:
        raise ValueError("Role not found.")
    target = application_url(p.url)
    canonical = target[1] if target else p.url
    key = hashlib.sha256(canonical.encode()).hexdigest()
    old = db.scalar(select(ApplicationTask).where((ApplicationTask.posting_id == p.id) | (ApplicationTask.application_key == key)))
    if old:
        return old  # Repeated taps never create another employer submission.
    if p.applied_at or p.status in ("applied", "interview", "offer", "rejected"):
        raise ValueError("This role is already tracked as applied.")
    if p.closed_at:
        raise ValueError("This role is marked closed.")
    settings = db.get(ApplicantSettings, 1)
    resume = db.get(StoredResume, resume_id)
    if not settings or not resume or not resume.active:
        raise ValueError("Save your details and choose an available resume first.")
    data = validate_profile(json_data(settings.data))
    row = ApplicationTask(posting_id=p.id, resume_id=resume.id, applicant=json.dumps(data),
                          target_url=p.url, application_key=key,
                          review_before_submit=settings.review_before_submit)
    if not application_url(p.url):
        row.state = "needs_action"
        row.detail = "This application site needs you to complete its form. Open the employer page, then confirm submission here."
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        existing = db.scalar(select(ApplicationTask).where(ApplicationTask.application_key == key))
        if existing is None:
            raise
        return existing
    return row


def claim_next(db):
    now = utcnow()
    worker = db.get(ApplicationWorker, 1)
    if worker is None:
        worker = ApplicationWorker(id=1)
        db.add(worker)
    worker.last_seen_at = now
    stale = now - timedelta(minutes=15)
    # A lost browser after the irreversible boundary is NEVER resubmitted.
    db.execute(update(ApplicationTask).where(ApplicationTask.state == "submitting", ApplicationTask.claimed_at < stale).values(
        state="uncertain", detail="The worker stopped after submission began. Check the employer site or confirmation email before marking Applied.", updated_at=now))
    db.execute(update(ApplicationTask).where(ApplicationTask.state == "running", ApplicationTask.claimed_at < stale).values(
        state="queued", claim_token=None, updated_at=now))
    db.commit()
    row = db.scalar(select(ApplicationTask).where(ApplicationTask.state == "queued").order_by(ApplicationTask.created_at).limit(1))
    if row is None:
        return None
    token = secrets.token_hex(24)
    changed = db.execute(update(ApplicationTask).where(ApplicationTask.id == row.id, ApplicationTask.state == "queued").values(
        state="running", claimed_at=now, updated_at=now, claim_token=token, detail="Reading the employer application form."))
    db.commit()
    if changed.rowcount != 1:
        return None
    db.expire_all()
    return db.get(ApplicationTask, row.id)


def owned_transition(db, task_id, token, from_state="running", **values):
    values["updated_at"] = utcnow()
    result = db.execute(update(ApplicationTask).where(ApplicationTask.id == task_id,
        ApplicationTask.claim_token == token, ApplicationTask.state == from_state).values(**values))
    db.commit()
    return result.rowcount == 1


def mark_confirmed(db, task, evidence):
    p = db.scalar(select(Posting).where(Posting.id == task.posting_id).with_for_update())
    now = utcnow()
    task.state, task.submitted_at = "submitted", task.submitted_at or now
    task.updated_at, task.confirmation = now, evidence[:600]
    task.detail = "Submitted. Removed from Discover and queued for your Google Sheet."
    if p.applied_at is None:
        p.applied_at = now
    if p.status in ("new", "interested"):
        p.status = "applied"
    p.status_updated_at = now
    from shared.google_sheet import enqueue
    from shared.application_sheet import update_workbook
    db.flush()
    enqueue(db, p)
    update_workbook(db)


def form_digest(questions):
    return hashlib.sha256(json.dumps(questions, sort_keys=True).encode()).hexdigest()
