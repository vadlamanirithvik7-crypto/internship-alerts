"""Manual employer navigation and inert compatibility links for removed features."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from shared.db import Posting


def register(owner_app, templates, get_db):
    def private_only(request: Request):
        if request.state.demo:
            raise HTTPException(404)

    router = APIRouter(dependencies=[Depends(private_only)])

    @router.get('/jobs/{posting_id}/apply')
    def open_application(posting_id: int, db=Depends(get_db)):
        posting=db.get(Posting,posting_id)
        if not posting: raise HTTPException(404)
        from poller.application_links import application_url, repair_links
        if not application_url(posting):
            repair_links(db,posting_id=posting_id,limit=1)
            db.commit()
        return RedirectResponse(application_url(posting) or f'/jobs/{posting_id}',303)

    @router.get('/autopilot')
    @router.get('/autopilot/{rest:path}')
    @router.get('/apply-settings')
    @router.get('/mail-updates')
    def old_feature():
        return RedirectResponse('/',303)

    @router.get('/apply-tasks')
    @router.get('/apply-tasks/{rest:path}')
    def old_queue():
        return RedirectResponse('/applications',303)

    @router.post('/jobs/{posting_id}/apply')
    @router.post('/apply-tasks/{rest:path}')
    @router.post('/autopilot/{rest:path}')
    @router.post('/integrations/auto-apply')
    @router.post('/integrations/mail')
    @router.post('/apply-settings')
    @router.post('/resumes')
    @router.post('/mail-connection/{rest:path}')
    def removed_automation():
        raise HTTPException(410,'Automatic applications have been removed. Open the employer site and apply yourself.')

    owner_app.include_router(router)
