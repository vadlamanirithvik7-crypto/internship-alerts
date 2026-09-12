import base64
import hashlib
import json
from datetime import timedelta
import pytest
from sqlalchemy import select
from shared.db import AutoApplication, AutoApplySettings, Posting, utcnow, init_db
from shared import auto_apply as queue
from applicant.forms import answer_for, supported
from test_app import client
from test_applying import setup_data


def setup(db):
    p,r=setup_data(db)
    profile=db.get(__import__('shared.db',fromlist=['ApplicantSettings']).ApplicantSettings,1)
    d=json.loads(profile.data);d.update(phone='+15555555555',location='Austin, TX');profile.data=json.dumps(d)
    cfg=queue.settings(db)
    cfg.software_resume_id=cfg.hardware_resume_id=cfg.embedded_resume_id=r.id
    cfg.token_digest=hashlib.sha256(b'test-token').hexdigest()
    queue.control(db,cfg,'running')
    p.first_seen_at=utcnow()+timedelta(seconds=1)
    db.commit()
    return p,r,cfg


def test_queue_resume_snapshot_dedup_and_old_listing(db):
    p,r,cfg=setup(db)
    duplicate=Posting(company_name=p.company_name,title=p.title,location='Denver, CO',url=p.url+'?copy=1',
        source='test',raw_hash='duplicate',target_eligible=True,first_seen_at=p.first_seen_at)
    old=Posting(company_name='Old',title=p.title,location=p.location,url=p.url+'/old',source='test',raw_hash='old',
        target_eligible=True,first_seen_at=utcnow()-timedelta(days=1))
    db.add_all([duplicate,old]);db.flush()
    queue.replenish(db,cfg)
    tasks=list(db.scalars(select(AutoApplication)))
    assert len(tasks)==1 and tasks[0].resume_id==r.id
    cfg.answers='{"new":"answer"}'
    assert tasks[0].answers=='{}'


def test_pause_stop_and_submission_gate(db):
    p,r,cfg=setup(db); task=queue.claim(db,cfg)
    assert task and queue.permit(db,cfg,task)
    queue.control(db,cfg,'paused')
    assert not queue.permit(db,cfg,task) and task.state=='queued'
    queue.control(db,cfg,'running'); task=queue.claim(db,cfg)
    task.submission_started_at=utcnow();task.state='submitting'
    queue.control(db,cfg,'stopped')
    assert task.state=='uncertain'
    queue.control(db,cfg,'running')
    assert queue.claim(db,cfg) is None
    queue.receipt(db,task,{'state':'submitted','confirmation':'Your application has been received'})
    assert p.status=='applied' and task.state=='submitted'
    queue.receipt(db,task,{'state':'submitted','confirmation':'duplicate receipt'})
    assert task.confirmation=='Your application has been received'


def test_lease_expiry_and_migration_dont_repeat(db):
    p,r,cfg=setup(db);t=queue.claim(db,cfg)
    t.state='submitting';t.submission_started_at=utcnow();t.claimed_at=utcnow()-timedelta(minutes=3)
    db.commit();init_db(db.get_bind())
    assert queue.claim(db,cfg) is None and t.state=='uncertain'


def test_manual_dismiss_prevents_submit(db):
    p,r,cfg=setup(db);t=queue.claim(db,cfg);p.status='not_interested'
    assert not queue.permit(db,cfg,t)


def test_unknown_answers_and_host_guards():
    assert answer_for('First name *',{'first_name':'Test'}, {})=='Test'
    assert answer_for('Will you require sponsorship?',{}, {}) is None
    assert answer_for('Will you require sponsorship?',{}, {'Will you require sponsorship?':'Yes'})=='Yes'
    assert answer_for('Will you NOT require sponsorship?',{}, {'Will you require sponsorship?':'Yes'}) is None
    assert supported('https://job-boards.greenhouse.io/company/jobs/123')
    assert not supported('https://greenhouse.io.evil.example/jobs')
    assert not supported('https://jobright.ai/jobs/info/123')


def test_worker_auth_demo_and_permit_once(client, monkeypatch):
    c,m,Session=client
    with Session() as db: p,r,cfg=setup(db)
    monkeypatch.setenv('ADMIN_PASSWORD','private')
    assert c.post('/integrations/auto-apply',json={'action':'claim'}).status_code==401
    h={'Authorization':'Bearer test-token'}
    response=c.post('/integrations/auto-apply',headers=h,json={'action':'claim'})
    assert response.status_code==200
    task=response.json()['task'];assert base64.b64decode(task['resume']).startswith(b'%PDF')
    data={'action':'permit','task_id':task['id'],'claim_token':task['claim_token']}
    assert c.post('/integrations/auto-apply',headers=h,json=data).json()['allowed']
    assert not c.post('/integrations/auto-apply',headers=h,json=data).json()['allowed']
    assert c.get('/autopilot').status_code==401
    assert c.get('/autopilot',auth=('owner','private')).status_code==200
    monkeypatch.setattr(m,'DEMO',True)
    assert c.post('/integrations/auto-apply',headers=h,json=data).status_code==404


def test_headless_form_receipt_and_pause_gate(monkeypatch):
    import asyncio
    from playwright.async_api import async_playwright
    from applicant import worker
    from test_applying import pdf_bytes
    html='''<p>Fixture</p><h1>Software Engineering Intern</h1><form onsubmit="event.preventDefault(); document.body.innerHTML='Your application has been received';">
    <label>First name<input required name="first"></label><label>Last name<input required name="last"></label>
    <label>Email<input required type="email"></label><label>Resume<input type="file" required></label>
    <button type="submit">Submit application</button></form>'''
    class Fake:
        def __init__(self,allowed): self.allowed=allowed;self.calls=[]
        async def call(self,action,task=None,**data):
            self.calls.append((action,data))
            return {'allowed':self.allowed if action=='permit' else True,'mode':'running'}
    async def fixture_route(route):
        await route.fulfill(status=200,content_type='text/html',body=html)
    monkeypatch.setattr(worker,'public_request',fixture_route)
    task={'id':1,'claim_token':'x','url':'https://jobs.lever.co/fixture/1','title':'Software Engineering Intern',
          'company':'Fixture','profile':{'first_name':'Test','last_name':'Applicant','email':'test@example.com'},
          'answers':{},'filename':'resume.pdf','resume':base64.b64encode(pdf_bytes()).decode()}
    async def check():
        async with async_playwright() as p:
            confirmed=Fake(True);await worker.execute(confirmed,task,p,'')
            receipts=[d for a,d in confirmed.calls if a=='result']
            assert receipts[-1]['state']=='submitted'
            denied=Fake(False);await worker.execute(denied,task,p,'')
            assert not any(d.get('state')=='submitted' for a,d in denied.calls)
    asyncio.run(check())


def test_missing_answer_prevents_browser_submission(monkeypatch):
    import asyncio
    from playwright.async_api import async_playwright
    import httpx
    from applicant.forms import fill_form
    from test_applying import pdf_bytes
    async def check():
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True)
            page=await browser.new_page()
            await page.set_content('<form><label>Are you a US citizen?<select required><option value="">Select</option><option>Yes</option><option>No</option></select></label></form>')
            async with httpx.AsyncClient() as client:
                missing,_=await fill_form(page,{'profile':{},'answers':{}},client)
            assert missing==['Are you a US citizen?']
            await browser.close()
    asyncio.run(check())


def test_citizenship_cancellation_does_not_mark_applied(db):
    p,r,cfg=setup(db); task=queue.claim(db,cfg)
    queue.receipt(db,task,{'state':'cancelled','detail':'US citizenship question; answer No.'})
    assert task.state=='cancelled' and not task.submission_started_at
    assert p.status!='applied' and p.applied_at is None
    assert queue.claim(db,cfg) is None
    # Cancellation must never conceal a submission that might have succeeded.
    task.state='submitting';task.submission_started_at=utcnow()
    queue.receipt(db,task,{'state':'cancelled'})
    assert task.state=='uncertain'


@pytest.mark.parametrize('question', [
    'Are you a U.S. citizen?', 'Do you hold United States citizenship?',
    'Are you a citizen of the United States of America?', 'Are you an American citizen?',
])
def test_citizenship_question_wording(question):
    from applicant.forms import asks_us_citizenship
    assert asks_us_citizenship(question)
    assert not asks_us_citizenship('Are you legally authorized to work in the US?')
    assert not asks_us_citizenship('What is your country of citizenship?')


@pytest.mark.parametrize('form', [
    '<label>Are you a U.S. citizen?<select><option>Yes</option><option>No</option></select></label>',
    '<fieldset><legend>Are you a citizen of the United States?</legend><label>Yes<input type="radio" name="citizen"></label><label>No<input type="radio" name="citizen"></label></fieldset>',
])
def test_citizenship_question_cancels_before_permit(monkeypatch, form):
    import asyncio
    from playwright.async_api import async_playwright
    from applicant import worker
    from applicant.forms import SKIP_CITIZENSHIP
    html='<p>Fixture</p><h1>Software Engineering Intern</h1><form>'+form+'<button>Submit application</button></form>'
    class Fake:
        def __init__(self): self.calls=[]
        async def call(self,action,task=None,**data):
            self.calls.append((action,data)); return {'allowed':True,'mode':'running'}
    async def fixture_route(route):
        await route.fulfill(status=200,content_type='text/html',body=html)
    monkeypatch.setattr(worker,'public_request',fixture_route)
    task={'id':1,'claim_token':'x','url':'https://jobs.lever.co/fixture/1',
          'title':'Software Engineering Intern','company':'Fixture','profile':{},
          'answers':{SKIP_CITIZENSHIP:'Yes'}}
    async def check():
        async with async_playwright() as p:
            connection=Fake(); await worker.execute(connection,task,p,'')
            assert not any(action=='permit' for action,_ in connection.calls)
            receipts=[data for action,data in connection.calls if action=='result']
            assert receipts[-1]['state']=='cancelled'
            assert 'answer is No' in receipts[-1]['detail']
    asyncio.run(check())
