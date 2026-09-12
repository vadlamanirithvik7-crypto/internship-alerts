import json
import re
from shared.profile_answers import known_answer,match_option,PREFIX,combined_answers
from shared.answer_inbox import task_view
from shared.db import AutoApplication,ApplicantSettings,Posting,utcnow
from shared import auto_apply as queue
from test_app import client
from test_auto_input import waiting_task


def test_profile_aliases_dates_links_and_degree_choices():
    facts={PREFIX+'school':'University of Texas at Austin',PREFIX+'major':'Electrical and Computer Engineering',
           PREFIX+'degree':'Bachelor of Science',PREFIX+'graduation_date':'May 10, 2028',
           PREFIX+'college_start_date':'August 28, 2024',PREFIX+'gpa':'3.00'}
    profile={'github':'https://github.com/example','linkedin':'https://www.linkedin.com/in/example'}
    for label in ['School*','Which university are you currently attending? Select "Other" if not listed*']:
        assert known_answer(label,profile,facts)=='University of Texas at Austin'
    assert known_answer('Field of study',profile,facts)=='Electrical and Computer Engineering'
    assert known_answer('Expected graduation year?',profile,facts)=='2028'
    assert known_answer('End date month*',profile,facts)=='May'
    assert known_answer('Start date month*',profile,facts)=='August'
    assert known_answer('GitHub Profile*',profile,{'GitHub Profile*':'no'})==profile['github']
    assert known_answer('LinkedIn profile URL',profile,facts)==profile['linkedin']
    assert known_answer('When can you start this internship?',profile,facts) is None
    assert known_answer('Have you completed a degree?',profile,facts) is None
    assert match_option('Degree','Bachelor of Science',["Bachelor's Degree","Master's Degree"])=="Bachelor's Degree"
    assert match_option('Discipline',facts[PREFIX+'major'],['Computer Engineering','Electrical Engineering']) is None
    assert match_option('Please select your GPA range based on a 4.0 scale.','3.00',['3.0-3.29','3.3-3.59'])=='3.0-3.29'
    assert combined_answers({PREFIX+'school':'Old school','Why this company?':'Specific'},facts)[PREFIX+'school']==facts[PREFIX+'school']


def add_waiting(db,rid,label,options=None):
    p=Posting(company_name='Second employer',title='Software Intern',url='https://example.com/second',source='test',raw_hash='inbox-second',first_seen_at=utcnow())
    db.add(p);db.flush()
    t=AutoApplication(posting_id=p.id,resume_id=rid,application_key='inbox-second',target_url=p.url,applicant='{}',state='needs_input',questions=json.dumps([{'label':label,'options':options or []}]))
    db.add(t);db.commit();return t.id


def test_inbox_asks_once_skips_known_and_resumes_matching_tasks(client):
    c,_,Session=client
    with Session() as db:
        tid,_,rid=waiting_task(db,['GitHub Profile*','School*','Preferred interview language?'])
        cfg=queue.settings(db);cfg.answers=json.dumps({PREFIX+'school':'UT Austin'})
        profile=db.get(ApplicantSettings,1);data=json.loads(profile.data);data['github']='https://github.com/example';profile.data=json.dumps(data)
        second=add_waiting(db,rid,'Preferred interview language?')
    page=c.get('/autopilot/answers')
    assert page.status_code==200 and 'Remaining questions · 1' in page.text
    assert 'Used by 2 applications' in page.text and '2 question occurrences already have saved answers' in page.text
    name=re.search(r'<textarea name="(q_[a-f0-9]+)"',page.text)[1]
    response=c.post('/autopilot/answers',data={name:'Python'})
    assert response.status_code==200 and '2 applications were queued' in response.text
    with Session() as db:
        for task_id in (tid,second):
            task=db.get(AutoApplication,task_id)
            assert task.state=='queued' and json.loads(task.answers)['Preferred interview language?']=='Python'
        assert json.loads(queue.settings(db).answers)['Preferred interview language?']=='Python'


def test_inbox_keeps_different_dropdowns_separate_and_rejects_invalid_choice(client):
    c,_,Session=client
    with Session() as db:
        tid,_,rid=waiting_task(db,[{'label':'Interview language?','options':['Python','Java']}])
        add_waiting(db,rid,'Interview language?',['C++','Java'])
    page=c.get('/autopilot/answers')
    assert 'Remaining questions · 2' in page.text
    name=re.search(r'<select name="(q_[a-f0-9]+)"',page.text)[1]
    assert c.post('/autopilot/answers',data={name:'Fabricated option'}).status_code==422
    with Session() as db: assert db.get(AutoApplication,tid).state=='needs_input'


def test_remembered_profile_reaches_already_queued_worker_tasks(client):
    c,_,Session=client
    with Session() as db:
        tid,_,_=waiting_task(db,['School*']);task=db.get(AutoApplication,tid);task.state='queued';task.applicant='{}'
        task.answers=json.dumps({PREFIX+'school':'Stale school'})
        cfg=queue.settings(db);cfg.answers=json.dumps({PREFIX+'school':'Current school'})
        p=db.get(ApplicantSettings,1);data=json.loads(p.data);data['github']='https://github.com/current';p.data=json.dumps(data);db.commit()
    task=c.post('/integrations/auto-apply',headers={'Authorization':'Bearer test-token'},json={'action':'claim'}).json()['task']
    assert task['profile']['github']=='https://github.com/current'
    assert task['answers'][PREFIX+'school']=='Current school'


def test_shared_inbox_is_private_and_uncertain_never_requeued(client,monkeypatch):
    c,_,Session=client
    with Session() as db:
        tid,_,_=waiting_task(db,['School*']);task=db.get(AutoApplication,tid);task.state='uncertain';task.submission_started_at=utcnow();db.commit()
    c.post('/autopilot/answers',data={'profile_school':'UT Austin'})
    with Session() as db: assert db.get(AutoApplication,tid).state=='uncertain'
    monkeypatch.setenv('ADMIN_PASSWORD','private')
    assert c.get('/autopilot/answers').status_code==401
    assert c.post('/autopilot/answers',data={'profile_school':'Other'}).status_code==401


def test_employer_essays_are_not_shared_just_because_wording_matches(client):
    c,_,Session=client
    with Session() as db:
        tid,_,rid=waiting_task(db,['Why this company?']);add_waiting(db,rid,'Why this company?')
    page=c.get('/autopilot/answers')
    assert 'Remaining questions · 2' in page.text
    name=re.search(r'<textarea name="(q_[a-f0-9]+)"',page.text)[1]
    c.post('/autopilot/answers',data={name:'This specific employer interests me.'})
    with Session() as db:
        assert 'Why this company?' not in json.loads(queue.settings(db).answers)


def test_phone_can_answer_shared_dropdown_once(client):
    import pytest
    from test_apply_navigation import local_app
    pw=pytest.importorskip('playwright.sync_api')
    c,_,Session=client
    with Session() as db:
        tid,_,rid=waiting_task(db,['School*',{'label':'Interview language?','options':['Python','Java']}])
        cfg=queue.settings(db);cfg.answers=json.dumps({PREFIX+'school':'UT Austin'})
        second=add_waiting(db,rid,'Interview language?',['Python','Java'])
    with local_app(client[1].app) as origin,pw.sync_playwright() as runtime:
        browser=runtime.chromium.launch()
        page=browser.new_page(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
        page.goto(origin+'/autopilot/answers')
        assert page.locator('[name="profile_school"]').input_value()=='UT Austin'
        assert page.locator('select[name^="q_"]').count()==1
        page.locator('select[name^="q_"]').select_option(label='Python')
        page.get_by_role('button',name='Save shared answers and continue',exact=True).first.click()
        pw.expect(page.locator('section[role="status"]')).to_contain_text('2 applications were queued')
        assert page.locator('select[name^="q_"]').count()==0
        browser.close()


def test_extract_resume_profile_links_from_annotations():
    from io import BytesIO
    from pypdf import PdfWriter
    from pypdf.annotations import Link
    from shared.applying import resume_profile_links
    writer=PdfWriter();writer.add_blank_page(width=612,height=792)
    writer.add_annotation(0,Link(rect=(0,0,100,30),url='https://github.com/ResumeUser'))
    writer.add_annotation(0,Link(rect=(0,40,100,60),url='https://www.linkedin.com/in/resume-user'))
    writer.add_annotation(0,Link(rect=(0,80,100,100),url='https://github.com/ResumeUser/some-project'))
    data=BytesIO();writer.write(data)
    assert resume_profile_links(data.getvalue())=={'github':'https://github.com/ResumeUser','linkedin':'https://www.linkedin.com/in/resume-user'}


def test_observed_employer_formats_reuse_existing_facts():
    facts={PREFIX+'graduation_date':'May 10, 2028',PREFIX+'school':'University of Texas at Austin'}
    assert known_answer('Internships at Hudl are only open to currently-enrolled college students. What is your expected graduation date?*',{},facts)=='May 10, 2028'
    assert match_option('Graduation date','May 10, 2028',['May 2027','May 2028'])=='May 2028'
    assert match_option('Please select your anticipated graduation date','May 10, 2028',['Spring 2027','Spring 2028'])=='Spring 2028'
    assert match_option('Graduation date','May 10, 2028',['January 2028 - July 2028','August 2028 - December 2028'])=='January 2028 - July 2028'
    assert match_option('GPA','3.00',['3.5 - 3.99','3.49 - 3.0','2.99 or below'])=='3.49 - 3.0'
    assert match_option('GPA','3.00',['Below 3.2','3.21 - 3.3'])=='Below 3.2'
    assert match_option('Country of residence','United States',['United States of America'])=='United States of America'
    assert match_option('School','University of Texas at Austin',['University of Texas Austin'])=='University of Texas Austin'
    assert match_option('Discipline','Electrical and Computer Engineering',['Engineering','Computer Science'])=='Engineering'
    task=AutoApplication(state='needs_input',answers='{}',questions=json.dumps([{'label':'School*','options':['School '+str(i) for i in range(100)]}]))
    assert task_view(task,{},facts)['fields']==[]


def test_worker_searches_school_beyond_initial_menu():
    import asyncio
    import httpx
    from playwright.async_api import async_playwright
    from applicant.forms import fill_form
    async def check():
        async with async_playwright() as runtime:
            browser=await runtime.chromium.launch();page=await browser.new_page()
            await page.set_content('''<label for="school">School*</label><input id="school" role="combobox" required><div id="menu"><div role="option">Aalto University</div></div>
            <script>school.oninput=()=>{menu.innerHTML='<div role="option">University of Texas at Austin</div>';menu.firstChild.onclick=()=>{school.value=menu.firstChild.textContent;menu.innerHTML='';};};</script>''')
            async with httpx.AsyncClient() as client:
                missing,_=await fill_form(page,{'profile':{},'answers':{PREFIX+'school':'University of Texas at Austin'}},client)
            assert missing==[]
            assert await page.locator('#school').input_value()=='University of Texas at Austin'
            await browser.close()
    asyncio.run(check())
