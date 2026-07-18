"""FastAPI dashboard: browse postings, manage alert filters, inspect the watchlist.

Server-rendered with Jinja2 + HTMX so there is no JS build step - the whole app is
plain Python plus templates, which keeps the free-tier deploy trivial.
"""

import os
import sys
from datetime import datetime, timedelta

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import distinct, func, or_, select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.db import (  # noqa: E402
    AlertSent,
    Company,
    Filter,
    Posting,
    get_engine,
    get_session_factory,
    init_db,
    like_term,
    pack_list,
    unpack_list,
)
from shared.sectors import sector_labels  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="Internship Alerts")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

engine = init_db(get_engine())
SessionFactory = get_session_factory(engine)

PAGE_SIZE = 50


def get_db():
    session = SessionFactory()
    try:
        yield session
    finally:
        session.close()


def _split_csv(value: str):
    return [part.strip() for part in (value or "").split(",") if part.strip()]


templates.env.globals["sector_labels"] = sector_labels()
templates.env.filters["unpack"] = unpack_list


def build_posting_query(params):
    """Translate query params into a filtered, ordered posting query."""
    query = select(Posting)

    sectors = params.getlist("sector")
    if sectors:
        query = query.where(
            or_(*[Posting.sector_tags.like(like_term(s)) for s in sectors])
        )

    search = (params.get("q") or "").strip()
    if search:
        pattern = f"%{search.lower()}%"
        query = query.where(
            or_(
                func.lower(Posting.title).like(pattern),
                func.lower(Posting.company_name).like(pattern),
                func.lower(Posting.location).like(pattern),
            )
        )

    exclude = (params.get("exclude") or "").strip().lower()
    if exclude:
        for term in _split_csv(exclude):
            query = query.where(~func.lower(Posting.title).like(f"%{term}%"))

    location = (params.get("location") or "").strip().lower()
    if location:
        query = query.where(func.lower(Posting.location).like(f"%{location}%"))

    if params.get("remote"):
        query = query.where(Posting.remote.is_(True))

    source = params.get("source")
    if source:
        query = query.where(Posting.source == source)

    term = (params.get("term") or "").strip().lower()
    if term:
        query = query.where(func.lower(Posting.term).like(f"%{term}%"))

    days = params.get("days")
    if days and days.isdigit():
        cutoff = datetime.utcnow() - timedelta(days=int(days))
        query = query.where(Posting.first_seen_at >= cutoff)

    return query.order_by(Posting.first_seen_at.desc(), Posting.id.desc())


def _feed_context(request: Request, db, page: int):
    params = request.query_params
    query = build_posting_query(params)

    total = db.scalar(select(func.count()).select_from(query.subquery()))
    postings = db.execute(query.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)).scalars().all()

    sources = db.execute(select(distinct(Posting.source)).order_by(Posting.source)).scalars().all()

    return {
        "request": request,
        "postings": postings,
        "total": total,
        "page": page,
        "pages": max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
        "sources": sources,
        "selected_sectors": params.getlist("sector"),
        "params": params,
        "query_string": str(request.url.query),
    }


@app.get("/")
def feed(request: Request, page: int = 1, db=Depends(get_db)):
    context = _feed_context(request, db, page)
    context["stats"] = {
        "postings": db.scalar(select(func.count()).select_from(Posting)),
        "companies": db.scalar(select(func.count()).select_from(Company)),
        "filters": db.scalar(
            select(func.count()).select_from(Filter).where(Filter.active.is_(True))
        ),
        "alerts": db.scalar(select(func.count()).select_from(AlertSent)),
        "today": db.scalar(
            select(func.count())
            .select_from(Posting)
            .where(Posting.first_seen_at >= datetime.utcnow() - timedelta(days=1))
        ),
    }
    return templates.TemplateResponse(request, "feed.html", context)


@app.get("/partials/postings")
def postings_partial(request: Request, page: int = 1, db=Depends(get_db)):
    """HTMX target - returns just the results list so filtering feels instant."""
    return templates.TemplateResponse(
        request, "_postings.html", _feed_context(request, db, page)
    )


@app.get("/filters")
def filters_page(request: Request, db=Depends(get_db)):
    rows = db.execute(select(Filter).order_by(Filter.id)).scalars().all()
    return templates.TemplateResponse(
        request, "filters.html", {"filters": rows}
    )


@app.post("/filters")
def create_filter(
    name: str = Form(...),
    sectors: list = Form(default=[]),
    keywords: str = Form(default=""),
    exclude_keywords: str = Form(default=""),
    locations: str = Form(default=""),
    remote_only: bool = Form(default=False),
    channels: list = Form(default=[]),
    db=Depends(get_db),
):
    db.add(
        Filter(
            name=name.strip() or "Untitled filter",
            sectors=pack_list(sectors),
            keywords=pack_list(_split_csv(keywords)),
            exclude_keywords=pack_list(_split_csv(exclude_keywords)),
            locations=pack_list(_split_csv(locations)),
            remote_only=bool(remote_only),
            channels=pack_list(channels or ["email"]),
            active=True,
        )
    )
    db.commit()
    return RedirectResponse("/filters", status_code=303)


@app.post("/filters/{filter_id}/toggle")
def toggle_filter(filter_id: int, db=Depends(get_db)):
    row = db.get(Filter, filter_id)
    if row:
        row.active = not row.active
        db.commit()
    return RedirectResponse("/filters", status_code=303)


@app.post("/filters/{filter_id}/delete")
def delete_filter(filter_id: int, db=Depends(get_db)):
    row = db.get(Filter, filter_id)
    if row:
        # Clear the alert ledger too, or the unique constraint keeps blocking
        # re-alerts if a filter with the same id is recreated later.
        db.query(AlertSent).filter(AlertSent.filter_id == filter_id).delete()
        db.delete(row)
        db.commit()
    return RedirectResponse("/filters", status_code=303)


@app.get("/companies")
def companies_page(request: Request, q: str = "", page: int = 1, db=Depends(get_db)):
    query = select(Company)
    if q.strip():
        query = query.where(func.lower(Company.name).like(f"%{q.strip().lower()}%"))
    query = query.order_by(Company.resolved.desc(), Company.name)

    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.execute(query.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)).scalars().all()

    by_ats = db.execute(
        select(Company.ats_type, func.count())
        .group_by(Company.ats_type)
        .order_by(func.count().desc())
    ).all()

    return templates.TemplateResponse(
        request,
        "companies.html",
        {
            "companies": rows,
            "total": total,
            "page": page,
            "pages": max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
            "q": q,
            "by_ats": by_ats,
        },
    )


@app.get("/healthz")
def healthz(db=Depends(get_db)):
    return {
        "status": "ok",
        "postings": db.scalar(select(func.count()).select_from(Posting)),
        "companies": db.scalar(select(func.count()).select_from(Company)),
    }
