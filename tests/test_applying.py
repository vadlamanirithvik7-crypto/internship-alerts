import hashlib
import json
from datetime import timedelta, timezone
from io import BytesIO

import pytest
from pypdf import PdfWriter
from sqlalchemy import select
from shared.db import (Posting, ApplicantSettings, StoredResume, ApplicationTask,
                       ApplicationSync, MailEvent, utcnow)
from shared.applying import store_resume, validate_profile
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


def test_profile_typo():
    with pytest.raises(ValueError):
        validate_profile({"first_name":"T", "last_name":"A", "email":"vadlam,ani.rithvik@gmail.com"})


def test_resume_slots_and_archived_versions(db):
    p, resume = setup_data(db)
    original = resume.id
    store_resume(db, "Hardware", "hardware.pdf", pdf_bytes())
    store_resume(db, "Embedded", "embedded.pdf", pdf_bytes())
    with pytest.raises(ValueError, match="Three resumes"):
        store_resume(db, "Fourth", "fourth.pdf", pdf_bytes())
    resume.active = False
    db.commit()
    replacement = store_resume(db, "Software v2", "new.pdf", pdf_bytes())
    assert replacement.id != original
    assert db.get(StoredResume, original).content == pdf_bytes()


def test_resume_rejects_non_pdf(db):
    setup_data(db)
    for name, data in [("resume.html", b'<script>bad</script>'), ('resume.pdf', b'%PDF-garbage'), ('huge.pdf', b'%PDF-' + b'x'*2_000_000)]:
        with pytest.raises(ValueError):
            store_resume(db, "Bad file", name, data)


def save_settings(c):
    return c.post('/apply-settings', data={'first_name':'Test', 'last_name':'Applicant', 'email':'applicant@example.com'})


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
    p.status='applied'
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
