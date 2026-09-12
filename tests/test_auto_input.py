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
        page.locator('#worker-questions summary').click()
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


def test_show_more_questions_preserves_expanded_draft(client):
    from datetime import timedelta
    from shared.db import Posting
    from test_apply_navigation import local_app
    pw=pytest.importorskip('playwright.sync_api')
    c,_,Session=client
    with Session() as db:
        tid,_,rid=waiting_task(db,['Earliest start date?'])
        for i in range(20):
            posting=Posting(company_name=f'Fixture {i}',title='Software Intern',url=f'https://example.com/{i}',
                            source='test',raw_hash=f'inline-{i}',first_seen_at=utcnow())
            db.add(posting); db.flush()
            db.add(AutoApplication(posting_id=posting.id,resume_id=rid,application_key=f'inline-{i}',
                                   target_url=posting.url,applicant='{}',state='needs_input',
                                   questions='["Fixture question?"]',updated_at=utcnow()-timedelta(days=1)))
        db.commit()
    assert len(c.get('/autopilot/activity').json()['attention'])==20
    assert len(c.get('/autopilot/activity?question_limit=40').json()['attention'])==21
    with local_app(client[1].app) as origin,pw.sync_playwright() as runtime:
        browser=runtime.chromium.launch()
        page=browser.new_page(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
        page.goto(origin+'/autopilot')
        page.locator('#worker-questions summary').first.click()
        field=page.locator('#worker-questions').get_by_label('Earliest start date?',exact=True)
        field.fill('June 1, 2027')
        page.get_by_role('button',name='Show more applications with questions',exact=True).click()
        pw.expect(page.locator('#worker-questions details')).to_have_count(21)
        assert field.input_value()=='June 1, 2027' and field.is_visible()
        pw.expect(page.locator('#questions-more')).to_be_hidden()
        assert page.url==origin+'/autopilot'
        browser.close()


def test_metadata_refresh_merges_options_and_preserves_partial_draft(client,monkeypatch):
    from shared.employer_questions import greenhouse_questions_url,refresh_questions,discovery_answer,SOURCE_KEY
    assert greenhouse_questions_url('https://job-boards.greenhouse.io/test/jobs/123').endswith('/test/jobs/123?questions=true')
    assert greenhouse_questions_url('https://job-boards.greenhouse.io.evil.test/test/jobs/123') is None
    schema=[{'label':'Authorization?','options':['Choice A','Choice B'],'required':True},
            {'label':'How did you hear about us?','options':['AfroTech','Friend','Other'],'required':True}]
    updated=refresh_questions(['Authorization?*','AfroTech','Friend','Other','School*'],schema)
    assert len(updated)==3 and updated[0]['options']==['Choice A','Choice B']
    assert updated[-1]['label']=='How did you hear about us?'
    assert discovery_answer('How did you hear about us?',['Other','Friend'],{SOURCE_KEY:'Internship Radar'})=='Other'
    assert discovery_answer('How did you hear about us?',['Friend'],{SOURCE_KEY:'Internship Radar'}) is None
    c,_,Session=client
    with Session() as db:
        tid,_,_=waiting_task(db,['Authorization?*','AfroTech','Friend','Other','School*'])
        task=db.get(AutoApplication,tid); task.target_url='https://job-boards.greenhouse.io/test/jobs/123'
        version=question_view(task)['version'];db.commit()
    class Response:
        def raise_for_status(self): pass
        def json(self): return {'questions':[{'label':q['label'],'required':True,'fields':[{'type':'multi_value_single_select','values':[{'label':v} for v in q['options']]}]} for q in schema]}
    monkeypatch.setattr('requests.get',lambda *args,**kwargs:Response())
    result=c.post(f'/autopilot/tasks/{tid}/refresh-questions',data={'version':version,'answer_0':'Unclear draft','answer_4':'My university'})
    assert result.status_code==200
    attention=c.get('/autopilot/activity').json()['attention'][0]
    assert attention['fields'][0]['options']==['Choice A','Choice B']
    assert attention['values'][0]=='Unclear draft'
    assert not any(f['label']=='School*' for f in attention['fields'])
    with Session() as db:
        assert db.get(AutoApplication,tid).state=='needs_input'
        assert json.loads(db.get(AutoApplication,tid).answers)['School*']=='My university'


def test_batch_retry_preserves_specific_answers_and_submission_guards(client):
    c,_,Session=client
    with Session() as db:
        tid,_,rid=waiting_task(db,['Country?'])
        cfg=queue.settings(db);cfg.answers='{"Country?":"United States","GPA":"3.00"}'
        task=db.get(AutoApplication,tid);task.answers='{"Country?":"Canada"}';db.commit()
    assert c.post('/autopilot/retry-attention').status_code==200
    with Session() as db:
        task=db.get(AutoApplication,tid)
        assert task.state=='queued' and task.resume_id==rid
        assert json.loads(task.answers)=={'Country?':'Canada','GPA':'3.00'}
        task.state='needs_input';db.commit()
    c.post('/autopilot/retry-attention',data={'replace_saved':'yes'})
    with Session() as db:
        task=db.get(AutoApplication,tid)
        assert json.loads(task.answers)['Country?']=='United States'
        task.state='needs_input';task.submission_started_at=utcnow();db.commit()
    c.post('/autopilot/retry-attention')
    with Session() as db: assert db.get(AutoApplication,tid).state=='needs_input'


def test_greenhouse_choices_checkbox_groups_and_hidden_proxies():
    import asyncio
    import httpx
    from playwright.async_api import async_playwright
    from applicant.forms import fill_form,answer_for
    from shared.employer_questions import SOURCE_KEY
    assert answer_for('Will you now or in the future require visa sponsorship?',{}, {'Sponsorship required':'No'})=='No'
    assert answer_for('What visa do you hold?',{}, {'Sponsorship required':'No'}) is None
    async def check():
        async with async_playwright() as p, httpx.AsyncClient() as client:
            browser=await p.chromium.launch()
            page=await browser.new_page()
            await page.set_content('''<label for="auth">Work authorization*</label>
<input id="auth" role="combobox" aria-required="true" readonly onclick="document.getElementById('choices').hidden=false">
<input id="proxy" required aria-hidden="true" tabindex="-1">
<div id="choices" role="listbox" hidden><div role="option" onclick="pick(this)">Needs sponsorship</div><div role="option" onclick="pick(this)">No restrictions</div></div>
<fieldset aria-required="true"><legend>How did you hear about us?*</legend>
<label><input id="afro" type="checkbox" required onchange="for(const e of this.closest('fieldset').querySelectorAll('input'))e.required=false">AfroTech</label>
<label><input id="other" type="checkbox" required onchange="for(const e of this.closest('fieldset').querySelectorAll('input'))e.required=false">Other</label></fieldset>
<label>If Other, Please Specify<input id="source-detail"></label><label>City<input id="city"></label>
<script>function pick(e){const ghost=document.createElement('input');ghost.type='hidden';document.body.prepend(ghost);document.getElementById('auth').value=e.textContent;document.getElementById('proxy').value='selected';document.getElementById('choices').hidden=true}</script>''')
            task={'profile':{},'answers':{SOURCE_KEY:'Internship Radar'},'_employer_schema':[{'label':'Work authorization','options':['Needs sponsorship','No restrictions']} ]}
            missing,_=await fill_form(page,task,client)
            assert missing==['Work authorization*']
            assert task['_question_fields']['Work authorization*']==['Needs sponsorship','No restrictions']
            assert await page.get_by_label('Other',exact=True).is_checked()
            assert await page.get_by_label('If Other, Please Specify',exact=True).input_value()=='Internship Radar'
            task['answers']['Work authorization*']='No restrictions'
            task['answers']['City']='Austin'
            missing,_=await fill_form(page,task,client)
            assert missing==[]
            assert await page.locator('#auth').input_value()=='No restrictions'
            assert await page.locator('#city').input_value()=='Austin'
            await browser.close()
    asyncio.run(check())
