from types import SimpleNamespace
from unittest.mock import Mock
from shared.db import ResumeProfile, Posting, utcnow, pack_list
from shared import matching


def profile(**kwargs):
    return SimpleNamespace(
        id=1,
        name="Software",
        resume_text="Built Python FastAPI services with PostgreSQL.",
        preferences="Backend engineering",
        locations="",
        term="",
        exclusions="",
        remote_only=False,
        **kwargs,
    )


def job(**kwargs):
    args = dict(
        id=1,
        title="Backend Intern",
        description="Build Python FastAPI services. Kubernetes is preferred.",
        company_name="Acme",
        location="Austin",
        term="Summer 2027",
        remote=False,
        source="lever",
        first_seen_at=utcnow(),
    )
    args.update(kwargs)
    return SimpleNamespace(**args)


def test_fallback_is_explicit(db, monkeypatch):
    monkeypatch.setattr(matching, "vectors", Mock(side_effect=RuntimeError("offline")))
    rows, scores, mode = matching.rank(db, [job()], profile())
    assert mode == "keyword fallback" and len(rows) == 1


def test_hard_preferences_are_not_overridden_by_similarity(db, monkeypatch):
    p = profile()
    p.remote_only = True
    assert matching.rank(db, [job()], p)[0] == []
    p.remote_only = False
    p.term = "Fall 2027"
    assert not matching.allowed(job(), p)
    p.term = ""
    p.exclusions = pack_list(["Kubernetes"])
    assert not matching.allowed(job(), p)


def test_excerpts_are_grounded_and_gaps_are_not_claimed_as_missing_skills():
    p, j = profile(), job()
    result = matching.evidence(p, j)
    assert result["matches"]
    assert all(
        m["resume_quote"] in p.resume_text and m["job_quote"] in matching.job_text(j)
        for m in result["matches"]
    )
    assert result["not_demonstrated"][0]["skill"] == "Kubernetes"


def test_llm_hallucinated_quotes_rejected(db, monkeypatch):
    import requests, json

    monkeypatch.setenv("OLLAMA_URL", "http://localhost:11434")
    response = Mock()
    response.json.return_value = {
        "message": {
            "content": json.dumps(
                {
                    "matches": [
                        {
                            "skill": "Python",
                            "resume_quote": "Made up",
                            "job_quote": "Made up",
                        }
                    ],
                    "not_demonstrated": [],
                }
            )
        }
    }
    monkeypatch.setattr(requests, "post", Mock(return_value=response))
    result = matching.explain(db, profile(), job())
    assert result["mode"] == "source excerpts"


def test_llm_valid_quotes_cached_and_invalidated(db, monkeypatch):
    import requests, json

    monkeypatch.setenv("OLLAMA_URL", "http://localhost:11434")
    p, j = profile(), job()
    response = Mock()
    response.json.return_value = {
        "message": {
            "content": json.dumps(
                {
                    "matches": [
                        {
                            "skill": "Python",
                            "resume_quote": p.resume_text,
                            "job_quote": j.description,
                        }
                    ],
                    "not_demonstrated": [],
                }
            )
        }
    }
    sender = Mock(return_value=response)
    monkeypatch.setattr(requests, "post", sender)
    assert matching.explain(db, p, j)["mode"] == "AI with verified excerpts"
    matching.explain(db, p, j)
    assert sender.call_count == 1
    p.resume_text += " New project."
    matching.explain(db, p, j)
    assert sender.call_count == 2


def test_exact_technical_tokens():
    assert {"c++", "c#", ".net", "rtl"} <= matching.tokens("C++ C# .NET RTL")


def test_metric_known_order():
    from evaluation.evaluate import metrics

    good = metrics([1, 2, 3], {1: 3, 2: 2, 3: 0})
    assert good["ndcg_at_10"] == 1 and good["precision_at_10"] == 0.6667
    assert metrics([3, 2, 1], {1: 3, 2: 2, 3: 0})["ndcg_at_10"] < 1
