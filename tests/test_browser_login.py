import re
from html import unescape

import pytest

from backend.auth import SESSION_COOKIE, issue_token, safe_next, valid_token
from test_app import client
from test_apply_navigation import local_app


def login_form(c):
    response = c.get('/login')
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    return re.search(r'name="csrf" value="([^"]+)"', response.text).group(1)


def test_browser_redirect_login_and_logout(client, monkeypatch):
    c, _, _ = client
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-secret')
    response = c.get('/jobs/1?profile=', headers={'Accept': 'text/html'})
    assert response.status_code == 200 and response.url.path == '/login'
    assert 'Workspace password' in response.text
    assert 'www-authenticate' not in response.headers
    csrf = login_form(c)
    result = c.post('/login', data={'csrf': csrf, 'password': 'test-secret', 'next': '/jobs/1?profile='})
    assert result.status_code == 200 and result.url.path == '/jobs/1'
    assert c.get('/apply-settings').status_code == 200
    assert c.post('/logout').url.path == '/login'
    assert c.get('/apply-settings').status_code == 401


def test_incorrect_password_csrf_and_external_redirect(client, monkeypatch):
    c, _, _ = client
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-secret')
    csrf = login_form(c)
    bad = c.post('/login', data={'csrf': csrf, 'password': 'wrong'})
    assert "wasn't correct" in unescape(bad.text)
    assert c.get('/').status_code == 401
    for data, headers in [({'password': 'test-secret'}, {}),
                          ({'csrf': login_form(c), 'password': 'test-secret'}, {'Origin': 'https://evil.example'})]:
        assert 'expired' in c.post('/login', data=data, headers=headers).text
        assert c.get('/').status_code == 401
    csrf = login_form(c)
    result = c.post('/login', data={'csrf': csrf, 'password': 'test-secret', 'next': '//evil.example'}, follow_redirects=False)
    assert result.headers['location'] == '/'
    for value in ['https://evil.example', '//evil.example', '/\\evil.example', '/\nevil.example']:
        assert safe_next(value) == '/'


def test_session_expiry_tampering_rotation_and_secure_cookie(client, monkeypatch):
    c, _, _ = client
    monkeypatch.setenv('ADMIN_PASSWORD', 'secret')
    for token in [issue_token('secret', 'session', -1),
                  issue_token('secret', 'login', 900),
                  issue_token('secret', 'session', 900) + 'x', 'garbage']:
        c.cookies.set(SESSION_COOKIE, token)
        assert c.get('/').status_code == 401
    token = issue_token('secret', 'session', 900)
    assert valid_token(token, 'secret', 'session')
    c.cookies.set(SESSION_COOKIE, token)
    assert c.get('/').status_code == 200
    monkeypatch.setenv('ADMIN_PASSWORD', 'changed')
    assert c.get('/').status_code == 401
    c.cookies.clear()
    c.base_url = 'https://radar.example'
    csrf = login_form(c)
    response = c.post('/login', data={'csrf': csrf, 'password': 'changed'}, follow_redirects=False)
    cookie = response.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=strict' in cookie


def test_login_keeps_demo_separate_and_basic_compatible(client, monkeypatch):
    c, _, _ = client
    monkeypatch.setenv('ADMIN_PASSWORD', 'secret')
    assert c.get('/demo/login').status_code == 404
    assert c.post('/demo/login', data={'password': 'secret'}).status_code == 404
    assert c.get('/demo/').status_code == 200
    assert c.get('/', auth=('owner', 'secret')).status_code == 200
    assert c.get('/', headers={'Authorization': 'Basic malformed'}).status_code == 401


def test_phone_sign_in_returns_to_requested_role(client, monkeypatch):
    monkeypatch.setenv('ADMIN_PASSWORD', 'phone-test')
    pw = pytest.importorskip('playwright.sync_api')
    with local_app(client[1].app) as origin, pw.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        page = browser.new_page(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
        page.route('**/*', lambda route: route.continue_() if route.request.url.startswith(origin + '/') else route.abort())
        page.goto(origin + '/jobs/1?profile=')
        assert page.get_by_role('heading', name='Welcome back.').is_visible()
        page.get_by_label('Workspace password').fill('phone-test')
        page.get_by_role('button', name='Sign in', exact=True).click()
        page.wait_for_url('**/jobs/1?profile=')
        assert page.get_by_role('link', name='Open employer application').is_visible()
        page.reload()
        assert page.get_by_role('link', name='Open employer application').is_visible()
        browser.close()
