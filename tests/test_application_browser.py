"""Actual Chromium against intercepted fixtures only; no employer submissions."""
import json
import pytest
from sqlalchemy import select
from shared.db import ApplicationSync, Posting
from shared.applying import claim_next, queue_application
from test_applying import setup_data
from applicant.worker import process
from applicant.forms import PublicForm

FORM = '''<!doctype html><html><body><h1>Fixture Labs</h1>
<form id="application-form">
<label>Full name<input name="name" required></label>
<label>Email<input name="email" type="email" required></label>
<label>Resume<input name="resume" type="file" required></label>
<label>Are you authorized to work in the US? *<select name="authorization" required><option value="">Choose</option><option value="yes">Yes</option><option value="no">No</option></select></label>
<label><input name="agreement" type="checkbox" required>I confirm my answers are accurate *</label>
<label>Additional information<textarea name="extra"></textarea></label>
<button type="submit">Submit application</button></form>
<script>document.querySelector('form').addEventListener('submit',e=>{
 e.preventDefault(); window.receipt=Object.fromEntries(new FormData(e.target));
 window.resumeName=window.receipt.resume.name;
 e.target.remove(); document.body.insertAdjacentHTML('beforeend','<h2>Your application has been submitted</h2>');
});</script></body></html>'''


@pytest.fixture
def page():
    pw = pytest.importorskip('playwright.sync_api')
    with pw.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        page = browser.new_page()
        # Every request is fulfilled locally, even when the fixture has a real ATS host.
        page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=FORM))
        yield page
        browser.close()


def ready_task(db, page, review=False):
    p, resume = setup_data(db, review=review)
    task = queue_application(db, p.id, resume.id)
    db.commit()
    task = claim_next(db)
    process(db, task, page)
    db.refresh(task)
    assert task.state == 'needs_info'
    assert page.evaluate('window.receipt === undefined')
    fields=json.loads(task.questions)
    assert len([f for f in fields if not f['answered']]) == 3
    task.answers=json.dumps({f['key']: 'yes' if f['required'] else '' for f in fields if not f['answered']})
    task.state='queued';db.commit()
    return task, p


def test_browser_questions_resume_submission_receipt(db, page):
    task,p=ready_task(db,page)
    process(db,claim_next(db),page)
    db.refresh(task);db.refresh(p)
    assert task.state=='submitted', task.detail
    assert p.status=='applied'
    assert db.get(ApplicationSync,p.id).state=='pending'
    assert page.evaluate('window.receipt.email')=='applicant@example.com'
    assert page.evaluate('window.receipt.authorization')=='yes'
    assert page.evaluate('window.resumeName')=='resume.pdf'
    assert claim_next(db) is None


def test_browser_review_waits_for_owner(db, page):
    from shared.applying import form_digest
    task,p=ready_task(db,page,review=True)
    process(db,claim_next(db),page)
    db.refresh(task)
    assert task.state=='needs_review'
    assert page.evaluate('window.receipt === undefined')
    task.reviewed_digest=form_digest(json.loads(task.questions));task.state='queued';db.commit()
    process(db,claim_next(db),page)
    db.refresh(task)
    assert task.state=='submitted'


def test_browser_missing_receipt_never_retries(db, page, monkeypatch):
    task,p=ready_task(db,page)
    monkeypatch.setattr(PublicForm,'confirmation',lambda self:None)
    process(db,claim_next(db),page)
    db.refresh(task);db.refresh(p)
    assert task.state=='uncertain' and p.applied_at is None
    assert page.evaluate('window.receipt.email')=='applicant@example.com'
    assert claim_next(db) is None


def test_browser_conditional_question_pauses_before_submit(db, page):
    p,resume=setup_data(db)
    html=FORM.replace('<select name="authorization" required>', '<select name="authorization" required onchange="if(!document.querySelector(\'#extra-question\'))this.insertAdjacentHTML(\'afterend\',\'<label id=&quot;extra-question&quot;>Sponsorship details *<input name=&quot;details&quot; required></label>\')">')
    page.unroute('**/*')
    page.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body=html))
    task=queue_application(db,p.id,resume.id);db.commit()
    process(db,claim_next(db),page);db.refresh(task)
    task.answers=json.dumps({f['key']:'yes' if f['required'] else '' for f in json.loads(task.questions) if not f['answered']})
    task.state='queued';db.commit()
    process(db,claim_next(db),page);db.refresh(task)
    assert task.state=='needs_info'
    assert any(f['name']=='details' for f in json.loads(task.questions))
    assert page.evaluate('window.receipt === undefined')


def test_browser_custom_dropdown_handoff_does_not_submit(db,page):
    p,resume=setup_data(db)
    page.unroute('**/*')
    html=FORM.replace('<input name="name" required>', '<input role="combobox" name="name" required>')
    page.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body=html))
    queue_application(db,p.id,resume.id);db.commit()
    task=claim_next(db);process(db,task,page);db.refresh(task)
    assert task.state=='needs_action'
    assert page.evaluate('window.receipt === undefined')


def test_lever_javascript_submit_and_select2_native_options(db,page):
    p,resume=setup_data(db)
    html=FORM.replace('<button type="submit">Submit application</button>', '<button type="button" id="btn-submit" onclick="this.form.requestSubmit()">Submit application</button>')
    html=html.replace('<label>Additional information', '<div class="application-question"><div class="application-label">School ✱</div><div class="application-field"><select name="school" class="select2-hidden-accessible" required><option value="">Choose</option><option value="test-school">Test University</option></select><span class="select2-selection" role="combobox"></span></div></div><label>Additional information')
    page.unroute('**/*')
    page.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body=html))
    task=queue_application(db,p.id,resume.id);db.commit()
    process(db,claim_next(db),page);db.refresh(task)
    assert task.state=='needs_info'
    fields=json.loads(task.questions)
    task.answers=json.dumps({f['key']:('test-school' if f['name']=='school' else 'yes' if f['required'] else '') for f in fields if not f['answered']})
    task.state='queued';db.commit()
    process(db,claim_next(db),page);db.refresh(task)
    assert task.state=='submitted',task.detail
    assert page.evaluate('window.receipt.school')=='test-school'
