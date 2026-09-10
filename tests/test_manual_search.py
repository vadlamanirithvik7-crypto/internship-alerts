from unittest.mock import Mock
import json

import pytest
from sqlalchemy import select, text

from shared.db import Posting, ApplicationTask, ApplicationSync, ResumeProfile, pack_list, init_db
from shared.eligibility import eligible, restriction_reasons
from shared.role_search import role_tags
from poller import matcher
from test_app import client
from test_apply_navigation import local_app
from test_reliability import job, ingest, filt


@pytest.mark.parametrize('description', [
    'Must be a U.S. citizen.',
    'US citizenship is required for this position.',
    'Applicants must be US citizens or green card holders.',
    'Must be a lawful permanent resident.',
    'Open to U.S. citizens only.',
    'To comply with ITAR, the candidate must be a US person.',
    'USC/GC only.',
    'Currently pursuing a Master’s degree in Computer Science.',
    'Must be enrolled in a Ph.D. program.',
    'Minimum qualifications: MS or PhD in Electrical Engineering.',
    'Seeking graduate students for this internship.',
])
def test_stated_restrictions_are_excluded(description):
    assert restriction_reasons('Software Engineering Intern', description)
    assert not eligible('Software Engineering Intern', 'Austin, TX', 'Summer 2027', description)


@pytest.mark.parametrize('description', [
    'Must be pursuing a bachelor’s or master’s degree in engineering.',
    'Currently enrolled in a BS, MS, or PhD program.',
    'A bachelor degree is required. A master degree is preferred.',
    'Currently pursuing an undergraduate degree, graduating in 2028.',
    'U.S. citizenship is not required.',
    'We hire without regard to citizenship or permanent residency.',
    'Must be authorized to work in the United States. Sponsorship is not provided.',
    'Applicants pursuing a bachelor degree are eligible. Work alongside PhD researchers.',
])
def test_bachelors_alternatives_and_nonrequirements_remain(description):
    assert not restriction_reasons('Software Engineering Intern', description)
    assert eligible('Software Engineering Intern', 'Austin, TX', 'Summer 2027', description)


@pytest.mark.parametrize('title', ['Software Engineering Intern (Masters)', 'PhD Hardware Intern', 'Graduate Software Intern', 'MBA Software Intern'])
def test_graduate_titles_excluded(title):
    assert not eligible(title, 'Austin, TX', 'Summer 2027', 'Build systems.')


@pytest.mark.parametrize('title,roles', [
    ('Software Engineering Internship', ['software']),
    ('Embedded Software Engineering Intern', ['embedded']),
    ('Firmware Co-op', ['embedded']),
    ('RTL Design Verification Intern', ['hardware']),
    ('Electrical Engineering Intern', ['hardware']),
    ('Finance Intern', []),
    ('Business Operations Internship', []),
    ('Software Sales Engineering Intern', []),
    ('Data Analyst Internship', []),
])
def test_roles_identify_the_job_not_description(title, roles):
    assert role_tags(title) == roles


def test_role_filters_and_empty_selection(client):
    c, module, Session = client
    with Session() as db:
        db.query(ResumeProfile).delete()
        for index, title in enumerate(['Software Engineering Intern', 'Embedded Software Intern', 'Electrical Engineering Intern', 'Finance Intern'], start=1):
            p = db.get(Posting, index)
            p.title = title
            p.description = 'Our company builds software with Python, hardware and firmware.'
            p.search_roles = pack_list(role_tags(title))
            p.target_eligible = eligible(title, 'Austin, TX', 'Summer 2027', p.description)
            p.status = 'new'
        db.commit()
    module.TARGET_ONLY = True
    try:
        for role, title in [('software','Software Engineering Intern'), ('embedded','Embedded Software Intern'), ('hardware','Electrical Engineering Intern')]:
            html = c.get('/?roles_filter=1&role=' + role).text
            assert title in html
            assert 'Finance Intern</a>' not in html
            for other in ['Software Engineering Intern','Embedded Software Intern','Electrical Engineering Intern']:
                if other != title:
                    assert other + '</a>' not in html
        assert 'No roles in this view' in c.get('/?roles_filter=1').text
        assert c.get('/?role=bogus').status_code == 422
    finally:
        module.TARGET_ONLY = False


def test_manual_open_retired_endpoints_and_tracking(client):
    c, _, Session = client
    with Session() as db:
        expected = db.get(Posting, 1).url
    response = c.get('/jobs/1/apply', follow_redirects=False)
    assert response.headers['location'] == expected
    assert c.post('/jobs/1/apply').status_code == 410
    assert c.post('/apply-tasks/1/review').status_code == 410
    assert c.get('/apply-tasks').url.path == '/applications'
    with Session() as db:
        assert db.scalar(select(ApplicationTask)) is None
        assert db.get(Posting, 1).applied_at is None
    assert c.post('/jobs/1/status', data={'status':'applied'}).status_code == 200
    with Session() as db:
        assert db.get(Posting, 1).applied_at
        assert db.get(ApplicationSync, 1).state == 'pending'


def test_not_interested_persists_without_application_or_sheet(client):
    c, _, Session = client
    assert c.post('/jobs/1/status', data={'status':'not_interested'}).status_code == 200
    with Session() as db:
        p = db.get(Posting, 1)
        assert p.status == 'not_interested' and p.applied_at is None
        assert db.get(ApplicationSync, 1) is None
    assert 'data-posting="1"' not in c.get('/').text
    assert 'data-status="not_interested"' in c.get('/applications').text
    c.post('/jobs/1/status', data={'status':'new'})
    assert 'data-posting="1"' in c.get('/').text


def test_dismissed_and_restricted_roles_never_alert(db, monkeypatch):
    rows = ingest(db, [job(title='Software Engineering Intern', term='Summer 2027'),
                       job(id='other', title='Software Engineering Intern', term='Summer 2027', description='US citizens only.')])
    rows[0].status = 'not_interested'
    filt(db)
    db.commit()
    sender = Mock()
    monkeypatch.setattr(matcher, 'send_email', sender)
    assert matcher.process_new_postings(db, target_only=True) == {}
    sender.assert_not_called()


def test_backfill_rechecks_existing_restrictions_and_roles(db):
    p = ingest(db, [job(title='Software Engineering Intern', term='Summer 2027', description='Must be a US citizen.')])[0]
    p.target_eligible = True
    p.search_version = None
    p.search_roles = None
    db.commit()
    init_db(db.get_bind())
    db.refresh(p)
    assert p.target_eligible is False and p.search_version == 1
    assert p.search_roles == '|software|'


@pytest.mark.parametrize('started,expected', [(False,'cancelled'), (True,'uncertain')])
def test_old_application_tasks_retired_without_losing_history(db, started, expected):
    from shared.db import utcnow
    from test_applying import setup_data
    p, resume = setup_data(db)
    task = ApplicationTask(posting_id=p.id, resume_id=resume.id, applicant='{}',
                           target_url=p.url, application_key='legacy-task',
                           state='submitting' if started else 'queued',
                           submission_started_at=utcnow() if started else None)
    db.add(task)
    db.commit()
    init_db(db.get_bind())
    db.refresh(task)
    db.refresh(p)
    assert task.state == expected and p.applied_at is None
    assert resume.content


def test_retired_worker_never_connects_or_submits(capsys):
    from applicant.worker import main
    assert main() == 0
    assert 'disabled' in capsys.readouterr().out


def test_phone_not_interested_button(client):
    pw = pytest.importorskip('playwright.sync_api')
    with local_app(client[1].app) as origin, pw.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        page = browser.new_page(viewport={'width':390,'height':844}, is_mobile=True, has_touch=True)
        page.route('**/*', lambda route: route.continue_() if route.request.url.startswith(origin + '/') else route.abort())
        page.goto(origin)
        card = page.locator('[data-posting="1"]')
        card.get_by_role('button', name='Not interested').click()
        card.wait_for(state='hidden')
        page.reload()
        assert page.locator('[data-posting="1"]').count() == 0
        browser.close()
