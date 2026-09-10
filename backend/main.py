"""FastAPI dashboard: browse postings, manage alert filters, inspect the watchlist.

Server-rendered with Jinja2 + HTMX so there is no JS build step - the whole app is
plain Python plus templates, which keeps the free-tier deploy trivial.
"""

import base64
import hmac
import json
import os
import sys
from datetime import datetime, timedelta
from typing import Literal

from fastapi import (
    Depends,
    FastAPI,
    Form,
    Request,
    HTTPException,
    Query,
    UploadFile,
    File,
    BackgroundTasks,
)
from fastapi.responses import RedirectResponse, JSONResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import distinct, func, or_, select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.db import (  # noqa: E402
    AlertSent,
    Delivery,
    ResumeProfile,
    PollRun,
    SourceRun,
    utcnow,
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

DEMO = os.environ.get("DEMO_MODE") == "1"
app = FastAPI(title="Internship Radar")
demo_app = FastAPI(title="Internship Radar Demo")


@app.middleware("http")
@demo_app.middleware("http")
async def access_control(request, call_next):
    # The mounted demo has its own auth boundary and database dependency.
    if request.app is app and (
        request.url.path == "/demo" or request.url.path.startswith("/demo/")
    ):
        return await call_next(request)
    request.state.demo = DEMO or request.app is demo_app
    route_path = (
        request.scope["path"].removeprefix(request.scope.get("root_path", "")) or "/"
    )
    request.state.route_path = route_path
    if route_path not in ("/healthz", "/integrations/mail") and not route_path.startswith("/static/"):
        if request.state.demo:
            if request.method not in ("GET", "HEAD"):
                return JSONResponse(
                    {"detail": "Demo changes stay on your device."}, status_code=403
                )
        else:
            password = os.environ.get("ADMIN_PASSWORD")
            if password:
                try:
                    scheme, value = request.headers.get("authorization", "").split(
                        " ", 1
                    )
                    supplied = (
                        base64.b64decode(value).decode().split(":", 1)[1]
                        if scheme.lower() == "basic"
                        else ""
                    )
                except (ValueError, IndexError, UnicodeError):
                    supplied = ""
                if not hmac.compare_digest(supplied, password):
                    return Response(
                        status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="Internship Radar"'},
                    )
            elif request.url.hostname not in (
                "localhost",
                "127.0.0.1",
                "testserver",
            ) or request.client.host not in ("127.0.0.1", "::1", "testclient"):
                return JSONResponse(
                    {
                        "detail": "Set ADMIN_PASSWORD before sharing the private dashboard."
                    },
                    status_code=403,
                )
            if request.method == "POST":
                from urllib.parse import urlparse

                origin = request.headers.get("origin")
                if origin and urlparse(origin).netloc != request.url.netloc:
                    return Response(status_code=403)
    response = await call_next(request)
    response.headers["X-Radar-Mode"] = "demo" if request.state.demo else "private"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Cache-Control"] = (
        "no-store" if not route_path.startswith("/static/") else "public, max-age=3600"
    )
    return response


app.mount(
    "/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static"
)
templates = Jinja2Templates(
    directory=os.path.join(BASE_DIR, "templates"),
    context_processors=[
        lambda request: {
            "demo": request.state.demo,
            "base_path": request.scope.get("root_path", ""),
            "route_path": request.state.route_path,
        }
    ],
)

# Demo samples are isolated even when this process also serves a live database.
from scripts.demo import seed

demo_engine = init_db(get_engine("sqlite:///demo.db"))
seed(demo_engine)
DemoSessionFactory = get_session_factory(demo_engine)
engine = demo_engine if DEMO else init_db(get_engine())
SessionFactory = get_session_factory(engine)

PAGE_SIZE = 20
RANKING_LIMIT = 100
TARGET_ONLY = True


def get_db(request: Request):
    factory = DemoSessionFactory if request.state.demo else SessionFactory
    session = factory()
    try:
        yield session
    finally:
        session.close()


def _split_csv(value: str):
    return [part.strip() for part in (value or "").split(",") if part.strip()]


templates.env.globals["sector_labels"] = sector_labels()
templates.env.globals["demo"] = DEMO
templates.env.globals["statuses"] = [
    "new",
    "interested",
    "applied",
    "interview",
    "offer",
    "rejected",
]
templates.env.globals["brand"] = "Internship Radar"
templates.env.filters["unpack"] = unpack_list


def build_posting_query(params):
    """Translate query params into a filtered, ordered posting query."""
    query = select(Posting)
    if params.get("availability") != "all":
        query = query.where(Posting.closed_at.is_(None))
    if params.get("status"):
        query = query.where(Posting.status == params.get("status"))

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
        cutoff = utcnow() - timedelta(days=int(days))
        query = query.where(Posting.first_seen_at >= cutoff)

    return query.order_by(Posting.first_seen_at.desc(), Posting.id.desc())


def _feed_context(request: Request, db, page: int):
    params = request.query_params
    query = build_posting_query(params)
    if TARGET_ONLY and not request.state.demo:
        query = query.where(Posting.target_eligible.is_(True))
        if not params.get("status"):
            query = query.where(
                Posting.applied_at.is_(None), Posting.status.in_(["new", "interested"])
            )

    from shared.matching import rank

    profiles = list(db.scalars(select(ResumeProfile).order_by(ResumeProfile.id)))
    selected = params.get("profile", "")
    profile = next(
        (p for p in profiles if str(p.id) == selected),
        profiles[0] if profiles else None,
    )
    if profile:
        if profile.remote_only:
            query = query.where(Posting.remote.is_(True))
        locations = unpack_list(profile.locations)
        if locations:
            query = query.where(
                or_(
                    Posting.remote.is_(True),
                    *[
                        func.lower(func.coalesce(Posting.location, "")).contains(
                            x, autoescape=True
                        )
                        for x in locations
                    ],
                )
            )
        if profile.term:
            query = query.where(
                func.lower(func.coalesce(Posting.term, "")).contains(
                    profile.term.lower(), autoescape=True
                )
            )
        for term in unpack_list(profile.exclusions):
            content = func.lower(
                Posting.title + "\n" + func.coalesce(Posting.description, "")
            )
            query = query.where(~content.contains(term, autoescape=True))
    available = db.scalar(
        select(func.count()).select_from(query.order_by(None).subquery())
    )
    method = params.get("sort", "hybrid")
    if method not in {"hybrid", "weighted", "keywords", "newest"}:
        method = "hybrid"
    scores, ranking = {}, "chronological"
    if profile and method != "newest":
        # Bound CPU work and database transfer on free hosting. Newest-first
        # pagination still reaches every result, with the same profile filters.
        all_rows = list(db.scalars(query.limit(RANKING_LIMIT)))
        all_rows, scores, ranking = rank(db, all_rows, profile, method)
        total = len(all_rows)
    else:
        total = available
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(1, page), pages)
    postings = (
        all_rows[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
        if profile and method != "newest"
        else list(db.scalars(query.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)))
    )
    sources = list(
        db.scalars(select(distinct(Posting.source)).order_by(Posting.source))
    )
    return {
        "request": request,
        "postings": postings,
        "total": total,
        "available": available,
        "ranking_limited": available > total,
        "newest_url": str(request.url.include_query_params(sort="newest", page=1)),
        "page": page,
        "pages": pages,
        "sources": sources,
        "selected_sectors": params.getlist("sector"),
        "params": params,
        "query_string": str(request.url.query),
        "profile": profile,
        "profiles": profiles,
        "scores": scores,
        "ranking": ranking,
        "sort": method,
        "next_url": str(request.url.include_query_params(page=page + 1)),
        "prev_url": str(request.url.include_query_params(page=page - 1)),
    }


@app.get("/")
def feed(request: Request, page: int = Query(1, ge=1), db=Depends(get_db)):
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
            .where(Posting.first_seen_at >= utcnow() - timedelta(days=1))
        ),
    }
    return templates.TemplateResponse(request, "feed.html", context)


@app.get("/partials/postings")
def postings_partial(request: Request, page: int = Query(1, ge=1), db=Depends(get_db)):
    """HTMX target - returns just the results list so filtering feels instant."""
    return templates.TemplateResponse(
        request, "_postings.html", _feed_context(request, db, page)
    )


@app.get("/filters")
def filters_page(request: Request, db=Depends(get_db)):
    rows = db.execute(select(Filter).order_by(Filter.id)).scalars().all()
    deliveries = db.execute(
        select(Delivery, Posting.title, Filter.name)
        .join(Posting, Posting.id == Delivery.posting_id)
        .join(Filter, Filter.id == Delivery.filter_id)
        .order_by(Delivery.created_at.desc(), Delivery.id.desc())
        .limit(30)
    ).all()
    last_run = db.scalar(select(PollRun).order_by(PollRun.started_at.desc()))
    return templates.TemplateResponse(
        request,
        "filters.html",
        {
            "filters": rows,
            "deliveries": deliveries,
            "last_run": last_run,
        },
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
        db.query(Delivery).filter(Delivery.filter_id == filter_id).delete()
        db.delete(row)
        db.commit()
    return RedirectResponse("/filters", status_code=303)


@app.get("/companies")
def companies_page(
    request: Request, q: str = "", page: int = Query(1, ge=1), db=Depends(get_db)
):
    query = select(Company)
    if q.strip():
        query = query.where(func.lower(Company.name).like(f"%{q.strip().lower()}%"))
    query = query.order_by(
        Company.priority.desc(), Company.resolved.desc(), Company.name
    )

    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = (
        db.execute(query.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE))
        .scalars()
        .all()
    )

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


# Old feed links contain ?profile= when no matching profile has been created.
# Treat that as the default, while continuing to reject nonnumeric profile IDs.
@app.get("/jobs/{posting_id}")
def job_detail(request: Request, posting_id: int, profile: int | Literal[""] = 0, db=Depends(get_db)):
    from shared.matching import explain, rank

    p = db.get(Posting, posting_id)
    if not p:
        raise HTTPException(404)
    selected = (
        db.get(ResumeProfile, profile)
        if profile
        else db.scalar(select(ResumeProfile).order_by(ResumeProfile.id))
    )
    explanation = explain(db, selected, p) if selected else None
    return templates.TemplateResponse(
        request, "job.html", {"p": p, "profile": selected, "explanation": explanation}
    )


@app.post("/jobs/{posting_id}/status")
def update_status(
    posting_id: int,
    background_tasks: BackgroundTasks,
    status: str = Form(...),
    notes: str | None = Form(None),
    db=Depends(get_db),
):
    if status not in templates.env.globals["statuses"] or (
        notes is not None and len(notes) > 5000
    ):
        raise HTTPException(422, "Invalid status or notes too long")
    p = db.get(Posting, posting_id)
    if not p:
        raise HTTPException(404)
    # A manual applied/stage update must stop an in-flight preparation task.
    from shared.db import ApplicationTask
    task = db.scalar(select(ApplicationTask).where(ApplicationTask.posting_id == p.id).with_for_update())
    if task and task.state == "submitting":
        raise HTTPException(409, "Submission is in progress. Refresh its status before changing this role.")
    if task and status in ("applied", "interview", "offer", "rejected"):
        task.state = "submitted"
        task.submitted_at = task.submitted_at or utcnow()
        task.confirmation = "Owner recorded application progress manually."
        task.detail = "Tracked as applied by the owner."
        task.updated_at = utcnow()
    p.status = status
    p.status_updated_at = utcnow()
    if status == "applied" and p.applied_at is None:
        p.applied_at = p.status_updated_at
    if notes is not None:
        p.notes = notes
    from shared.application_sheet import update_workbook
    from shared.google_sheet import enqueue, sync_pending

    db.flush()
    update_workbook(db)
    enqueue(db, p)
    db.commit()
    background_tasks.add_task(sync_pending, db.get_bind())
    return RedirectResponse(f"/jobs/{posting_id}", 303)


@app.get("/applications.xlsx")
def application_spreadsheet(request: Request, db=Depends(get_db)):
    if request.state.demo:
        raise HTTPException(
            403, "The application workbook belongs to the private workspace."
        )
    from shared.application_sheet import workbook_bytes

    return Response(
        workbook_bytes(db),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": 'attachment; filename="internship-applications.xlsx"'
        },
    )


@app.get("/applications")
def applications(request: Request, background_tasks: BackgroundTasks, db=Depends(get_db)):
    rows = list(
        db.scalars(
            select(Posting)
            .where(Posting.status != "new")
            .order_by(Posting.first_seen_at.desc())
        )
    )
    if request.state.demo:
        rows = list(db.scalars(select(Posting).order_by(Posting.first_seen_at.desc())))
    sheet_status = None
    if not request.state.demo:
        from shared.google_sheet import enqueue, status, sync_pending
        from shared.db import ApplicationSync
        # Include applications saved before the Google connection was installed.
        for p in rows:
            if db.get(ApplicationSync, p.id) is None:
                enqueue(db, p)
        db.commit()
        sheet_status = status(db)
        if sheet_status["pending"]:
            background_tasks.add_task(sync_pending, db.get_bind())
    return templates.TemplateResponse(request, "applications.html", {"postings": rows, "sheet_sync": sheet_status})


@app.post("/companies/{company_id}/priority")
def company_priority(company_id: int, db=Depends(get_db)):
    c = db.get(Company, company_id)
    if not c:
        raise HTTPException(404)
    c.priority = not c.priority
    db.commit()
    return RedirectResponse("/companies", 303)


@app.get("/profile")
def profile_page(request: Request, profile: int | Literal[""] = 0, db=Depends(get_db)):
    profiles = list(db.scalars(select(ResumeProfile).order_by(ResumeProfile.id)))
    selected = next(
        (p for p in profiles if p.id == profile), profiles[0] if profiles else None
    )
    return templates.TemplateResponse(
        request, "profile.html", {"profile": selected, "profiles": profiles}
    )


@app.post("/profile")
def save_profile(
    name: str = Form(...),
    resume_text: str = Form(""),
    preferences: str = Form(""),
    locations: str = Form(""),
    term: str = Form(""),
    exclusions: str = Form(""),
    remote_only: bool = Form(False),
    resume: UploadFile | None = File(None),
    db=Depends(get_db),
):
    if resume and resume.filename:
        content = resume.file.read(2_000_001)
        if len(content) > 2_000_000:
            raise HTTPException(413, "Resume must be under 2 MB")
        try:
            if resume.filename.lower().endswith(".pdf"):
                from pypdf import PdfReader
                from io import BytesIO

                reader = PdfReader(BytesIO(content))
                if len(reader.pages) > 10:
                    raise ValueError("Too many pages")
                resume_text = "\n".join(
                    page.extract_text() or "" for page in reader.pages
                )
            elif resume.filename.lower().endswith(".txt"):
                resume_text = content.decode("utf-8")
            else:
                raise ValueError("Use PDF or plain text")
        except Exception:
            raise HTTPException(
                422, "Could not read resume. Use a text PDF or paste the text."
            )
    if not resume_text.strip() or len(resume_text) > 30000 or len(preferences) > 3000:
        raise HTTPException(422, "Resume text is required (maximum 30,000 characters).")
    row = ResumeProfile(
        name=name.strip()[:120] or "My profile",
        resume_text=resume_text,
        preferences=preferences,
        locations=pack_list(_split_csv(locations)),
        term=term[:120],
        exclusions=pack_list(_split_csv(exclusions)),
        remote_only=remote_only,
    )
    db.add(row)
    db.commit()
    return RedirectResponse(f"/?profile={row.id}", 303)


@app.get("/system")
def system_page(request: Request, db=Depends(get_db)):
    runs = list(
        db.scalars(select(PollRun).order_by(PollRun.started_at.desc()).limit(20))
    )
    observations = list(
        db.scalars(select(SourceRun).order_by(SourceRun.checked_at.desc()).limit(500))
    )
    latest = {}
    for r in observations:
        latest.setdefault(r.source, r)
    pending = db.scalar(
        select(func.count()).select_from(Delivery).where(Delivery.state == "pending")
    )
    return templates.TemplateResponse(
        request,
        "system.html",
        {"runs": runs, "sources": list(latest.values()), "pending": pending},
    )


@app.get("/about")
def about(request: Request):
    return templates.TemplateResponse(request, "about.html", {})


@app.get("/manifest.webmanifest")
def manifest(request: Request):
    prefix = request.scope.get("root_path", "")
    return JSONResponse(
        {
            "name": "Internship Radar",
            "short_name": "Radar",
            "start_url": prefix + "/",
            "scope": prefix + "/",
            "display": "standalone",
            "background_color": "#f5f6f8",
            "theme_color": "#102e30",
            "icons": [
                {
                    "src": prefix + "/static/icon.svg",
                    "sizes": "any",
                    "type": "image/svg+xml",
                    "purpose": "any",
                }
            ],
        }
    )


@app.get("/sw.js")
def service_worker(request: Request):
    if not request.state.demo:
        raise HTTPException(404)
    return FileResponse(
        os.path.join(BASE_DIR, "static", "sw.js"),
        media_type="application/javascript",
        headers={
            "Service-Worker-Allowed": request.scope.get("root_path", "") + "/",
            "Cache-Control": "no-cache",
        },
    )


@app.get("/offline")
def offline_page(request: Request):
    if not request.state.demo:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "offline.html", {})


# Reuse route handlers, but keep the mounted app's session/auth state separate.
demo_app.include_router(app.router)
demo_app.mount(
    "/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static"
)
from backend.apply_routes import register as register_application_routes
register_application_routes(app, templates, get_db)
app.mount("/demo", demo_app)
