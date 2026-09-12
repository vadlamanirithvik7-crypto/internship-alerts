import json
from types import SimpleNamespace
import pytest
from shared.auto_input import question_view, pack_questions, validate_questions
from shared.db import AutoApplication, utcnow
from shared import auto_apply as queue
from test_auto_apply import setup
from test_app import client


def test_legacy_questions_are_separated_from_browser_failures():
    task=SimpleNamespace(state='needs_input',detail='Needs input',questions=json.dumps([
        'The employer page could not be confirmed as the selected role.',
        'The application moved to another portal that needs manual review.',
        'Employer account login required.', 'Could not identify the next application step.',
        'Unlabeled required field', 'Country*',
        'Are you willing to relocate? (choose an exact option)',
        'This role is based in San Jose. Can you work on site?*']))
    view=question_view(task)
    assert len(view['blockers'])==5 and len(view['fields'])==3
    assert view['fields'][1]['label']=='Are you willing to relocate?'
    assert view['label']=='Needs answers'


def waiting_task(db, questions):
    p,r,cfg=setup(db);task=queue.claim(db,cfg)
    queue.receipt(db,task,{'state':'needs_input','questions':questions,'detail':'Review the application.'})
    db.commit()
    return task.id,task.posting_id,task.resume_id


def test_answer_form_validates_choices_and_retries_while_worker_runs(client):
    c,m,Session=client
    questions=[{'label':'Country*','options':['United States','Canada']},'Earliest start date?']
    with Session() as db:
        tid,pid,rid=waiting_task(db,questions);version=question_view(db.get(AutoApplication,tid))['version']
    page=c.get(f'/autopilot/tasks/{tid}')
    assert 'Answer the missing questions' in page.text and 'name="answer_0"' in page.text
    assert '<option value="United States"' in page.text
    bad=c.post(f'/autopilot/tasks/{tid}/answers',data={'version':version,'answer_0':'Made up','answer_1':'June 1, 2027'})
    assert bad.status_code==422 and 'listed options' in bad.text and 'June 1, 2027' in bad.text
    result=c.post(f'/autopilot/tasks/{tid}/answers',data={'version':version,'answer_0':'United States','answer_1':'June 1, 2027'})
    assert result.status_code==200 and 'Answers saved' in result.text
    with Session() as db:
        task=db.get(AutoApplication,tid);cfg=queue.settings(db)
        assert task.state=='queued' and task.resume_id==rid and task.submission_started_at is None
        assert json.loads(task.answers)['Country*']=='United States'
        assert 'Country*' not in json.loads(cfg.answers) and cfg.mode=='running'
    assert c.post(f'/autopilot/tasks/{tid}/answers',data={'version':version,'answer_0':'Canada','answer_1':'July 1'}).status_code==409


def test_manual_issue_has_review_link_and_no_fake_input_fields(client):
    c,m,Session=client
    with Session() as db: tid,pid,_=waiting_task(db,['Could not confirm the employer identity on the application page.'])
    page=c.get(f'/autopilot/tasks/{tid}')
    assert 'Needs manual review' in page.text and 'Employer-site issue' in page.text
    assert 'name="answer_0"' not in page.text and 'Open employer application' in page.text
    assert f'/autopilot/tasks/{tid}' in c.get(f'/jobs/{pid}').text
    activity=c.get('/autopilot/activity').json()
    item=next(x for x in activity['recent'] if x['id']==tid)
    assert item['status_label']=='Needs manual review' and item['action_label']=='Review issue'
    assert item['task_url']==f'/autopilot/tasks/{tid}'


def test_stale_answers_and_submitted_tasks_cannot_retry(client,monkeypatch):
    c,m,Session=client
    with Session() as db: tid,_,_=waiting_task(db,['Country?'])
    monkeypatch.setenv('ADMIN_PASSWORD','private')
    assert c.get(f'/autopilot/tasks/{tid}').status_code==401
    auth=('owner','private')
    assert c.post(f'/autopilot/tasks/{tid}/answers',auth=auth,data={'version':'stale','answer_0':'United States'}).status_code==409
    with Session() as db:
        task=db.get(AutoApplication,tid);version=question_view(task)['version'];task.submission_started_at=utcnow();db.commit()
    assert c.post(f'/autopilot/tasks/{tid}/answers',auth=auth,data={'version':version,'answer_0':'United States'}).status_code==409


def test_answers_can_be_remembered_explicitly(client):
    c,m,Session=client
    with Session() as db:
        tid,_,_=waiting_task(db,['Country?']);version=question_view(db.get(AutoApplication,tid))['version']
    assert c.post(f'/autopilot/tasks/{tid}/answers',data={'version':version,'answer_0':'United States','remember':'yes'}).status_code==200
    with Session() as db: assert json.loads(queue.settings(db).answers)['Country?']=='United States'


def test_structured_options_and_request_budget():
    result=pack_questions(['Country (choose an exact option)','Employer account login required.'],{'Country':['United States','Canada']})
    assert result[0]=={'label':'Country','options':['United States','Canada']}
    assert validate_questions(result)==result
    with pytest.raises(ValueError): validate_questions([{'label':'Country','options':[{'bad':1}]}])
    big=pack_questions([f'Question {i}' for i in range(80)],{f'Question {i}':['a'*299]*300 for i in range(80)})
    assert len(json.dumps(big).encode())<=50000 and len(big)==80


def test_inline_api_returns_questions_and_queues_answers_without_navigation(client):
    c,_,Session=client
    with Session() as db:
        tid,pid,rid=waiting_task(db,[{'label':'Country?','options':['United States','Canada']},
                                   'Could not identify the next application step.'])
    data=c.get('/autopilot/activity').json()
    assert data['answer_count']==1 and data['answer_ids']==[tid]
    attention=data['attention'][0]
    assert attention['fields']==[{'label':'Country?','options':['United States','Canada']}]
    assert attention['blockers']==['Could not identify the next application step.']
    assert 'answers' not in attention and 'applicant' not in attention
    headers={'Accept':'application/json'}
    bad=c.post(f'/autopilot/tasks/{tid}/answers',headers=headers,
               data={'version':attention['version'],'answer_0':'Invalid'})
    assert bad.status_code==422 and 'listed options' in bad.json()['detail']
    result=c.post(f'/autopilot/tasks/{tid}/answers',headers=headers,
                  data={'version':attention['version'],'answer_0':'United States'})
    assert result.status_code==200 and result.json()['state']=='queued'
    assert not result.history
    assert c.get('/autopilot/activity').json()['attention']==[]
    with Session() as db:
        task=queue.claim(db,queue.settings(db))
        assert task.id==tid and task.resume_id==rid and task.state=='running'
        assert json.loads(task.answers)['Country?']=='United States'
        assert task.submitted_at is None


def test_phone_inline_answers_survive_polling_and_continue_in_place(client):
    from test_apply_navigation import local_app
    pw=pytest.importorskip('playwright.sync_api')
    c,_,Session=client
    with Session() as db:
        tid,_,_=waiting_task(db,['Earliest start date?'])
    with local_app(client[1].app) as origin, pw.sync_playwright() as runtime:
        browser=runtime.chromium.launch()
        page=browser.new_page(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
        page.goto(origin+'/autopilot')
        field=page.locator('#worker-questions').get_by_label('Earliest start date?',exact=True)
        field.fill('June 1, 2027')
        with page.expect_response('**/autopilot/activity'):
            pass
        assert field.input_value()=='June 1, 2027'
        assert field.evaluate('(node) => node === document.activeElement')
        assert page.locator('#worker-counts').inner_text().find('Confirmed submissions: 0')>=0
        # Exercise inline server error without replacing the user's draft.
        def reject_once(route):
            route.fulfill(status=422,content_type='application/json',body='{"detail":"Please review this answer."}')
            page.unroute('**/answers',reject_once)
        page.route('**/answers',reject_once)
        page.get_by_role('button',name='Save and continue',exact=True).click()
        pw.expect(page.locator('#worker-questions')).to_contain_text('Please review this answer.')
        assert field.input_value()=='June 1, 2027' and page.url==origin+'/autopilot'
        page.get_by_role('button',name='Save and continue',exact=True).click()
        pw.expect(page.locator('#answers-result')).to_contain_text('Answers saved.')
        assert page.url==origin+'/autopilot'
        pw.expect(page.locator('#worker-questions form')).to_have_count(0)
        browser.close()
    with Session() as db:
        task=db.get(AutoApplication,tid)
        assert task.state=='queued' and json.loads(task.answers)['Earliest start date?']=='June 1, 2027'
