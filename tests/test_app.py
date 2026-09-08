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
