import hashlib
import json
from datetime import timedelta, timezone
from io import BytesIO

import pytest
from pypdf import PdfWriter
from sqlalchemy import select
from shared.db import (Posting, ApplicantSettings, StoredResume, ApplicationTask,
                       ApplicationSync, MailEvent, utcnow)
from shared.applying import (store_resume, queue_application, claim_next, mark_confirmed,
                             application_url, validate_profile, owned_transition)
from test_app import client


def pdf_bytes():
    out = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(out)
    return out.getvalue()


def setup_data(db, review=False):
    db.add(ApplicantSettings(id=1, data=json.dumps({"first_name":"Test", "last_name":"Applicant", "email":"applicant@example.com"}), review_before_submit=review))
    db.flush()
    resume = store_resume(db, "Software", "resume.pdf", pdf_bytes())
    p = Posting(company_name="Fixture Labs", title="Software Engineering Intern", location="Austin, TX", term="Summer 2027", source="lever", first_seen_at=utcnow(), raw_hash="fixture-job", url="https://jobs.lever.co/fixture/11111111-1111-1111-1111-111111111111", target_eligible=True)
    db.add(p)
    db.flush()
    db.commit()
    return p, resume


def test_profile_typo_and_host_allowlist():
    with pytest.raises(ValueError):
        validate_profile({"first_name":"T", "last_name":"A", "email":"vadlam,ani.rithvik@gmail.com"})
    assert application_url("https://jobs.lever.co/a/11111111-1111-1111-1111-111111111111")[1].endswith('/apply')
    for url in ["https://jobs.lever.co.evil.example/a/123", "http://jobs.lever.co/a/123", "https://127.0.0.1/", "https://jobs.lever.co@evil.example/a/123", "https://boards.greenhouse.io:8443/a/jobs/123", "https://jobs.lever.co/a/../b"]:
        assert application_url(url) is None


def test_resume_slots_immutable_tasks_and_double_tap(db):
    p, resume = setup_data(db)
    task = queue_application(db, p.id, resume.id)
    db.commit()
    assert queue_application(db, p.id, resume.id).id == task.id
    store_resume(db, "Hardware", "hardware.pdf", pdf_bytes())
    store_resume(db, "Embedded", "embedded.pdf", pdf_bytes())
    with pytest.raises(ValueError, match="Three resumes"):
        store_resume(db, "Fourth", "fourth.pdf", pdf_bytes())
    resume.active = False
    db.commit()
    replacement = store_resume(db, "Software v2", "new.pdf", pdf_bytes())
    assert replacement.id != task.resume_id
    assert db.get(StoredResume, task.resume_id).content == pdf_bytes()


def test_resume_rejects_non_pdf(db):
    setup_data(db)
    for name, data in [("resume.html", b'<script>bad</script>'), ('resume.pdf', b'%PDF-garbage'), ('huge.pdf', b'%PDF-' + b'x'*2_000_000)]:
        with pytest.raises(ValueError):
            store_resume(db, "Bad file", name, data)


def test_queue_never_marks_applied_until_confirmation(db):
    p, resume = setup_data(db)
    task = queue_application(db, p.id, resume.id)
    db.commit()
    task = claim_next(db)
    assert task.state == 'running' and p.applied_at is None
    assert claim_next(db) is None
    token = task.claim_token
    assert not owned_transition(db, task.id, 'wrong-token', state='submitted')
    assert owned_transition(db, task.id, token, state='submitting', submission_started_at=utcnow())
    mark_confirmed(db, task, 'Fixture confirmation')
    db.commit()
    first = p.applied_at
    assert p.status == 'applied' and first
    assert db.get(ApplicationSync, p.id).state == 'pending'
    mark_confirmed(db, task, 'Same receipt')
    db.commit()
    assert p.applied_at == first


def test_stale_submit_never_requeues(db):
    p, resume = setup_data(db)
    task = queue_application(db, p.id, resume.id)
    db.commit()
    task = claim_next(db)
    task.state = 'submitting'
    task.claimed_at = utcnow() - timedelta(minutes=20)
    task.submission_started_at = task.claimed_at
    db.commit()
    assert claim_next(db) is None
    db.refresh(task)
    assert task.state == 'uncertain' and p.applied_at is None


def test_stale_preparation_can_recover(db):
    p, resume = setup_data(db)
    queue_application(db, p.id, resume.id)
    db.commit()
    task = claim_next(db)
    first_token = task.claim_token
    task.claimed_at = utcnow() - timedelta(minutes=20)
    db.commit()
    recovered = claim_next(db)
    assert recovered.state == 'running' and recovered.claim_token != first_token
    assert not owned_transition(db, task.id, first_token, state='submitting')


def save_settings(c):
    return c.post('/apply-settings', data={'first_name':'Test', 'last_name':'Applicant', 'email':'applicant@example.com'})


def test_private_phone_flow_and_demo_isolation(client, monkeypatch):
    c, _, Session = client
    assert save_settings(c).status_code == 200
    upload = c.post('/resumes', data={'name':'Software'}, files={'resume':('software.pdf',pdf_bytes(),'application/pdf')})
    assert upload.status_code == 200
    with Session() as db:
        resume_id = db.scalar(select(StoredResume.id))
    assert c.get('/jobs/1/apply').status_code == 200
    response = c.post('/jobs/1/apply', data={'resume_id':resume_id})
    assert response.status_code == 200 and 'needs action' in response.text.lower()
    with Session() as db:
        task = db.scalar(select(ApplicationTask))
        tid = task.id
        assert db.get(Posting, 1).applied_at is None
    c.post('/jobs/1/apply', data={'resume_id':resume_id})
    with Session() as db:
        assert len(list(db.scalars(select(ApplicationTask)))) == 1
    assert c.post(f'/apply-tasks/{tid}/confirm',data={}).status_code == 409
    assert c.post(f'/apply-tasks/{tid}/confirm',data={'confirmed':'yes'}).status_code == 200
    for path in ['/apply-settings','/apply-tasks','/mail-updates',f'/resumes/{resume_id}/download']:
        assert c.get(path).status_code == 200
        assert c.get('/demo'+path).status_code == 404
    monkeypatch.setenv('ADMIN_PASSWORD','private')
    for path in ['/apply-settings','/apply-tasks','/mail-updates',f'/resumes/{resume_id}/download']:
        assert c.get(path).status_code == 401


def test_missing_questions_validated_and_submission_cannot_retry(client):
    c, _, Session = client
    save_settings(c)
    c.post('/resumes',data={'name':'Software'},files={'resume':('r.pdf',pdf_bytes(),'application/pdf')})
    c.post('/jobs/1/apply',data={'resume_id':1})
    question={'key':'question1','label':'Eligible?','type':'select-one','required':True,'answered':False,'maxlength':100,'options':[{'label':'Yes','value':'yes'}],'value':''}
    with Session() as db:
        task=db.scalar(select(ApplicationTask)); tid=task.id
        task.state='needs_info'; task.questions=json.dumps([question]);db.commit()
    assert c.post(f'/apply-tasks/{tid}/answers',data={'question1':'invented'}).status_code == 422
    assert c.post(f'/apply-tasks/{tid}/answers',data={'question1':'yes'}).status_code == 200
    with Session() as db:
        task=db.get(ApplicationTask,tid)
        assert task.state=='queued' and json.loads(task.answers)['question1']=='yes'
        task.state='uncertain';task.submission_started_at=utcnow();db.commit()
    assert c.post(f'/apply-tasks/{tid}/retry').status_code == 409
    assert c.post(f'/apply-tasks/{tid}/cancel').status_code == 409


def test_mail_token_wrong_account_rotation_and_demo(client, monkeypatch):
    from zipfile import ZipFile
    import re
    c, _, _ = client
    save_settings(c)
    setup = c.post('/mail-connection/setup')
    code = ZipFile(BytesIO(setup.content)).read('Code.gs').decode()
    token = re.search(r'"RADAR_TOKEN": "([^"]+)"',code)[1]
    monkeypatch.setenv('ADMIN_PASSWORD','private')
    headers={'Authorization':'Bearer '+token}
    assert c.post('/integrations/mail',json={'action':'config','email':'applicant@example.com'}).status_code == 401
    assert c.post('/integrations/mail',headers=headers,json={'action':'config','email':'wrong@example.com'}).status_code == 422
    assert c.post('/integrations/mail',headers=headers,json={'action':'config','email':'applicant@example.com'}).status_code == 200
    assert c.post('/demo/integrations/mail',headers=headers,json={'action':'config','email':'applicant@example.com'}).status_code == 404
    c.post('/mail-connection/disconnect',auth=('owner','private'))
    assert c.post('/integrations/mail',headers=headers,json={'action':'config','email':'applicant@example.com'}).status_code == 401


def test_mail_dedupe_match_stale_and_ambiguous(db):
    from shared.mail_tracking import ingest
    p, resume = setup_data(db)
    task=queue_application(db,p.id,resume.id)
    mark_confirmed(db,task,'Fixture')
    p.applied_at=utcnow()-timedelta(days=2);p.status_updated_at=p.applied_at
    db.commit()
    msg={'id':'1234567890abcdef','timestamp':int((utcnow()-timedelta(hours=1)).replace(tzinfo=timezone.utc).timestamp()*1000),
         'subject':'Fixture Labs: Software Engineering Intern interview invitation',
         'sender':'Recruiting <recruiting@example.com>','text':'We would like to schedule an interview.'}
    assert ingest(db,'applicant@example.com',[msg])==1
    db.commit()
    assert p.status=='interview'
    assert ingest(db,'applicant@example.com',[msg])==0
    stale=dict(msg,id='2234567890abcdef', timestamp=int((utcnow()-timedelta(days=1)).replace(tzinfo=timezone.utc).timestamp()*1000),text='We are not moving forward with your application.')
    ingest(db,'applicant@example.com',[stale]);db.commit()
    assert p.status=='interview'
    ambiguous=dict(msg,id='3234567890abcdef',subject='Fixture Labs application update')
    ingest(db,'applicant@example.com',[ambiguous]);db.commit()
    event=db.scalar(select(MailEvent).where(MailEvent.message_id==ambiguous['id']))
    assert event.state=='review' and event.posting_id is None
    unrelated=dict(msg,id='4234567890abcdef',subject='Grocery receipt',text='Your order has arrived')
    assert ingest(db,'applicant@example.com',[unrelated])==0


def test_mail_classification_does_not_use_quoted_old_rejection():
    from shared.mail_tracking import classify
    assert classify('Please schedule an interview.\nOn Tuesday someone wrote:\nWe are not moving forward.')=='interview'
    assert classify('Please complete the coding assessment.')=='next_steps'
    assert classify('Thank you for applying.')=='applied'


def test_same_employer_url_cannot_create_second_submission(db):
    p,resume=setup_data(db)
    task=queue_application(db,p.id,resume.id)
    db.commit()
    other=Posting(company_name=p.company_name,title=p.title,source='tracker',first_seen_at=utcnow(),raw_hash='duplicate-fixture',url=p.url+'/apply?utm_source=tracker')
    db.add(other);db.flush()
    assert queue_application(db,other.id,resume.id).id==task.id
