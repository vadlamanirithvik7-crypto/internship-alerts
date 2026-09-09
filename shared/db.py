"""Database models and session helpers.

Portability note: list-valued fields (sector tags, filter keywords, etc.) are stored
as delimiter-wrapped strings like "|semiconductor|robotics|" rather than Postgres
ARRAY columns. This keeps the schema identical on SQLite (local dev/testing) and
Postgres (Supabase in production), and makes membership queries a simple, portable
LIKE '%|semiconductor|%'. Use pack_list()/unpack_list() at every boundary.
"""

import hashlib
import os
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()

DELIM = "|"


def utcnow():
    """Naive UTC timestamp (columns are timezone-naive)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def pack_list(values) -> str:
    """['a', 'b'] -> '|a|b|'   (empty list -> '')"""
    cleaned = [str(v).strip().lower() for v in (values or []) if str(v).strip()]
    if not cleaned:
        return ""
    return DELIM + DELIM.join(cleaned) + DELIM


def unpack_list(packed: str) -> list:
    """'|a|b|' -> ['a', 'b']"""
    if not packed:
        return []
    return [v for v in packed.split(DELIM) if v]


def like_term(value: str) -> str:
    """Build the LIKE pattern that matches `value` inside a packed list column."""
    return f"%{DELIM}{value.strip().lower()}{DELIM}%"


def raw_hash(*parts: str) -> str:
    """Stable identity for a posting, used to dedupe across sources and runs."""
    return hashlib.sha256(
        DELIM.join(p.strip().lower() for p in parts if p).encode()
    ).hexdigest()


class Company(Base):
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True)
    name = Column(String(300), nullable=False, unique=True)
    # greenhouse | lever | workday | ashby | smartrecruiters | other | unresolved
    ats_type = Column(String(40), nullable=False, default="unresolved")
    slug = Column(String(200), nullable=True)
    workday_tenant = Column(String(200), nullable=True)
    workday_site = Column(String(200), nullable=True)
    resolved = Column(Boolean, nullable=False, default=False)
    source_hint = Column(String(100), nullable=True)  # where we discovered it
    last_checked_at = Column(DateTime, nullable=True)
    workday_host = Column(String(20), nullable=True)
    priority = Column(Boolean, nullable=False, default=False, server_default="0")


class Posting(Base):
    __tablename__ = "postings"

    id = Column(Integer, primary_key=True)
    external_id = Column(String(200), nullable=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True)
    company_name = Column(String(300), nullable=False)
    title = Column(String(500), nullable=False)
    url = Column(Text, nullable=False)
    location = Column(Text, nullable=True)
    remote = Column(Boolean, nullable=False, default=False)
    source = Column(String(50), nullable=False)
    category_hint = Column(String(120), nullable=True)
    sector_tags = Column(Text, nullable=False, default="")  # packed list
    term = Column(String(120), nullable=True)  # e.g. "Summer 2026"
    posted_at = Column(DateTime, nullable=True)
    first_seen_at = Column(DateTime, nullable=False)
    raw_hash = Column(String(64), nullable=False)
    description = Column(
        Text, nullable=True
    )  # NULL means legacy input was not retained
    last_seen_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, nullable=True)
    missing_sweeps = Column(Integer, nullable=False, default=0, server_default="0")
    status = Column(String(30), nullable=False, default="new", server_default="new")
    notes = Column(Text, nullable=False, default="", server_default="")
    soft_key = Column(String(64), nullable=True, index=True)
    alert_eligible = Column(Boolean, nullable=False, default=True, server_default="1")
    target_eligible = Column(Boolean, nullable=True, index=True)
    applied_at = Column(DateTime, nullable=True)
    status_updated_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("raw_hash", name="uq_posting_raw_hash"),
        Index("ix_postings_first_seen_at", "first_seen_at"),
        Index("ix_postings_company_name", "company_name"),
    )


class Filter(Base):
    __tablename__ = "filters"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    sectors = Column(Text, nullable=False, default="")  # packed list
    keywords = Column(Text, nullable=False, default="")  # packed list
    exclude_keywords = Column(Text, nullable=False, default="")  # packed list
    locations = Column(Text, nullable=False, default="")  # packed list
    remote_only = Column(Boolean, nullable=False, default=False)
    channels = Column(Text, nullable=False, default="")  # packed list: email, ntfy
    active = Column(Boolean, nullable=False, default=True)


class AlertSent(Base):
    """Receipt ledger, unique per (posting, filter, channel); transports are at-least-once."""

    __tablename__ = "alerts_sent"

    id = Column(Integer, primary_key=True)
    posting_id = Column(Integer, ForeignKey("postings.id"), nullable=False)
    filter_id = Column(Integer, ForeignKey("filters.id"), nullable=False)
    channel = Column(String(30), nullable=False)
    sent_at = Column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "posting_id", "filter_id", "channel", name="uq_alert_once_per_channel"
        ),
    )


class Delivery(Base):
    """Durable outbox: external transports provide at-least-once delivery."""

    __tablename__ = "deliveries"
    id = Column(Integer, primary_key=True)
    posting_id = Column(Integer, ForeignKey("postings.id"), nullable=False)
    filter_id = Column(Integer, ForeignKey("filters.id"), nullable=False)
    channel = Column(String(30), nullable=False)
    state = Column(String(20), default="pending", nullable=False)
    attempts = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    attempted_at = Column(DateTime)
    __table_args__ = (UniqueConstraint("posting_id", "filter_id", "channel"),)


class ApplicationSync(Base):
    """One durable, replaceable Google Sheets update per application."""

    __tablename__ = "application_sync"
    posting_id = Column(Integer, ForeignKey("postings.id"), primary_key=True)
    version = Column(String(40), nullable=False)
    payload = Column(Text, nullable=False)
    state = Column(String(20), nullable=False, default="pending", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    synced_at = Column(DateTime)


class PollRun(Base):
    __tablename__ = "poll_runs"
    id = Column(Integer, primary_key=True)
    started_at = Column(DateTime, default=utcnow, nullable=False)
    finished_at = Column(DateTime)
    state = Column(String(20), default="running", nullable=False)
    harvested = Column(Integer, default=0, nullable=False)
    new_postings = Column(Integer, default=0, nullable=False)
    elapsed = Column(Integer, default=0, nullable=False)


class SourceRun(Base):
    __tablename__ = "source_runs"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("poll_runs.id"), nullable=False)
    source = Column(String(80), nullable=False, index=True)
    checked_at = Column(DateTime, default=utcnow, nullable=False)
    count = Column(Integer, default=0, nullable=False)
    state = Column(String(20), nullable=False, default="ok")
    detail = Column(String(500), default="", nullable=False)
    elapsed = Column(Integer, default=0, nullable=False)


class ResumeProfile(Base):
    __tablename__ = "resume_profiles"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    resume_text = Column(Text, nullable=False)
    preferences = Column(Text, default="", nullable=False)
    locations = Column(Text, default="", nullable=False)
    term = Column(String(120), default="", nullable=False)
    exclusions = Column(Text, default="", nullable=False)
    remote_only = Column(Boolean, default=False, nullable=False)
    updated_at = Column(DateTime, default=utcnow, nullable=False)


class AICache(Base):
    __tablename__ = "ai_cache"
    key = Column(String(64), primary_key=True)
    kind = Column(String(30), nullable=False)
    payload = Column(Text, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)


def get_engine(db_url: str = None):
    db_url = db_url or os.environ.get("DATABASE_URL", "sqlite:///internships.db")
    # Supabase connection strings sometimes come as postgres:// which SQLAlchemy 2 rejects.
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    kwargs = {"pool_pre_ping": True} if not db_url.startswith("sqlite") else {}
    return create_engine(db_url, **kwargs)


def get_session_factory(engine=None):
    return sessionmaker(bind=engine or get_engine())


def init_db(engine=None):
    engine = engine or get_engine()
    # Serialize additive startup migrations on PostgreSQL. No drop/rewrite of data.
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_advisory_xact_lock(71429031)"))
        from shared.migrations import migrate

        migrate(conn)
        Base.metadata.create_all(conn)
        if engine.dialect.name == "postgresql":
            # The app uses a trusted server connection, never the client Data API.
            # Protect new tables in the same transaction as their creation.
            roles = set(conn.scalars(text("SELECT rolname FROM pg_roles")))
            quote = conn.dialect.identifier_preparer.quote
            secured = set(conn.scalars(text(
                "SELECT relname FROM pg_class WHERE relnamespace=current_schema()::regnamespace AND relrowsecurity"
            )))
            exposed = {}
            for role in ("anon", "authenticated"):
                if role in roles:
                    exposed[role] = set(conn.scalars(text(
                        "SELECT name FROM unnest(CAST(:tables AS text[])) AS t(name) "
                        "WHERE has_table_privilege(:role, name, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')"
                    ), {"tables": list(Base.metadata.tables), "role": role}))
            for table in Base.metadata.sorted_tables:
                name = quote(table.name)
                # Avoid taking AccessExclusive table locks on every web wakeup
                # while the alert worker is writing companies and postings.
                if table.name not in secured:
                    conn.execute(text(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY"))
                for role in ("anon", "authenticated"):
                    if table.name in exposed.get(role, set()):
                        conn.execute(
                            text(f"REVOKE ALL ON TABLE {name} FROM {quote(role)}")
                        )
    return engine
