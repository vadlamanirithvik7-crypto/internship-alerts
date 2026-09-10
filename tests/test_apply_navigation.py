"""Regression for applying with saved PDFs but no matching profile."""
from urllib.parse import urlsplit
from contextlib import contextmanager
import socket
import threading
import time

import uvicorn

import pytest
from sqlalchemy import delete, select

from shared.db import ApplicationTask, Posting, ResumeProfile, StoredResume
from test_app import client
from test_applying import pdf_bytes, save_settings


def prepare_perpay(client):
    c, _, Session = client
    with Session() as db:
        db.execute(delete(ResumeProfile))
        posting = db.get(Posting, 1)
        posting.company_name = "Perpay"
        posting.title = "Software Engineering Internship, Summer 2027"
        posting.url = "https://job-boards.greenhouse.io/perpay/jobs/4076988007"
        db.commit()
    save_settings(c)
    c.post('/resumes', data={'name': 'Software'},
           files={'resume': ('software.pdf', pdf_bytes(), 'application/pdf')})


def test_blank_profile_links_and_validation(client):
    c, _, _ = client
    prepare_perpay(client)
    for path in ['/jobs/1?profile=', '/profile?profile=']:
        assert c.get(path).status_code == 200
    for path in ['/jobs/1?profile=garbage', '/profile?profile=garbage']:
        assert c.get(path).status_code == 422
    assert c.get('/jobs/1?profile=1').status_code == 200
    for path in ['/', '/partials/postings', '/jobs/1']:
        assert '?profile="' not in c.get(path).text


@contextmanager
def local_app(app):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        server = uvicorn.Server(uvicorn.Config(app, lifespan='off', log_level='error'))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started:
                if time.monotonic() >= deadline:
                    raise RuntimeError('Test app did not start')
                time.sleep(0.01)
            yield f'http://127.0.0.1:{sock.getsockname()[1]}'
        finally:
            server.should_exit = True
            thread.join(timeout=5)


def test_phone_discover_opens_employer_without_matching_profile(client):
    c, _, Session = client
    prepare_perpay(client)
    pw = pytest.importorskip('playwright.sync_api')
    with local_app(client[1].app) as origin, pw.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        page = browser.new_page(viewport={'width': 390, 'height': 844},
                                is_mobile=True, has_touch=True)

        # Employer navigation is intercepted: never submit a real application.
        def route_request(route):
            if route.request.url.startswith(origin + '/'):
                route.continue_()
            else:
                route.fulfill(status=200, content_type='text/html', body='<h1>Employer application fixture</h1>')
        page.route('**/*', route_request)
        page.goto(origin)
        page.get_by_role('link', name='Software Engineering Internship, Summer 2027', exact=True).click()
        page.get_by_role('link', name='Open employer application').click()
        page.wait_for_url('https://job-boards.greenhouse.io/perpay/jobs/4076988007')
        assert page.get_by_role('heading', name='Employer application fixture').is_visible()
        browser.close()
    with Session() as db:
        assert db.scalar(select(ApplicationTask)) is None
        assert db.get(Posting, 1).applied_at is None


def test_matching_profile_still_preserved(client):
    c, _, _ = client
    assert 'href="/jobs/1?profile=2"' in c.get('/?profile=2').text
    detail = c.get('/jobs/1?profile=2')
    assert detail.status_code == 200
    assert 'href="/?profile=2"' in detail.text
