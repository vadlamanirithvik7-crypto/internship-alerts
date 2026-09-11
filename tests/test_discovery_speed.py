from datetime import timedelta
from threading import Event

import pytest
from sqlalchemy import select

from poller import main
from poller.normalize import canonical_url
from poller.sources import trackers, simplify
from poller.store import clean_description
from shared.db import Company, Posting, AICache, init_db, utcnow, raw_hash
from shared.eligibility import eligible, SEARCH_VERSION
from test_reliability import job, ingest


@pytest.mark.parametrize('title,term', [
    ('Software Co-op', 'Summer 2027'), ('Software COOP', 'Summer 2027'),
    ('Electrical Co op', 'Summer 2027'), ('Firmware Co‑op', 'Summer 2027'),
    ('Software Internship / Co-op', 'Summer 2027'),
    ('Software Engineering Intern', 'Summer 2027; Co-op'),
])
def test_coops_are_excluded(title, term):
    assert not eligible(title, 'Austin, TX', term)


def test_internship_description_can_mention_other_coop_programs():
    assert eligible('Software Engineering Intern', 'Austin, TX', 'Summer 2027',
                    'We also offer co-op programs in other departments.')


def test_query_identifies_separate_employer_requisitions():
    first = 'https://careers.example.com/jobs?gh_jid=123&utm_source=tracker'
    assert canonical_url(first) == canonical_url('https://careers.example.com/jobs?gh_jid=123')
    assert canonical_url(first) != canonical_url('https://careers.example.com/jobs?gh_jid=456')
    assert canonical_url('https://careers.example.com/view?jobId=100') != canonical_url('https://careers.example.com/view?jobId=101')


def test_backfill_preserves_history_and_separate_future_jobs(db):
    p = ingest(db, [job(title='Software Co-op', term='Summer 2027', url='https://careers.example.com/jobs?gh_jid=123')])[0]
    p.raw_hash = raw_hash('careers.example.com/jobs')
    p.search_version = 2
    p.target_eligible = True
    p.status = 'applied'
    p.applied_at = utcnow()
    p.notes = 'Already submitted'
    original_id = p.id
    db.commit()
    init_db(db.get_bind())
    db.refresh(p)
    assert p.id == original_id and p.status == 'applied' and p.applied_at
    assert p.notes == 'Already submitted' and not p.target_eligible
    assert p.search_version == SEARCH_VERSION
    assert p.raw_hash == raw_hash(canonical_url(p.url))
    added = ingest(db, [job(title='Software Engineering Intern', term='Summer 2027', url='https://careers.example.com/jobs?gh_jid=456')])
    assert len(added) == 1 and added[0].id != original_id


def test_rotation_has_room_even_with_many_matching_companies(db):
    for i in range(6):
        p = ingest(db, [job(id=str(i), company_name=f'Hot {i}', title='Software Engineering Intern', term='Summer 2027')])[0]
        c = db.get(Company, p.company_id)
        c.last_checked_at = utcnow() - timedelta(hours=1)
    for i in range(4):
        db.add(Company(name=f'Unseen {i}', ats_type='lever', slug=f'unseen{i}'))
    db.commit()
    selection = main.select_board_companies(db, 4)
    assert len(selection) == 4
    assert sum(c.name.startswith('Unseen') for c in selection) == 2


def test_sources_commit_as_they_finish_after_direct_boards(db, monkeypatch):
    monkeypatch.setattr(main, 'get_engine', lambda: db.get_bind())
    monkeypatch.setattr(main.health, 'due', lambda *a: False)
    events = []
    saved_fast = Event()
    board = job(id='board', title='Software Engineering Intern', term='Summer 2027')
    fast = job(id='fast', company_name='New Employer', title='Software Engineering Intern', term='Summer 2027')
    def boards(session, limit, on_batch, **kwargs):
        result = on_batch([board])
        events.append('board saved')
        return result
    monkeypatch.setattr(main, 'poll_boards', boards)
    monkeypatch.setattr(main.simplify, 'fetch', lambda: [fast])
    def slow(**kwargs):
        assert events == ['board saved']
        assert kwargs['discover'] is False
        assert saved_fast.wait(5), 'Fast feed should be committed while slow feed is still running'
        return []
    monkeypatch.setattr(main.trackers, 'fetch', slow)
    original_record = main.health.record
    def record(session, run_id, name, *args, **kwargs):
        if name == 'simplify':
            assert session.scalar(select(Posting).where(Posting.company_name == 'New Employer'))
            saved_fast.set()
        return original_record(session, run_id, name, *args, **kwargs)
    monkeypatch.setattr(main.health, 'record', record)
    result = main._run(board_slice=4, resolve_slice=0, skip_search=True, skip_alerts=True)
    assert result['new'] == 2 and saved_fast.is_set()


def test_tracker_prefers_original_over_aggregator_button():
    text = '''| Company | Role | Location | Application |
|---|---|---|---|
| Acme | Software Intern Summer 2027 | Austin, TX | [Quick apply](https://jobright.ai/jobs/info/a) [Company](https://careers.acme.com/jobs?jobId=123) |
'''
    assert trackers._parse_markdown_table(text, 'tracker-md')[0]['url'] == 'https://careers.acme.com/jobs?jobId=123'
    rows = simplify.postings_from_listings([dict(id='1', company_name='Acme', title='Software Intern', url='https://simplify.jobs/p/a', application_url='https://careers.acme.com/jobs/123')])
    assert rows[0]['url'] == 'https://careers.acme.com/jobs/123'


def test_cached_tracker_feeds_are_reused_without_repository_search(monkeypatch):
    monkeypatch.setattr(trackers, 'MARKDOWN_TRACKERS', [])
    monkeypatch.setattr(trackers, 'discover_feeds', lambda: pytest.fail('Repository discovery must not run on every scan'))
    monkeypatch.setattr(trackers, 'get_json', lambda *a, **k: [dict(id='1', company_name='Acme', title='Software Intern', url='https://careers.acme.com/jobs/123')])
    assert len(trackers.fetch(feeds=[('json', 'known', 'https://raw.githubusercontent.com/acme/jobs/main/listings.json')])) == 1


def test_encoded_employer_description_is_readable():
    assert clean_description('&lt;p&gt;Must be a US citizen&lt;/p&gt;').strip() == 'Must be a US citizen'


def test_workday_fetches_term_hidden_in_description(monkeypatch):
    from unittest.mock import Mock
    from poller.sources import ats
    client = Mock()
    client.post.return_value.json.return_value = {"jobPostings": [{"title": "Software Engineering Intern", "locationsText": "2 Locations", "externalPath": "/job/Intern_123"}], "total": 1}
    monkeypatch.setattr(ats, "session", lambda: client)
    monkeypatch.setattr(ats, "get_json", lambda *a, **k: {"jobPostingInfo": {"jobDescription": "Join us for Summer 2027.", "location": "Austin, TX", "additionalLocations": ["Boston, MA"]}})
    row = ats.fetch_workday("acme", "Careers", "Acme", "wd5")[0]
    assert eligible(row["title"], row["location"], row["term"], row["description"])
