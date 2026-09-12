"""Persistent local-worker queue. Submission receipts survive stops and retries."""
import hashlib
import json
import secrets
from datetime import timedelta
from sqlalchemy import select
from shared.db import AutoApplySettings, AutoApplication, ApplicantSettings, StoredResume, Posting, utcnow
from shared.role_search import role_tags
from shared.applying import validate_profile, mark_confirmed
from poller.application_links import application_url, _company_key, _match_title_key
from poller.normalize import canonical_url


def settings(db):
    row = db.scalar(select(AutoApplySettings).where(AutoApplySettings.id == 1).with_for_update())
    if row is None:
        row = AutoApplySettings(id=1)
        db.add(row)
        db.flush()
    return row


def identity(p):
    # Cross-source and multi-location copies of the same role are one application.
    return hashlib.sha256((_company_key(p.company_name) + '|' + _match_title_key(p.title)).encode()).hexdigest()


def destination_key(url):
    return hashlib.sha256(canonical_url(url).encode()).hexdigest() if url else None


def ready(db, cfg):
    profile = db.get(ApplicantSettings, 1)
    if not profile:
        raise ValueError('Save your application details in Resumes & details first.')
    data = validate_profile(json.loads(profile.data))
    if not data.get('phone') or not data.get('location'):
        raise ValueError('Save your phone number and location first.')
    for rid in (cfg.software_resume_id, cfg.hardware_resume_id, cfg.embedded_resume_id):
        resume = db.get(StoredResume, rid) if rid else None
        if not resume or not resume.active:
            raise ValueError('Choose an active resume for software, hardware, and embedded roles.')
    return data


def control(db, cfg, mode):
    if mode not in ('running', 'paused', 'stopped'):
        raise ValueError('Unknown worker control.')
    if mode == cfg.mode:
        return
    if mode == 'running':
        ready(db, cfg)
        if not cfg.token_digest:
            raise ValueError('Connect your Mac first.')
        cfg.started_at = cfg.started_at or utcnow()
    cfg.mode = mode
    cfg.generation += 1
    for task in db.scalars(select(AutoApplication).where(AutoApplication.state.in_(['running', 'submitting']))):
        if task.submission_started_at:
            task.state = 'uncertain'
            task.detail = 'Stopped during submission. Check employer confirmation before any retry.'
        else:
            task.state, task.claim_token = 'queued', None
            task.detail = 'Paused; preparation will restart when resumed.'


def replenish(db, cfg, *, include_existing=False):
    if not cfg.started_at and not include_existing:
        return
    if not include_existing and cfg.last_queued_at and cfg.last_queued_at > utcnow() - timedelta(seconds=60):
        return
    cfg.last_queued_at = utcnow()
    profile = ready(db, cfg)
    # Stable task snapshots keep resume and answers consistent during a run.
    query = select(Posting).where(Posting.target_eligible.is_(True))
    if not include_existing:
        query = query.where(Posting.first_seen_at >= cfg.started_at)
    rows = list(db.scalars(query.order_by(Posting.first_seen_at.desc(), Posting.id.desc())))
    tasks = list(db.scalars(select(AutoApplication)))
    known = {t.application_key for t in tasks}
    destinations = {t.destination_key for t in tasks if t.destination_key}
    applied = {identity(p) for p in db.scalars(select(Posting).where(Posting.applied_at.is_not(None)))}
    applied |= {identity(p) for p in rows if p.status in ('applied','interview','offer','rejected','not_interested')}
    for task in tasks:
        p = db.get(Posting, task.posting_id)
        if task.state == 'waiting_link' and p and application_url(p):
            key = destination_key(application_url(p))
            if key in destinations:
                task.state, task.detail = 'cancelled', 'Duplicate employer requisition.'
            else:
                task.target_url, task.state = application_url(p), 'queued'
                task.destination_key = key
                destinations.add(key)
    for p in rows:
        key = identity(p)
        if key in known or key in applied or p.closed_at:
            continue
        roles = role_tags(p.title)
        role = roles[0] if roles else ''
        rid = {'software':cfg.software_resume_id,'hardware':cfg.hardware_resume_id,'embedded':cfg.embedded_resume_id}.get(role)
        if not rid:
            continue
        url = application_url(p)
        dest_key = destination_key(url)
        if dest_key and dest_key in destinations:
            continue
        if dest_key:
            destinations.add(dest_key)
        db.add(AutoApplication(posting_id=p.id, resume_id=rid, application_key=key,
            target_url=url, destination_key=dest_key, applicant=json.dumps(profile), answers=cfg.answers,
            state='queued' if url else 'waiting_link',
            detail='Waiting for your Mac.' if url else 'Waiting for an exact employer application link.'))
        known.add(key)
    db.flush()


def available(db, task):
    p = db.get(Posting, task.posting_id)
    if not p or p.closed_at or not p.target_eligible or p.applied_at or p.status not in ('new','interested'):
        return False
    # Recheck duplicates, including manually applied copies, immediately before submit.
    for other in db.scalars(select(Posting).where(Posting.status.in_(['applied','interview','offer','rejected','not_interested']))):
        if identity(other) == task.application_key or (task.destination_key and destination_key(application_url(other)) == task.destination_key):
            return False
    return True


def claim(db, cfg):
    now = utcnow()
    cfg.last_seen_at = now
    # Expired leases never cause a second submission after an uncertain response.
    for t in db.scalars(select(AutoApplication).where(AutoApplication.state.in_(['running','submitting']))):
        if t.claimed_at and t.claimed_at < now - timedelta(seconds=90):
            t.state = 'uncertain' if t.submission_started_at else 'queued'
            t.detail = 'Worker disconnected during submission; check employer confirmation.' if t.submission_started_at else 'Worker disconnected; waiting to resume.'
    if cfg.mode != 'running':
        return None
    replenish(db, cfg)
    if db.scalar(select(AutoApplication.id).where(AutoApplication.state.in_(['running','submitting'])).limit(1)):
        return None
    # New discoveries stay ahead of the historical backlog.
    for t in db.scalars(select(AutoApplication).join(Posting, Posting.id == AutoApplication.posting_id)
                       .where(AutoApplication.state == 'queued')
                       .order_by(Posting.first_seen_at.desc(), AutoApplication.id)):
        if not available(db, t):
            t.state, t.detail = 'cancelled', 'Role closed, excluded, dismissed, or already applied.'
            continue
        t.claim_token = secrets.token_urlsafe(32)
        t.claimed_at = now
        t.generation = cfg.generation
        t.state, t.detail = 'running', 'Your Mac is preparing the employer form.'
        return t
    return None


def permit(db, cfg, task):
    allowed = (cfg.mode == 'running' and task.state == 'running'
               and task.generation == cfg.generation and available(db, task))
    if allowed:
        task.claimed_at = utcnow()
    return allowed


def receipt(db, task, payload):
    state = payload.get('state')
    if task.state == 'submitted':
        return
    if state == 'submitted':
        evidence = str(payload.get('confirmation', ''))[:600]
        if not task.submission_started_at or not evidence:
            raise ValueError('A submission receipt is required.')
        mark_confirmed(db, task, evidence)
        # Remove already indexed duplicates from the feed and preserve their IDs/history.
        for p in db.scalars(select(Posting).where(Posting.status.in_(['new','interested']))):
            if identity(p) == task.application_key:
                p.applied_at = task.submitted_at
                p.status = 'applied'
                p.status_updated_at = utcnow()
        return
    if task.submission_started_at:
        task.state = 'uncertain'
        task.detail = 'Submission was attempted without confirmation. Check the employer before retrying.'
    elif task.state == 'running':
        task.state = 'cancelled' if state == 'cancelled' else 'needs_input'
        task.detail = str(payload.get('detail', 'Please review the employer form.'))[:600]
        questions = payload.get('questions', [])
        if not isinstance(questions, list) or len(questions) > 80:
            raise ValueError('Invalid questions.')
        task.questions = json.dumps([str(q)[:300] for q in questions])
    task.updated_at = utcnow()
