"""Removal must disable old clients without harming manual application tracking."""
from sqlalchemy import inspect, text
from shared.db import Base, Posting, init_db
from test_app import client


def test_removed_ui_and_worker_endpoints(client):
    c,_,Session=client
    for path in ('/','/applications','/jobs/1','/profile','/filters'):
        page=c.get(path)
        assert page.status_code==200
        assert 'href="/autopilot' not in page.text
        assert 'href="/mail-updates' not in page.text
        assert 'href="/apply-settings' not in page.text
        assert 'Automatic application</h2>' not in page.text
    for path in ('/autopilot','/autopilot/answers','/autopilot/tasks/1','/mail-updates','/apply-settings'):
        assert c.get(path).url.path=='/'
    for path in ('/autopilot/control','/autopilot/connect','/autopilot/backfill','/autopilot/tasks/1/retry','/integrations/auto-apply','/integrations/mail','/mail-connection/setup','/jobs/1/apply','/resumes'):
        assert c.post(path,json={'action':'claim'}).status_code==410
    assert c.post('/jobs/1/status',data={'status':'applied'}).status_code==200
    with Session() as db:
        assert db.get(Posting,1).applied_at
    assert not any(name in Base.metadata.tables for name in ('auto_applications','auto_apply_settings','application_tasks','stored_resumes','mail_connection'))


def test_upgrade_revokes_connections_and_retires_pending_tasks(db):
    engine=db.get_bind()
    db.commit()
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE auto_apply_settings (mode TEXT, token_digest TEXT)'))
        conn.execute(text("INSERT INTO auto_apply_settings VALUES ('running','old-worker-token')"))
        conn.execute(text('CREATE TABLE mail_connection (enabled BOOLEAN, token_digest TEXT)'))
        conn.execute(text("INSERT INTO mail_connection VALUES (TRUE,'old-mail-token')"))
        for table in ('application_tasks','auto_applications'):
            conn.execute(text(f'CREATE TABLE {table} (id INTEGER, state TEXT, submission_started_at TIMESTAMP, claim_token TEXT, detail TEXT)'))
            conn.execute(text(f"INSERT INTO {table} VALUES (1,'queued',NULL,'lease',''),(2,'submitting','2026-09-12 01:00:00','lease',''),(3,'submitted','2026-09-12 01:00:00',NULL,'receipt'),(4,'uncertain','2026-09-12 01:00:00',NULL,'verify')"))
    init_db(engine);init_db(engine)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT mode,token_digest FROM auto_apply_settings')).one()==('stopped','')
        assert conn.execute(text('SELECT enabled,token_digest FROM mail_connection')).one()==(False,'')
        for table in ('application_tasks','auto_applications'):
            assert list(conn.execute(text(f'SELECT id,state FROM {table} ORDER BY id')))==[(1,'cancelled'),(2,'uncertain'),(3,'submitted'),(4,'uncertain')]
        assert conn.scalar(text('SELECT count(*) FROM auto_applications WHERE id IN (1,2) AND claim_token IS NOT NULL'))==0
