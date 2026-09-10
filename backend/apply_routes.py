"""Private, phone-friendly resume, application queue and mailbox pages."""
import hashlib
import hmac
import json
import secrets
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from fastapi import Depends, HTTPException, Request, BackgroundTasks, APIRouter
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from shared.db import (ApplicantSettings, StoredResume, Posting, ApplicationTask,
                       ApplicationWorker, MailConnection, MailEvent, utcnow)
from shared.applying import (PROFILE_FIELDS, validate_profile, store_resume, queue_application,
                             mark_confirmed, json_data, form_digest, application_url)


def register(app, templates, get_db):
    templates.env.filters["fromjson"] = json.loads
    owner_app = app

    def private_only(request: Request):
        if request.state.demo:
            raise HTTPException(404)

    app = APIRouter(dependencies=[Depends(private_only)])
    def render(request, template, context):
        return templates.TemplateResponse(request, template, context)

    def require_task(db, task_id):
        task = db.scalar(select(ApplicationTask).where(ApplicationTask.id == task_id).with_for_update())
        if task is None:
            raise HTTPException(404, "Application not found")
        return task

    @app.get("/apply-settings")
    def settings_page(request: Request, db=Depends(get_db)):
        settings = db.get(ApplicantSettings, 1)
        return render(request, "apply_settings.html", {
            "fields": PROFILE_FIELDS, "settings": settings,
            "values": json_data(settings.data) if settings else {},
            "resumes": list(db.scalars(select(StoredResume).where(StoredResume.active.is_(True)).order_by(StoredResume.id))),
            "connection": db.get(MailConnection, 1),
        })

    @app.post("/apply-settings")
    async def save_settings(request: Request, db=Depends(get_db)):
        form = await request.form()
        try:
            data = validate_profile(form)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        settings = db.get(ApplicantSettings, 1)
        if settings is None:
            settings = ApplicantSettings(id=1)
            db.add(settings)
        settings.data = json.dumps(data)
        settings.review_before_submit = form.get("review_before_submit") == "yes"
        settings.updated_at = utcnow()
        connection = db.get(MailConnection, 1)
        if connection and connection.email.lower() != data["email"].lower():
            connection.enabled = False
        db.commit()
        return RedirectResponse("/apply-settings#resumes", 303)

    @app.post("/resumes")
    async def upload_resume(request: Request, db=Depends(get_db)):
        form = await request.form()
        upload = form.get("resume")
        if not upload or not hasattr(upload, "read"):
            raise HTTPException(422, "Choose a PDF resume")
        content = await upload.read(2_000_001)
        try:
            store_resume(db, str(form.get("name", "")), upload.filename or "", content)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        db.commit()
        return RedirectResponse("/apply-settings#resumes", 303)

    @app.get("/resumes/{resume_id}/download")
    def download_resume(resume_id: int, db=Depends(get_db)):
        resume = db.get(StoredResume, resume_id)
        if resume is None:
            raise HTTPException(404)
        return Response(resume.content, media_type="application/pdf", headers={
            "Content-Disposition": f'attachment; filename="{resume.filename}"'})

    @app.post("/resumes/{resume_id}/archive")
    def archive_resume(resume_id: int, db=Depends(get_db)):
        resume = db.get(StoredResume, resume_id)
        if resume is None:
            raise HTTPException(404)
        resume.active = False
        db.commit()
        return RedirectResponse("/apply-settings#resumes", 303)

    @app.get("/jobs/{posting_id}/apply")
    def apply_page(request: Request, posting_id: int, db=Depends(get_db)):
        posting = db.get(Posting, posting_id)
        if posting is None:
            raise HTTPException(404)
        task = db.scalar(select(ApplicationTask).where(ApplicationTask.posting_id == posting_id))
        if task:
            return RedirectResponse(f"/apply-tasks/{task.id}", 303)
        settings = db.get(ApplicantSettings, 1)
        return render(request, "apply_choose.html", {
            "p": posting, "settings": settings,
            "values": json_data(settings.data) if settings else {},
            "supported": application_url(posting.url),
            "resumes": list(db.scalars(select(StoredResume).where(StoredResume.active.is_(True)).order_by(StoredResume.id))),
        })

    @app.post("/jobs/{posting_id}/apply")
    async def start_apply(request: Request, posting_id: int, db=Depends(get_db)):
        form = await request.form()
        try:
            existing = db.scalar(select(ApplicationTask).where(ApplicationTask.posting_id == posting_id))
            if existing:
                return RedirectResponse(f"/apply-tasks/{existing.id}", 303)
            upload = form.get("resume")
            if upload and hasattr(upload, "read") and upload.filename:
                resume = store_resume(db, str(form.get("name", "")), upload.filename, await upload.read(2_000_001))
                resume_id = resume.id
            else:
                resume_id = int(form.get("resume_id", "0"))
            task = queue_application(db, posting_id, resume_id)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        db.commit()
        return RedirectResponse(f"/apply-tasks/{task.id}", 303)

    @app.get("/apply-tasks")
    def queue_page(request: Request, db=Depends(get_db)):
        rows = db.execute(select(ApplicationTask, Posting).join(Posting).order_by(ApplicationTask.created_at.desc()).limit(100)).all()
        worker = db.get(ApplicationWorker, 1)
        return render(request, "apply_queue.html", {"rows": rows, "worker": worker,
            "worker_recent": worker and worker.last_seen_at > utcnow() - timedelta(minutes=30)})

    @app.get("/apply-tasks/{task_id}")
    def task_page(request: Request, task_id: int, db=Depends(get_db)):
        task = db.get(ApplicationTask, task_id)
        if task is None:
            raise HTTPException(404)
        return render(request, "apply_task.html", {"task": task, "p": db.get(Posting, task.posting_id),
            "resume": db.get(StoredResume, task.resume_id), "applicant": json_data(task.applicant),
            "questions": json.loads(task.questions), "answers": json_data(task.answers),
            "resumes": list(db.scalars(select(StoredResume).where(StoredResume.active.is_(True))))})

    @app.post("/apply-tasks/{task_id}/resume")
    async def change_resume(request: Request, task_id: int, db=Depends(get_db)):
        task = require_task(db, task_id)
        if task.state not in ("queued", "needs_info", "needs_review", "needs_action", "cancelled") or task.submission_started_at:
            raise HTTPException(409, "Wait for the worker before changing this application's resume.")
        form = await request.form()
        try:
            resume = db.get(StoredResume, int(form.get("resume_id", "0")))
        except ValueError:
            resume = None
        if not resume or not resume.active:
            raise HTTPException(422, "Choose an available resume.")
        task.resume_id, task.answers, task.questions, task.reviewed_digest = resume.id, "{}", "[]", ""
        task.state = "queued" if application_url(db.get(Posting, task.posting_id).url) else "needs_action"
        task.detail = "Resume changed. Earlier employer-specific answers were cleared; open the employer site if manual completion is needed."
        task.updated_at = utcnow()
        db.commit()
        return RedirectResponse(f"/apply-tasks/{task_id}", 303)

    @app.post("/apply-tasks/{task_id}/answers")
    async def answer_questions(request: Request, task_id: int, db=Depends(get_db)):
        form = await request.form()
        task = require_task(db, task_id)
        if task.state != "needs_info":
            raise HTTPException(409, "This application is no longer waiting for answers. Refresh the page.")
        answers = json_data(task.answers)
        for q in json.loads(task.questions):
            if q["answered"] or q["type"] == "file":
                continue
            value = str(form.get(q["key"], "")).strip()
            if len(value) > min(5000, q["maxlength"]):
                raise HTTPException(422, "One of your answers is too long.")
            if q["required"] and (not value or (q["type"] == "checkbox" and value != "yes")):
                raise HTTPException(422, "Answer every required question. If you cannot agree to a required statement, cancel or open the employer page.")
            if q["type"] in ("select-one", "radio") and value and value not in [o["value"] for o in q["options"]]:
                raise HTTPException(422, "Choose one of the employer's listed options.")
            if q["type"] == "checkbox" and value not in ("yes", "no", ""):
                raise HTTPException(422, "Choose Yes or No.")
            answers[q["key"]] = value
        task.answers, task.state = json.dumps(answers), "queued"
        task.reviewed_digest, task.detail = "", "Answers saved. Waiting for the application worker."
        task.updated_at = utcnow()
        db.commit()
        return RedirectResponse(f"/apply-tasks/{task_id}", 303)

    @app.post("/apply-tasks/{task_id}/review")
    def approve_review(task_id: int, db=Depends(get_db)):
        task = require_task(db, task_id)
        if task.state != "needs_review":
            raise HTTPException(409, "Refresh this application's current status.")
        task.reviewed_digest = form_digest(json.loads(task.questions))
        task.state, task.updated_at = "queued", utcnow()
        task.detail = "Approved for submission. Waiting for the application worker."
        db.commit()
        return RedirectResponse(f"/apply-tasks/{task_id}", 303)

    @app.post("/apply-tasks/{task_id}/retry")
    def retry_preparation(task_id: int, db=Depends(get_db)):
        task = require_task(db, task_id)
        if task.state not in ("needs_action", "cancelled") or task.submission_started_at:
            raise HTTPException(409, "This application cannot safely be retried. Check its current status.")
        if not application_url(db.get(Posting, task.posting_id).url):
            raise HTTPException(422, "This employer requires manual completion.")
        task.state, task.updated_at = "queued", utcnow()
        task.detail = "Waiting to retry preparation."
        db.commit()
        return RedirectResponse(f"/apply-tasks/{task_id}", 303)

    @app.post("/apply-tasks/{task_id}/cancel")
    def cancel(task_id: int, db=Depends(get_db)):
        task = require_task(db, task_id)
        if task.state not in ("queued", "needs_info", "needs_review", "needs_action") or task.submission_started_at:
            raise HTTPException(409, "The worker is processing this application. Refresh its status before taking another action.")
        task.state, task.updated_at = "cancelled", utcnow()
        task.detail = "Cancelled before submission."
        db.commit()
        return RedirectResponse(f"/apply-tasks/{task_id}", 303)

    @app.post("/apply-tasks/{task_id}/confirm")
    async def confirm_manual(request: Request, task_id: int, background_tasks: BackgroundTasks, db=Depends(get_db)):
        form = await request.form()
        task = require_task(db, task_id)
        if task.state not in ("needs_action", "uncertain") or form.get("confirmed") != "yes":
            raise HTTPException(409, "Confirm only after the employer has accepted your application.")
        mark_confirmed(db, task, "Owner verified submission on the employer site or in a confirmation email.")
        db.commit()
        from shared.google_sheet import sync_pending
        background_tasks.add_task(sync_pending, db.get_bind())
        return RedirectResponse(f"/apply-tasks/{task_id}", 303)

    @app.get("/mail-updates")
    def mail_page(request: Request, db=Depends(get_db)):
        events = list(db.scalars(select(MailEvent).order_by(MailEvent.received_at.desc()).limit(100)))
        ids = {p for e in events for p in json.loads(e.candidates)}
        postings = {p.id: p for p in db.scalars(select(Posting).where(Posting.id.in_(ids)))} if ids else {}
        return render(request, "mail_updates.html", {"events": events, "postings": postings,
            "connection": db.get(MailConnection, 1)})

    @app.post("/mail-updates/{event_id}/link")
    async def link_mail(request: Request, event_id: int, background_tasks: BackgroundTasks, db=Depends(get_db)):
        event = db.scalar(select(MailEvent).where(MailEvent.id == event_id).with_for_update())
        if not event or event.state != "review":
            raise HTTPException(409, "This update has already been reviewed.")
        form = await request.form()
        try:
            posting_id = int(form.get("posting_id", "0"))
        except ValueError:
            raise HTTPException(422, "Choose an application") from None
        if posting_id == 0:
            event.state = "ignored"
        elif posting_id in json.loads(event.candidates):
            from shared.mail_tracking import apply_event
            apply_event(db, event, db.get(Posting, posting_id))
        else:
            raise HTTPException(422, "Choose one of the matching applications.")
        db.commit()
        from shared.google_sheet import sync_pending
        background_tasks.add_task(sync_pending, db.get_bind())
        return RedirectResponse("/mail-updates", 303)

    @app.post("/mail-connection/setup")
    def setup_mail(db=Depends(get_db)):
        settings = db.get(ApplicantSettings, 1)
        if not settings:
            raise HTTPException(422, "Save your application email first.")
        email = json_data(settings.data)["email"]
        token = secrets.token_urlsafe(40)
        row = db.get(MailConnection, 1)
        if row is None:
            row = MailConnection(id=1)
            db.add(row)
        row.email, row.token_digest, row.enabled = email, hashlib.sha256(token.encode()).hexdigest(), True
        row.last_received_at = None
        db.commit()
        scripts = Path(__file__).resolve().parents[1] / "scripts"
        config = "\nfunction configureConnection() {\n PropertiesService.getScriptProperties().setProperties(" + json.dumps({
            "RADAR_URL": "https://internship-alerts-1412.onrender.com", "RADAR_TOKEN": token}) + ");\n connectRadar();\n}\n"
        output = BytesIO()
        with ZipFile(output, "w") as archive:
            archive.writestr("Code.gs", (scripts / "gmail-monitor.gs").read_text() + config)
            archive.writestr("appsscript.json", (scripts / "gmail-appsscript.json").read_text())
            archive.writestr("START-HERE.txt", f"Sign in to script.google.com with {email}. Create a private project.\nPaste Code.gs into the editor. Enable Show appsscript.json manifest in Project Settings and replace the manifest with appsscript.json.\nRun configureConnection and approve Gmail read-only access. A five-minute trigger will be installed.\nKeep these files private: Code.gs contains your revocable Radar connection token. Do not publish this project as a web app.\nCheck Radar > Email updates for the first successful check.\n")
        return Response(output.getvalue(), media_type="application/zip", headers={
            "Content-Disposition": 'attachment; filename="radar-gmail-setup.zip"'})

    @app.post("/mail-connection/disconnect")
    def disconnect_mail(db=Depends(get_db)):
        row = db.get(MailConnection, 1)
        if row:
            row.enabled = False
            row.token_digest = secrets.token_hex(32)
            db.commit()
        return RedirectResponse("/apply-settings", 303)

    @app.post("/integrations/mail")
    async def mail_bridge(request: Request, background_tasks: BackgroundTasks, db=Depends(get_db)):
        # Dedicated revocable token; this endpoint never grants access to resumes.
        row = db.scalar(select(MailConnection).where(MailConnection.id == 1).with_for_update())
        header = request.headers.get("authorization", "")
        supplied = header.removeprefix("Bearer ") if header.startswith("Bearer ") else ""
        if not row or not row.enabled or not hmac.compare_digest(hashlib.sha256(supplied.encode()).hexdigest(), row.token_digest):
            raise HTTPException(401, "Mailbox connection not authorized")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 250_000:
                raise HTTPException(413, "Mail batch too large")
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict) or str(payload.get("email", "")).lower() != row.email.lower():
                raise ValueError("Sign in to the Gmail account saved in Application details.")
            from shared.mail_tracking import candidates, ingest
            if payload.get("action") == "config":
                result = {"ok": True, "applications": [{"id": p.id, "company": p.company_name, "role": p.title} for p in candidates(db)]}
            elif payload.get("action") == "events":
                messages = payload.get("messages", [])
                if not isinstance(messages, list) or len(messages) > 30 or any(not isinstance(m, dict) for m in messages):
                    raise ValueError("Invalid mail batch.")
                result = {"ok": True, "accepted": ingest(db, row.email, messages)}
            else:
                raise ValueError("Unknown mailbox operation.")
        except (ValueError, TypeError):
            raise HTTPException(422, "Invalid mailbox request or wrong Gmail account.") from None
        row.last_received_at = utcnow()
        db.commit()
        from shared.google_sheet import sync_pending
        background_tasks.add_task(sync_pending, db.get_bind())
        return result

    owner_app.include_router(app)
