import importlib
import sys
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient
import pytest
from shared.db import init_db, get_session_factory, Posting, ResumeProfile
from scripts.demo import seed


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setenv("AI_ENABLED", "0")
    monkeypatch.setenv("DEMO_MODE", "0")
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    module = importlib.import_module("backend.main")
    monkeypatch.setattr(module, "DEMO", False)
    monkeypatch.setattr(module, "TARGET_ONLY", False)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    seed(init_db(engine))
    Session = get_session_factory(engine)

    def dependency():
        with Session() as db:
            yield db

    module.app.dependency_overrides[module.get_db] = dependency
    with TestClient(module.app) as client:
        yield client, module, Session
    module.app.dependency_overrides.clear()
    engine.dispose()


def test_all_pages_and_search(client):
    c, _, _ = client
    for path in [
        "/",
        "/jobs/1",
        "/profile",
        "/applications",
        "/companies",
        "/filters",
        "/system",
        "/about",
        "/manifest.webmanifest",
    ]:
        assert c.get(path).status_code == 200, path
    assert "No roles in this view" in c.get("/?q=not-a-real-job").text
    assert c.get("/?page=-1").status_code == 422
    assert c.get("/jobs/99999").status_code == 404
    assert "Backend Engineering Intern" in c.get("/?sector=software_tech").text
    assert "RTL Design Intern" not in c.get("/?sector=software_tech").text


def test_save_application_and_preserve_notes(client):
    c, _, Session = client
    assert (
        c.post(
            "/jobs/1/status", data={"status": "applied", "notes": "Follow up next week"}
        ).status_code
        == 200
    )
    c.post("/jobs/1/status", data={"status": "interview"})
    with Session() as db:
        p = db.get(Posting, 1)
        assert p.status == "interview" and p.notes == "Follow up next week"
    assert c.post("/jobs/1/status", data={"status": "invalid"}).status_code == 422


def test_demo_mutation_is_blocked(client, monkeypatch):
    c, m, _ = client
    monkeypatch.setattr(m, "DEMO", True)
    assert c.post("/jobs/1/status", data={"status": "applied"}).status_code == 403
    assert (
        c.post(
            "/profile", data={"name": "secret", "resume_text": "private"}
        ).status_code
        == 403
    )


def test_private_auth_and_origin(client, monkeypatch):
    c, _, _ = client
    monkeypatch.setenv("ADMIN_PASSWORD", "test-password")
    assert c.get("/").status_code == 401
    assert c.get("/", auth=("owner", "test-password")).status_code == 200
    assert (
        c.post(
            "/jobs/1/status",
            auth=("owner", "test-password"),
            headers={"Origin": "https://evil.example"},
            data={"status": "applied"},
        ).status_code
        == 403
    )
    assert c.get("/healthz").status_code == 200


def test_resume_upload_and_priority(client):
    c, _, Session = client
    response = c.post(
        "/profile",
        data={"name": "Personal", "preferences": "Backend"},
        files={
            "resume": (
                "resume.txt",
                b"Built Python services with PostgreSQL.",
                "text/plain",
            )
        },
    )
    assert response.status_code == 200
    with Session() as db:
        profile = db.scalar(
            select(ResumeProfile).where(ResumeProfile.name == "Personal")
        )
        assert "Python" in profile.resume_text
    assert c.post("/companies/1/priority").status_code == 200


def test_public_demo_isolated_from_private_workspace(client, monkeypatch):
    c, m, Session = client
    monkeypatch.setenv("ADMIN_PASSWORD", "owner-only")
    with Session() as db:
        db.get(Posting, 1).title = "CONFIDENTIAL PRIVATE OPPORTUNITY"
        db.get(ResumeProfile, 1).resume_text = "CONFIDENTIAL PRIVATE RESUME"
        db.commit()
    assert c.get("/").status_code == 401
    assert c.get("/demo").status_code == 200
    for path in [
        "/",
        "/jobs/1",
        "/profile",
        "/filters",
        "/system",
        "/applications",
        "/offline",
    ]:
        response = c.get("/demo" + path)
        assert response.status_code == 200, path
        assert response.headers["X-Radar-Mode"] == "demo"
        assert "CONFIDENTIAL PRIVATE" not in response.text
        # All root-relative links/forms/assets stay inside the public mount.
        import re

        links = re.findall(r'(?:href|action|src)="(/[^\"]*)"', response.text)
        assert links and all(link.startswith("/demo/") for link in links), links
    assert c.post("/demo/jobs/1/status", data={"status": "applied"}).status_code == 403
    assert c.post("/demo/filters", data={"name": "No write"}).status_code == 403
    assert c.get("/demo/manifest.webmanifest").json()["scope"] == "/demo/"
    assert c.get("/demo/sw.js").headers["Service-Worker-Allowed"] == "/demo/"
    assert c.get("/sw.js", auth=("owner", "owner-only")).status_code == 404
    assert (
        "CONFIDENTIAL PRIVATE OPPORTUNITY"
        in c.get("/jobs/1", auth=("owner", "owner-only")).text
    )


def test_alert_filters_and_delivery_history(client):
    from shared.db import Filter, Delivery

    c, _, Session = client
    c.post(
        "/filters",
        data={"name": "Backend watch", "keywords": "backend", "channels": "ntfy"},
    )
    with Session() as db:
        f = db.scalar(select(Filter).where(Filter.name == "Backend watch"))
        db.add(Delivery(posting_id=1, filter_id=f.id, channel="ntfy", attempts=2))
        db.commit()
    page = c.get("/filters").text
    assert "Internship alerts" in page and "Backend watch" in page
    assert "pending" in page and "2 attempts" in page


def test_large_feed_bounds_loaded_rows_and_keeps_all_pages(client):
    from datetime import timedelta
    from sqlalchemy import event
    from shared.db import utcnow

    c, m, Session = client
    with Session() as db:
        for profile in db.scalars(select(ResumeProfile)):
            profile.locations = profile.term = profile.exclusions = ""
            profile.remote_only = False
        for i in range(125):
            db.add(
                Posting(
                    company_name="Scale check",
                    title=f"Python intern {i}",
                    url=f"https://example.com/scale/{i}",
                    source="greenhouse",
                    first_seen_at=utcnow() + timedelta(minutes=i),
                    raw_hash=f"scale-{i}",
                    description="Python software",
                    remote=True,
                )
            )
        db.commit()
    loaded = []

    def track(posting, context):
        loaded.append(posting.id)

    event.listen(Posting, "load", track)
    try:
        page = c.get("/?sort=newest&page=2")
        assert page.status_code == 200
        assert len(loaded) == m.PAGE_SIZE
        assert "Python intern 104" in page.text
        loaded.clear()
        page = c.get("/?sort=keywords")
        assert len(loaded) <= m.RANKING_LIMIT
        assert "Matching the latest 100 of 143 roles" in page.text
        loaded.clear()
        page = c.get("/?sort=newest&page=7")
        assert "Page 7 of 8" in page.text
        assert len(loaded) == m.PAGE_SIZE
    finally:
        event.remove(Posting, "load", track)


def test_applied_action_updates_persistent_workbook(client):
    from io import BytesIO
    from openpyxl import load_workbook
    from shared.db import AICache

    c, _, Session = client
    assert (
        c.post(
            "/jobs/1/status",
            data={"status": "applied", "notes": '=HYPERLINK("https://evil.example")'},
        ).status_code
        == 200
    )
    with Session() as db:
        first = db.get(Posting, 1).applied_at
        assert first is not None
        assert db.get(AICache, "application-workbook-v1") is not None
    c.post("/jobs/1/status", data={"status": "applied"})
    c.post("/jobs/1/status", data={"status": "interview"})
    with Session() as db:
        assert db.get(Posting, 1).applied_at == first
    response = c.get("/applications.xlsx")
    sheet = load_workbook(BytesIO(response.content)).active
    rows = list(sheet.values)
    assert len([row for row in rows[1:] if row[0] == 1]) == 1
    assert rows[1][0] == 1 and rows[1][5] == "interview"
    assert sheet["J2"].data_type == "s"
    assert c.get("/demo/applications.xlsx").status_code == 403


def test_live_feed_requires_target_eligibility(client, monkeypatch):
    c, m, Session = client
    monkeypatch.setattr(m, "TARGET_ONLY", True)
    with Session() as db:
        for p in db.scalars(select(Posting)):
            p.target_eligible = False
        db.get(Posting, 1).target_eligible = True
        db.commit()
    page = c.get("/?sort=newest").text
    assert "Backend Engineering Intern" in page
    assert "RTL Design Intern" not in page
