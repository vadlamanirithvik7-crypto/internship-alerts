from datetime import timedelta
from unittest.mock import Mock
from sqlalchemy import select, func, text, inspect
from shared.db import (
    AlertSent,
    Delivery,
    Filter,
    Company,
    Posting,
    get_engine,
    init_db,
    pack_list,
    utcnow,
)
from poller.store import upsert_companies, upsert_postings, reconcile_board
from poller.normalize import make_posting, is_remote
from poller import matcher, alerts


def job(id="1", **kwargs):
    args = dict(
        company_name="Acme",
        title="Python Intern",
        location="Austin, TX",
        url=f"https://jobs.lever.co/acme/{id}",
        description="Build Python backend APIs.",
        source="lever",
    )
    args.update(kwargs)
    return make_posting(**args)


def ingest(db, rows, **kw):
    upsert_companies(db, rows)
    ps = upsert_postings(db, rows, **kw)
    db.commit()
    return ps


def filt(db, **kw):
    f = Filter(name="Backend", channels=pack_list(["email"]), **kw)
    db.add(f)
    db.commit()
    return f


def test_retry_without_new_inserts_and_past_lookback(db, monkeypatch):
    p = ingest(db, [job()])[0]
    filt(db)
    sender = Mock(return_value=False)
    monkeypatch.setattr(matcher, "send_email", sender)
    assert matcher.process_new_postings(db) == {}
    p.first_seen_at = utcnow() - timedelta(days=10)
    db.commit()
    sender.return_value = True
    assert matcher.process_new_postings(db) == {"Backend/email": 1}
    assert db.scalar(select(func.count()).select_from(AlertSent)) == 1
    assert matcher.process_new_postings(db) == {}
    assert sender.call_count == 2


def test_new_filter_sees_yesterday(db, monkeypatch):
    p = ingest(db, [job()])[0]
    p.first_seen_at = utcnow() - timedelta(days=1)
    db.commit()
    filt(db)
    monkeypatch.setattr(matcher, "send_email", Mock(return_value=True))
    assert matcher.process_new_postings(db) == {"Backend/email": 1}


def test_baseline_never_alerts_next_run(db, monkeypatch):
    ingest(db, [job()], alert_eligible=False)
    filt(db)
    sender = Mock()
    monkeypatch.setattr(matcher, "send_email", sender)
    matcher.process_new_postings(db)
    sender.assert_not_called()


def test_digest_consolidates_filters(db, monkeypatch):
    ingest(db, [job()])
    filt(db)
    filt(db)
    sender = Mock(return_value=True)
    monkeypatch.setattr(matcher, "send_email", sender)
    matcher.process_new_postings(db)
    assert sender.call_count == 1
    assert len(sender.call_args.args[0]) == 1
    assert db.scalar(select(func.count()).select_from(AlertSent)) == 2


def test_partial_push_only_records_delivered_ids(db, monkeypatch):
    rows = ingest(db, [job("1"), job("2")])
    f = filt(db)
    f.channels = pack_list(["ntfy"])
    db.commit()
    monkeypatch.setattr(matcher, "send_ntfy_deliveries", lambda ps: {rows[0].id})
    matcher.process_new_postings(db)
    assert list(db.scalars(select(AlertSent.posting_id))) == [rows[0].id]
    assert (
        db.scalar(select(Delivery.state).where(Delivery.posting_id == rows[1].id))
        == "pending"
    )


def test_wrapper_dedupe_does_not_merge_rows_or_lose_failed_alert(db, monkeypatch):
    ingest(
        db, [job("1"), job(url="https://jobright.ai/jobs/info/a", source="trackers")]
    )
    filt(db)
    sender = Mock(return_value=False)
    monkeypatch.setattr(matcher, "send_email", sender)
    matcher.process_new_postings(db)
    assert len(sender.call_args.args[0]) == 1
    assert set(db.scalars(select(Delivery.state))) == {"pending"}
    sender.return_value = True
    matcher.process_new_postings(db)
    assert set(db.scalars(select(Delivery.state))) == {"sent", "suppressed"}
    assert db.scalar(select(func.count()).select_from(Posting)) == 2


def test_same_title_distinct_direct_requisitions_both_alert(db, monkeypatch):
    ingest(db, [job("1"), job("2")])
    filt(db)
    sender = Mock(return_value=True)
    monkeypatch.setattr(matcher, "send_email", sender)
    matcher.process_new_postings(db)
    assert len(sender.call_args.args[0]) == 2


def test_tagging_input_survives_reingestion(db):
    from shared.sectors import tag_posting

    p = ingest(db, [job(description="Implement RTL and FPGA testbenches.")])[0]
    original = p.sector_tags
    assert p.description == "Implement RTL and FPGA testbenches."
    assert original == pack_list(tag_posting(p.title, p.description, p.category_hint))
    old = p.last_seen_at
    assert ingest(db, [job(description="")]) == []
    assert p.sector_tags == original and p.last_seen_at >= old


def test_closure_requires_three_complete_scans_and_reopens(db):
    p = ingest(db, [job()])[0]
    c = db.get(Company, p.company_id)
    for _ in range(5):
        reconcile_board(db, c, [], complete=False)
    assert not p.closed_at
    for _ in range(2):
        reconcile_board(db, c, [], complete=True)
    assert not p.closed_at
    reconcile_board(db, c, [], complete=True)
    assert p.closed_at
    ingest(db, [job()])
    assert not p.closed_at


def test_tracker_inactive_updates_known_posting(db):
    p = ingest(db, [job(source="simplify")])[0]
    closed = job(source="simplify")
    closed["active"] = False
    ingest(db, [closed])
    assert p.closed_at


def test_workday_host_is_retained(db):
    ingest(
        db,
        [
            job(
                url="https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Intern",
                source="workday",
            )
        ],
    )
    c = db.scalar(select(Company))
    assert c.workday_host == "wd5" and c.resolved


def test_migration_preserves_existing_data_and_repeats():
    engine = get_engine("sqlite://")
    # Use actual old schema so create_all must upgrade, not recreate.
    _create_legacy_schema(engine)
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO companies(name,ats_type,resolved) VALUES ('Legacy','other',0)"
            )
        )
    init_db(engine)
    from sqlalchemy import event
    startup_writes = []
    def capture_startup(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split()[0].upper() in {"ALTER", "CREATE", "UPDATE", "REVOKE"}:
            startup_writes.append(statement)
    event.listen(engine, "before_cursor_execute", capture_startup)
    try:
        init_db(engine)
    finally:
        event.remove(engine, "before_cursor_execute", capture_startup)
    assert startup_writes == [], "A fully upgraded restart must not lock app tables for DDL"
    with engine.connect() as c:
        assert c.scalar(text("SELECT name FROM companies")) == "Legacy"
        assert c.scalar(text("SELECT priority FROM companies")) == 0
        assert "description" in {x["name"] for x in inspect(c).get_columns("postings")}


def test_filter_semantics(db):
    p = ingest(db, [job()])[0]
    f = filt(db)
    assert matcher.posting_matches(p, f)
    f.sectors = pack_list(["robotics"])
    f.keywords = pack_list(["Python"])
    assert matcher.posting_matches(p, f)
    f.exclude_keywords = pack_list(["Python"])
    assert not matcher.posting_matches(p, f)
    f.exclude_keywords = ""
    f.locations = pack_list(["Boston"])
    assert not matcher.posting_matches(p, f)
    p.remote = True
    assert matcher.posting_matches(p, f)


def test_distributed_systems_title_does_not_imply_remote():
    assert not is_remote("Seattle, WA", "Distributed Systems Intern")
    assert is_remote("Remote, US")


def test_ntfy_token_and_partial_receipts(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "test")
    monkeypatch.setenv("NTFY_TOKEN", "tk_test")
    request = Mock()
    monkeypatch.setattr(alerts.requests, "post", request)
    ps = [
        Mock(
            id=1,
            title="Python – Intern",
            company_name="A",
            location="Austin",
            url="https://example.com",
        )
    ]
    assert alerts.send_ntfy_deliveries(ps) == {1}
    assert request.call_args.kwargs["headers"]["Authorization"] == "Bearer tk_test"


def test_postgres_schema_upgrade_when_available():
    import os, pytest

    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("CI runs the real PostgreSQL migration check")
    engine = get_engine(url)
    # Isolated CI service database only; refuse a non-test database.
    assert engine.url.database == "radar_test"
    _create_legacy_schema(engine)
    with engine.begin() as c:
        for role in ("anon", "authenticated"):
            if not c.scalar(
                text("SELECT 1 FROM pg_roles WHERE rolname=:role"), {"role": role}
            ):
                c.execute(text(f"CREATE ROLE {role} NOLOGIN"))
            c.execute(text(f"GRANT ALL ON ALL TABLES IN SCHEMA public TO {role}"))
            c.execute(
                text(
                    f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO {role}"
                )
            )
        c.execute(
            text(
                "INSERT INTO companies(name,ats_type,resolved) VALUES ('Legacy','other',false)"
            )
        )
    init_db(engine)
    from sqlalchemy import event
    startup_writes = []
    def capture_startup(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split()[0].upper() in {"ALTER", "CREATE", "UPDATE", "REVOKE"}:
            startup_writes.append(statement)
    event.listen(engine, "before_cursor_execute", capture_startup)
    try:
        init_db(engine)
    finally:
        event.remove(engine, "before_cursor_execute", capture_startup)
    assert startup_writes == [], "A PostgreSQL restart must not take migration table locks"
    with engine.connect() as c:
        from shared.db import Base

        for table in Base.metadata.tables:
            assert c.scalar(
                text(
                    "SELECT relrowsecurity FROM pg_class WHERE oid=to_regclass(:table)"
                ),
                {"table": table},
            )
            for role in ("anon", "authenticated"):
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    assert not c.scalar(
                        text("SELECT has_table_privilege(:role,:table,:privilege)"),
                        {"role": role, "table": table, "privilege": privilege},
                    )
    from shared.db import get_session_factory

    with get_session_factory(engine)() as db:
        assert (
            db.scalar(select(Company).where(Company.name == "Legacy")).priority is False
        )
        ingest(db, [job()])
        filt(db)


def test_ats_description_disclaimer_does_not_make_fulltime_role_an_internship(db):
    assert (
        ingest(
            db,
            [
                job(
                    title="Senior Software Engineer",
                    description="We offer benefits to employees and interns.",
                )
            ],
        )
        == []
    )


def _create_legacy_schema(engine):
    from pathlib import Path

    sql = (
        Path(__file__).parent / "fixtures" / f"legacy_{engine.dialect.name}.sql"
    ).read_text()
    with engine.begin() as conn:
        for statement in sql.split(";"):
            if statement.strip():
                conn.execute(text(statement))


def test_applied_postings_do_not_notify_again(db, monkeypatch):
    p = ingest(db, [job(term="Summer 2027")])[0]
    filt(db)
    p.status = "applied"
    p.applied_at = utcnow()
    db.commit()
    sender = Mock()
    monkeypatch.setattr(matcher, "send_email", sender)
    assert matcher.process_new_postings(db, target_only=True) == {}
    sender.assert_not_called()


def test_target_only_ingestion_and_existing_description_enrichment(db):
    assert ingest(db, [job(term="Summer 2026")], target_only=True) == []
    assert (
        ingest(db, [job(term="Summer 2027", location="Remote")], target_only=True) == []
    )
    row = ingest(db, [job(term="Summer 2027")], target_only=True)[0]
    assert row.target_eligible
    ingest(db, [job(description="Expanded Python and SQL evidence")], target_only=True)
    db.refresh(row)
    assert row.target_eligible and "Expanded" in row.description
