"""Owner controls and a narrowly scoped, revocable local-worker connection."""
import base64
import hashlib
import hmac
import json
import secrets
from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks, Query
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select, func
from shared.db import AutoApplication, StoredResume, Posting, utcnow
from shared import auto_apply as queue


def register(owner_app, templates, get_db):
    def private(request: Request):
        if request.state.demo:
            raise HTTPException(404)
    router = APIRouter(dependencies=[Depends(private)])

    @router.get('/autopilot')
    def page(request: Request, page: int = Query(1, ge=1), db=Depends(get_db)):
        cfg = queue.settings(db)
        db.commit()
        counts = dict(db.execute(select(AutoApplication.state, func.count()).group_by(AutoApplication.state)).all())
        total = sum(counts.values())
        pages = max(1, (total + 49) // 50)
        page = min(page, pages)
        tasks = list(db.scalars(select(AutoApplication).order_by(AutoApplication.updated_at.desc(), AutoApplication.id.desc()).offset((page-1)*50).limit(50)))
        return templates.TemplateResponse(request, 'autopilot.html', {
            'config':cfg, 'tasks':tasks,
            'resumes':list(db.scalars(select(StoredResume).where(StoredResume.active.is_(True)))),
            'postings':{t.posting_id:db.get(Posting,t.posting_id) for t in tasks},
            'saved_answers':json.loads(cfg.answers),
            'counts':counts, 'total_tasks':total, 'queue_page':page, 'queue_pages':pages,
        })

    @router.post('/autopilot/backfill')
    def backfill(db=Depends(get_db)):
        cfg = queue.settings(db)
        try:
            queue.replenish(db, cfg, include_existing=True)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        db.commit()
        return RedirectResponse('/autopilot', 303)

    @router.post('/autopilot/settings')
    async def save(request: Request, db=Depends(get_db)):
        form = await request.form(); cfg = queue.settings(db)
        if cfg.mode == 'running':
            raise HTTPException(409, 'Pause before changing resumes or answers.')
        try:
            for role in ('software','hardware','embedded'):
                rid = int(form.get(role, 0))
                resume = db.get(StoredResume, rid)
                if not resume or not resume.active:
                    raise ValueError('Select an uploaded resume for each role.')
                setattr(cfg, role+'_resume_id', rid)
            answers = {}
            for line in str(form.get('answers','')).splitlines():
                if not line.strip(): continue
                key, value = line.split('=',1)
                if not key.strip() or not value.strip() or len(key)>300 or len(value)>1500:
                    raise ValueError('Use Question = Answer, one per line.')
                answers[key.strip()] = value.strip()
            if len(answers)>100: raise ValueError('Use at most 100 saved answers.')
            cfg.answers = json.dumps(answers)
        except (ValueError, TypeError) as e:
            raise HTTPException(422,str(e)) from None
        db.commit(); return RedirectResponse('/autopilot',303)

    @router.post('/autopilot/control')
    async def control(request: Request, db=Depends(get_db)):
        form = await request.form(); cfg=queue.settings(db)
        try: queue.control(db,cfg,str(form.get('mode','')))
        except ValueError as e: raise HTTPException(422,str(e)) from None
        db.commit(); return RedirectResponse('/autopilot',303)

    @router.post('/autopilot/connect')
    def connect(db=Depends(get_db)):
        cfg=queue.settings(db); queue.control(db,cfg,'stopped')
        token=secrets.token_urlsafe(40)
        cfg.token_digest=hashlib.sha256(token.encode()).hexdigest()
        db.commit()
        return Response(json.dumps({'url':'https://internship-alerts-1412.onrender.com', 'token':token}),
            media_type='application/json',headers={'Content-Disposition':'attachment; filename="radar-worker.json"','Cache-Control':'no-store'})

    @router.post('/autopilot/tasks/{task_id}/retry')
    async def retry(task_id:int, request:Request, db=Depends(get_db)):
        queue.settings(db)
        t=db.get(AutoApplication,task_id)
        if not t or t.state!='needs_input' or t.submission_started_at:
            raise HTTPException(409,'Only unsubmitted applications needing input can resume.')
        cfg=queue.settings(db)
        t.answers=cfg.answers
        t.applicant=json.dumps(queue.ready(db,cfg))
        t.state='queued'; t.questions='[]'; t.claim_token=None
        db.commit(); return RedirectResponse('/autopilot',303)

    @router.post('/integrations/auto-apply')
    async def worker(request:Request, background_tasks:BackgroundTasks, db=Depends(get_db)):
        cfg=queue.settings(db)
        supplied=request.headers.get('authorization','').removeprefix('Bearer ')
        if not cfg.token_digest or not hmac.compare_digest(hashlib.sha256(supplied.encode()).hexdigest(),cfg.token_digest):
            raise HTTPException(401,'Worker connection not authorized.')
        body=bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body)>64000: raise HTTPException(413,'Request too large.')
        try:
            data=json.loads(body)
            if not isinstance(data,dict): raise ValueError()
            action=data.get('action'); cfg.last_seen_at=utcnow()
            result={'mode':cfg.mode}
            if action=='claim':
                task=queue.claim(db,cfg)
                result['task']=None
                if task:
                    p=db.get(Posting,task.posting_id); r=db.get(StoredResume,task.resume_id)
                    result['task']={'id':task.id,'claim_token':task.claim_token,'url':task.target_url,
                        'title':p.title,'company':p.company_name,'profile':json.loads(task.applicant),
                        'answers':json.loads(task.answers),'resume':base64.b64encode(r.content).decode(),
                        'filename':r.filename}
            elif action in ('heartbeat','permit','result'):
                task=db.get(AutoApplication,int(data.get('task_id',0)))
                if not task or not task.claim_token or not hmac.compare_digest(task.claim_token,str(data.get('claim_token',''))):
                    raise HTTPException(409,'Application lease expired.')
                if action=='result':
                    queue.receipt(db,task,data)
                    from shared.google_sheet import sync_pending
                    background_tasks.add_task(sync_pending,db.get_bind())
                else:
                    result['allowed']=queue.permit(db,cfg,task)
                    if action=='permit' and result['allowed']:
                        task.state='submitting'; task.submission_started_at=utcnow()
                    elif action=='heartbeat' and task.state=='submitting':
                        result['allowed']=cfg.mode=='running' and task.generation==cfg.generation
                        task.claimed_at=utcnow()
            elif action=='local_stop':
                queue.control(db,cfg,'stopped'); result['mode']='stopped'
            else: raise ValueError('Unknown worker action.')
        except (ValueError, TypeError, KeyError) as e:
            raise HTTPException(422,str(e) or 'Invalid worker request.') from None
        db.commit(); return result
    owner_app.include_router(router)
