from datetime import timedelta
from types import SimpleNamespace

import pytest
import requests
from sqlalchemy import create_engine
from shared.db import ApplicationSync, Posting, get_session_factory, init_db, utcnow
from shared import google_sheet


@pytest.fixture
def sheet_db(tmp_path, monkeypatch):
    engine = init_db(create_engine(f"sqlite:///{tmp_path / 'sheet.db'}"))
    Session = get_session_factory(engine)
    monkeypatch.setenv("GOOGLE_SHEETS_WEBHOOK_URL", "https://script.google.com/macros/s/test/exec")
    monkeypatch.setenv("GOOGLE_SHEETS_SYNC_TOKEN", "test-only-token")
    with Session() as db:
        p = Posting(company_name="Example", title="Summer 2027 Intern", url="https://example.com/job",
                    source="test", raw_hash="sheet-test", first_seen_at=utcnow(),
                    status="applied", applied_at=utcnow(), status_updated_at=utcnow(), notes="=1+1")
        db.add(p)
        db.flush()
        google_sheet.enqueue(db, p)
        db.commit()
    return engine, Session


def ack(**kwargs):
    return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
        "ok": True, "acknowledged": [[r[0], r[10]] for r in kwargs["json"]["rows"]]
    })


def test_failed_transport_retries_from_durable_queue(sheet_db, monkeypatch):
    engine, Session = sheet_db
    def fail(*args, **kwargs):
        raise requests.Timeout()
    monkeypatch.setattr(google_sheet.requests, "post", fail)
    assert google_sheet.sync_pending(engine) == 0
    with Session() as db:
        item = db.get(ApplicationSync, 1)
        assert item.state == "pending" and item.attempts == 1
        assert db.get(Posting, 1).status == "applied"
    monkeypatch.setattr(google_sheet.requests, "post", lambda url, **kw: ack(**kw))
    assert google_sheet.sync_pending(engine) == 1
    assert google_sheet.sync_pending(engine) == 0
    with Session() as db:
        assert db.get(ApplicationSync, 1).synced_at is not None


def test_new_edit_during_send_is_not_acknowledged_as_old_version(sheet_db, monkeypatch):
    engine, Session = sheet_db
    def edit_during_send(url, **kwargs):
        with Session() as db:
            p = db.get(Posting, 1)
            p.status = "interview"
            p.status_updated_at += timedelta(seconds=1)
            google_sheet.enqueue(db, p)
            db.commit()
        return ack(**kwargs)
    monkeypatch.setattr(google_sheet.requests, "post", edit_during_send)
    assert google_sheet.sync_pending(engine) == 0
    with Session() as db:
        assert db.get(ApplicationSync, 1).state == "pending"
        assert 'interview' in db.get(ApplicationSync, 1).payload


def test_unacknowledged_rows_and_missing_configuration_stay_pending(sheet_db, monkeypatch):
    engine, Session = sheet_db
    monkeypatch.setattr(google_sheet.requests, "post", lambda *a, **k: SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {"ok": True, "acknowledged": []}))
    assert google_sheet.sync_pending(engine) == 0
    monkeypatch.setenv("GOOGLE_SHEETS_WEBHOOK_URL", "https://example.com/not-google")
    assert not google_sheet.configured()
    assert google_sheet.sync_pending(engine) == 0
    with Session() as db:
        assert db.get(ApplicationSync, 1).state == "pending"


def test_apps_script_receiver():
    import shutil
    import subprocess
    from pathlib import Path
    if not shutil.which("node"):
        pytest.skip("Node is required for the Apps Script receiver contract check")
    subprocess.run(["node", "--test", "tests/test_sheets_bridge.cjs"],
                   cwd=Path(__file__).resolve().parents[1], check=True, capture_output=True, text=True)
