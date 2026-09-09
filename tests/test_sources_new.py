from unittest.mock import Mock
from sqlalchemy import select
from poller.sources import ats, simplify
from poller.main import poll_boards
from poller import health
from shared.db import Company, PollRun, utcnow


def test_workday_valid_empty_stops_probing(monkeypatch):
    client = Mock()
    client.post.return_value.json.return_value = {"jobPostings": [], "total": 0}
    monkeypatch.setattr(ats, "session", lambda: client)
    result = ats.fetch_workday("acme", "Careers", "Acme", "wd5")
    assert result.complete and result.host == "wd5" and len(result) == 0
    assert client.post.call_count == 2
    assert all(".wd5." in c.args[0] for c in client.post.call_args_list)


def test_workday_partial_is_not_closure_evidence(monkeypatch):
    import requests

    client = Mock()
    good = Mock()
    good.json.return_value = {
        "jobPostings": [{"title": "Intern", "externalPath": "/job/1"}] * 20,
        "total": 40,
    }
    client.post.side_effect = [
        good,
        requests.ConnectionError(),
        requests.ConnectionError(),
    ]
    monkeypatch.setattr(ats, "session", lambda: client)
    result = ats.fetch_workday("acme", "Careers", wd_num="wd5")
    assert not result.complete and len(result) == 1


def test_smartrecruiters_pagination(monkeypatch):
    fetch = Mock(
        side_effect=[
            {"content": [{"id": "1", "name": "Engineer"}], "totalFound": 2},
            {"content": [{"id": "2", "name": "Designer"}], "totalFound": 2},
        ]
    )
    monkeypatch.setattr(ats, "get_json", fetch)
    result = ats.fetch_smartrecruiters("Acme")
    assert result.complete and len(result) == 2
    assert fetch.call_args.kwargs["params"]["offset"] == 1


def test_failed_board_distinct_from_valid_empty(monkeypatch):
    monkeypatch.setattr(ats, "get_json", lambda *a, **k: None)
    assert not ats.fetch_greenhouse("missing").complete
    monkeypatch.setattr(ats, "get_json", lambda *a, **k: {"jobs": []})
    assert ats.fetch_greenhouse("empty").complete


def test_priority_runs_before_rotating_slice(db, monkeypatch):
    for name, priority in [("priority", True), ("old", False), ("new", False)]:
        db.add(Company(name=name, ats_type="lever", slug=name, priority=priority))
    db.commit()
    seen = []
    monkeypatch.setattr(
        ats, "fetch_for_company", lambda c: seen.append(c.name) or ats.BoardResult()
    )
    poll_boards(db, 1)
    assert len(seen) == 2 and "priority" in seen
    seen.clear()
    poll_boards(db, 0)
    assert not seen


def test_inactive_signal_not_discarded():
    rows = simplify.postings_from_listings(
        [
            {
                "id": "a",
                "url": "https://example.com",
                "active": False,
                "title": "Intern",
                "company_name": "Acme",
            }
        ]
    )
    assert len(rows) == 1 and rows[0]["active"] is False


def test_health_transitions_and_schedule(db, monkeypatch):
    run = PollRun()
    db.add(run)
    db.flush()
    send = Mock(return_value=True)
    monkeypatch.setattr(health, "publish_ntfy", send)
    health.record(db, run.id, "feed", 4)
    db.commit()
    assert not health.due(db, "feed", 6)
    health.record(db, run.id, "feed", 0)
    db.commit()
    assert send.call_count == 1
    health.record(db, run.id, "feed", 0)
    db.commit()
    assert send.call_count == 1
    health.record(db, run.id, "feed", 2)
    db.commit()
    assert send.call_count == 2


def test_amazon_public_payload(monkeypatch):
    from poller.sources import bigtech

    monkeypatch.setattr(
        bigtech,
        "get_json",
        lambda *a, **k: {
            "hits": 1,
            "jobs": [
                {
                    "id": "123",
                    "title": "Software Intern",
                    "job_path": "/en/jobs/123",
                    "location": "Austin, TX",
                    "description": "Python",
                    "is_intern": True,
                }
            ],
        },
    )
    rows = bigtech.fetch_amazon()
    assert len(rows) == 1 and rows[0]["url"] == "https://www.amazon.jobs/en/jobs/123"


def test_microsoft_search_and_description(monkeypatch):
    from poller.sources import bigtech

    getter = Mock(
        side_effect=[
            {
                "data": {
                    "count": 1,
                    "positions": [
                        {
                            "id": 123,
                            "name": "Software Engineering Intern",
                            "locations": ["United States, Texas, Austin"],
                            "positionUrl": "/careers/job/123",
                        }
                    ],
                }
            },
            {"data": {"jobDescription": "Build Python APIs."}},
        ]
    )
    monkeypatch.setattr(bigtech, "get_json", getter)
    rows = bigtech.fetch_microsoft()
    assert len(rows) == 1 and rows[0]["description"] == "Build Python APIs."


def test_workable_and_recruitee_shapes(monkeypatch):
    monkeypatch.setattr(
        ats,
        "get_json",
        lambda *a, **k: {
            "jobs": [
                {
                    "shortcode": "A",
                    "title": "Intern",
                    "url": "https://apply.workable.com/acme/j/A",
                    "city": "Austin",
                    "country": "US",
                    "description": "Python",
                }
            ]
        },
    )
    rows = ats.fetch_workable("acme")
    assert rows.complete and rows[0]["description"] == "Python"
    monkeypatch.setattr(
        ats,
        "get_json",
        lambda *a, **k: {
            "offers": [
                {
                    "id": 1,
                    "title": "Intern",
                    "slug": "intern",
                    "location": "Austin, TX",
                    "description": "Python",
                    "requirements": "SQL",
                }
            ]
        },
    )
    rows = ats.fetch_recruitee("acme")
    assert rows.complete and rows[0]["url"] == "https://acme.recruitee.com/o/intern"
