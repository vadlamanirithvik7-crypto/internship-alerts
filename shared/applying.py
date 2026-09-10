"""Resume library and compatibility handling for historical application receipts."""
import hashlib
import json
import re
from io import BytesIO
from urllib.parse import urlsplit

from pypdf import PdfReader
from sqlalchemy import select
from shared.db import (ApplicationTask, ApplicantSettings, StoredResume, Posting,
                       utcnow)

PROFILE_FIELDS = {
    "first_name": "First name", "last_name": "Last name", "email": "Application email",
    "phone": "Phone (including country code)", "location": "City, state, country",
    "linkedin": "LinkedIn URL", "github": "GitHub URL", "website": "Portfolio URL",
}


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
