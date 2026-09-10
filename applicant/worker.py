"""Free scheduled browser worker; no personal artifacts or form bodies in logs."""
import argparse
import ipaddress
import json
import socket
from functools import lru_cache
from urllib.parse import urlsplit

from shared.db import get_engine, init_db, get_session_factory, Posting, StoredResume, utcnow
from shared.applying import (claim_next, owned_transition, mark_confirmed,
                             application_url, json_data, form_digest)
from applicant.forms import PublicForm, Handoff


@lru_cache(maxsize=128)
def public_host(host):
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return bool(addresses) and all(ipaddress.ip_address(a[4][0]).is_global for a in addresses)
    except (OSError, ValueError):
        return False


def network_guard(route):
    p = urlsplit(route.request.url)
    if p.scheme in ("data", "blob"):
        return route.continue_()
    if p.scheme != "https" or p.port not in (None, 443) or p.username or not public_host(p.hostname):
        return route.abort()
    if route.request.resource_type in ("image", "media", "font"):
        return route.abort()
    route.continue_()


def process(db, task, page):
    task_id, token = task.id, task.claim_token
    boundary = False
    try:
        posting = db.get(Posting, task.posting_id)
        target = application_url(task.target_url)
        if posting.closed_at or posting.applied_at or posting.status not in ("new", "interested"):
            raise Handoff("This role is closed or already applied to. Check your application history.")
        if not target:
            raise Handoff("This application site requires manual completion.")
        provider, url = target
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.locator('form#application-form,form#application_form').wait_for(timeout=15000)
        landed = application_url(page.url)
        if not landed or landed != target:
            raise Handoff("The employer redirected to another application. Check its page before continuing.")
        form = PublicForm(page, provider)
        action = form.form.get_attribute("action") or ""
        if action.startswith(("http:", "https:", "//")) and urlsplit(action).hostname != urlsplit(url).hostname:
            raise Handoff("The application submits to a different service. Complete it on the employer site.")
        fields = form.inspect(json_data(task.applicant), json_data(task.answers))
        # All unknown questions are offered, including optional ones which can be skipped.
        missing = [f for f in fields if not f["answered"] and (f["type"] != "file" or f["required"])]
        if any(f["type"] == "file" for f in missing):
            raise Handoff("The employer requires another document. Upload it directly on the employer site.")
        if missing:
            owned_transition(db, task_id, token, state="needs_info", questions=json.dumps(fields),
                             detail="Answer the employer's questions below. Optional answers can be left blank.")
            return
        resume = db.get(StoredResume, task.resume_id)
        form.fill(fields, resume)
        # A conditional question or changed option must be answered before submitting.
        fresh = form.inspect(json_data(task.applicant), json_data(task.answers))
        if form_digest(fresh) != form_digest(fields):
            owned_transition(db, task_id, token, state="needs_info", questions=json.dumps(fresh),
                             detail="The employer revealed additional questions. Review them before continuing.")
            return
        errors = form.validation_errors()
        if errors:
            for error in errors:
                for field in fields:
                    if field["index"] == error["index"]:
                        field["answered"] = False
                        field["error"] = error["message"][:300]
            owned_transition(db, task_id, token, state="needs_info", questions=json.dumps(fields),
                             detail="Some answers did not pass the employer's validation. Correct them below.")
            return
        digest = form_digest(fields)
        if task.review_before_submit and task.reviewed_digest != digest:
            owned_transition(db, task_id, token, state="needs_review", questions=json.dumps(fields),
                             detail="The completed answers are ready for your review.")
            return
        form.verify_values(fields)
        button = form.submit_button()
        # Commit before the irreversible browser click. Crash => uncertain, never retry.
        if not owned_transition(db, task_id, token, state="submitting", questions=json.dumps(fields),
                                submission_started_at=utcnow(), detail="Submitting to the employer. Please do not submit another copy."):
            return
        boundary = True
        button.click(timeout=15000, no_wait_after=True)
        receipt = form.confirmation()
        if not receipt:
            raise RuntimeError("No confirmation")
        from shared.db import ApplicationTask
        db.expire_all()
        current = db.get(ApplicationTask, task_id)
        if current.state == "submitting" and current.claim_token == token:
            mark_confirmed(db, current, "Employer confirmation: " + receipt)
            db.commit()
    except Exception as error:
        db.rollback()
        if boundary:
            owned_transition(db, task_id, token, from_state="submitting", state="uncertain",
                detail="Submission may have completed, but no receipt was verified. Check the employer or your email; the app will not submit again.")
        else:
            detail = str(error) if isinstance(error, Handoff) else "The employer page could not be completed automatically. Open its application page or retry preparation."
            owned_transition(db, task_id, token, state="needs_action", detail=detail[:600])


def main(limit=3):
    from playwright.sync_api import sync_playwright
    engine = init_db(get_engine())
    Session = get_session_factory(engine)
    with Session() as db, sync_playwright() as pw:
        browser = None
        try:
            for _ in range(limit):
                task = claim_next(db)
                if task is None:
                    break
                if browser is None:
                    browser = pw.chromium.launch(headless=True)
                context = browser.new_context(service_workers="block", accept_downloads=False)
                context.route("**/*", network_guard)
                context.set_default_timeout(10000)
                try:
                    process(db, task, context.new_page())
                finally:
                    context.close()
                print("Application task processed; details are available in the private app.")
        finally:
            if browser:
                browser.close()
    from shared.google_sheet import sync_pending
    sync_pending(engine)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=3)
    main(max(1, min(parser.parse_args().limit, 5)))
