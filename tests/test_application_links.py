from types import SimpleNamespace
from sqlalchemy import select

from poller import application_links as links
from shared.db import Posting, ApplicationSync
from test_app import client


def job(**values):
    data = dict(id=1, company_name='Acme', title='Software Intern Summer 2027',
                location='Austin, TX', url='https://jobright.ai/jobs/info/' + 'a' * 24,
                application_url=None, employer_site_url=None, closed_at=None, target_eligible=True)
    return SimpleNamespace(**(data | values))


def test_direct_destinations_do_not_allow_aggregators_or_unsafe_links():
    for url in ['https://jobright.ai/jobs/1', 'https://www.jobright.ai/jobs/1', 'https://linkedin.com/jobs/1',
                'javascript:alert(1)', 'https://localhost/x', 'http://127.0.0.1/x', 'https://user:pw@company.com/job',
                'https://company.internal/job', 'https://[invalid']:
        assert links.direct_url(url) == ''
    url = 'https://acme.wd5.myworkdayjobs.com/Careers/job/Austin/Intern_R123?source=Radar'
    assert links.direct_url(url) == url
    assert links.notification_url(job()).endswith('/jobs/1')
    assert 'jobright.ai/jobs' not in links.notification_url(job())


def test_conservative_matching_never_picks_a_different_requisition():
    p = job()
    direct = job(id=2, url='https://careers.acme.com/jobs/123', location='Austin, TX, United States')
    assert links.match_employer(p, [direct]) == direct.url
    assert not links.match_employer(p, [job(id=3, url='https://careers.acme.com/jobs/456'), direct])
    assert not links.match_employer(p, [job(url=direct.url, location='Seattle, WA')])
    assert not links.match_employer(p, [job(url=direct.url, title='Software Intern Summer 2026')])
    assert not links.match_employer(p, [job(url=direct.url, company_name='Different Employer')])
    assert not links.match_employer(p, [job(url=direct.url, closed_at=True)])


def test_public_jobright_metadata_is_checked_and_honestly_labeled(monkeypatch):
    p = job()
    detail = dict(jobResult=dict(jobId='a'*24, jobTitle=p.title, isCompanySiteLink=True),
                  companyResult=dict(companyName='Acme', companyURL='https://acme.com'))
    monkeypatch.setattr('poller.net.get_json', lambda *a, **k: {'success': True, 'result': {'jobDetail': detail}})
    assert links.jobright_destinations(p.url, p.company_name, p.title) == {'application_url':'', 'employer_site_url':'https://acme.com'}
    detail['jobResult']['originalUrl'] = 'https://acme.wd5.myworkdayjobs.com/Careers/job/Intern_R123'
    assert links.jobright_destinations(p.url, p.company_name, p.title)['application_url'].endswith('Intern_R123')
    detail['jobResult']['originalUrl'] = 'https://linkedin.com/jobs/123'
    assert links.jobright_destinations(p.url, p.company_name, p.title)['application_url'] == ''
    detail['companyResult']['companyName'] = 'Different Employer'
    assert links.jobright_destinations(p.url, p.company_name, p.title) == {}


def test_repair_preserves_identity_and_application_history(client, monkeypatch):
    c, _, Session = client
    monkeypatch.setattr(links, 'jobright_destinations', lambda *a: {})
    with Session() as db:
        original = db.get(Posting, 1)
        original.url = job().url
        original.target_eligible = True
        identity = original.raw_hash
        direct = db.get(Posting, 2)
        direct.company_name, direct.title, direct.location = original.company_name, original.title, original.location
        direct.url = 'https://careers.acme.com/jobs/123'
        direct.target_eligible = True
        db.commit()
    c.post('/jobs/1/status', data={'status':'applied', 'notes':'Already submitted'})
    with Session() as db:
        result = links.repair_links(db)
        db.commit()
        assert result['exact_links'] == 1
        p = db.get(Posting, 1)
        assert p.raw_hash == identity and p.url == job().url
        assert p.status == 'applied' and p.applied_at and p.notes == 'Already submitted'
        assert p.application_url == 'https://careers.acme.com/jobs/123'
        assert links.repair_links(db)['new_exact_links'] == 0
    assert c.get('/jobs/1/apply', follow_redirects=False).headers['location'] == 'https://careers.acme.com/jobs/123'


def test_every_apply_surface_avoids_aggregators_and_network_failure_is_safe(client, monkeypatch):
    c, _, Session = client
    monkeypatch.setattr(links, 'jobright_destinations', lambda *a: {})
    with Session() as db:
        p = db.get(Posting, 1)
        p.url = job().url
        db.commit()
    page = c.get('/jobs/1')
    assert page.status_code == 200 and 'Find employer application' in page.text
    assert f'href="{job().url}"' not in page.text
    assert c.get('/jobs/1/apply', follow_redirects=False).headers['location'] == '/jobs/1'
    with Session() as db:
        p = db.get(Posting, 1)
        p.employer_site_url = 'https://careers.acme.com'
        db.commit()
    page = c.get('/jobs/1').text
    assert 'Open company careers / website' in page
    assert 'exact application link is not confirmed' in page
    with Session() as db:
        p = db.get(Posting, 1)
        p.application_url = 'https://acme.wd5.myworkdayjobs.com/Careers/job/Intern_R123'
        db.commit()
    page = c.get('/jobs/1').text
    assert 'Open employer application' in page and 'target="_blank"' in page
    assert 'href="https://acme.wd5.myworkdayjobs.com/Careers/job/Intern_R123"' in page


def test_workday_fallback_retains_correct_tenant_host_site():
    company = SimpleNamespace(resolved=True, slug='acme', ats_type='workday',
                              workday_tenant='acme', workday_host='wd5', workday_site='Careers')
    assert links.company_careers_url(company, 'Software Intern').startswith('https://acme.wd5.myworkdayjobs.com/Careers?q=')
    company.resolved = False
    assert links.company_careers_url(company) == ''
