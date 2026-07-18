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
    """One row per (posting, filter, channel) so a posting alerts at most once."""

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
    Base.metadata.create_all(engine)
    return engine
