"""Owner controls and a narrowly scoped, revocable local-worker connection."""
import base64
import hashlib
import hmac
import json
import secrets
from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks, Query
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select, func
from shared.db import AutoApplication, AutoApplySettings, StoredResume, Posting, utcnow
from shared import auto_apply as queue
from shared.auto_input import question_view

PHASES = {'opening':'Opening employer application', 'checking':'Checking requirements',
          'filling':'Filling saved details and attaching your resume', 'advancing':'Moving to the next step',
          'ready':'Ready for final submission', 'submitting':'Sending application',
          'confirmation':'Waiting for employer confirmation'}


def register(owner_app, templates, get_db):
    def private(request: Request):
        if request.state.demo:
            raise HTTPException(404)
    router = APIRouter(dependencies=[Depends(private)])

    @router.get('/autopilot/activity')
    def activity(db=Depends(get_db)):
        cfg=db.get(AutoApplySettings,1) or queue.settings(db)
        now=utcnow()
        counts=dict(db.execute(select(AutoApplication.state,func.count()).group_by(AutoApplication.state)).all())
        active=db.scalar(select(AutoApplication).where(AutoApplication.state.in_(['running','submitting'])).order_by(AutoApplication.claimed_at.desc()).limit(1))
        recent=list(db.scalars(select(AutoApplication).where(AutoApplication.state.not_in(['queued','waiting_link'])).order_by(AutoApplication.updated_at.desc(),AutoApplication.id.desc()).limit(10)))
        def item(task):
            if not task: return None
            posting=db.get(Posting,task.posting_id)
            view=question_view(task)
            return {'id':task.id,'posting_id':task.posting_id,'company':posting.company_name,
                    'title':posting.title,'state':task.state,'detail':task.detail,
                    'status_label':view['label'],'action_label':view['action'],
                    'task_url':f'/autopilot/tasks/{task.id}',
                    'updated_at':task.updated_at.isoformat()+'Z'}
        result={'mode':cfg.mode,'connected':bool(cfg.last_seen_at and cfg.last_seen_at>now-timedelta(seconds=30)),
                'last_seen_at':cfg.last_seen_at.isoformat()+'Z' if cfg.last_seen_at else None,
                'counts':counts,'total':sum(counts.values()),'active':item(active),
                'recent':[item(task) for task in recent],'checked_at':now.isoformat()+'Z'}
        db.commit()
        return result

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
            'task_views':{t.id:question_view(t) for t in tasks},
        })

    def task_page(request, db, task, error=None, values=None):
        view=question_view(task)
        answers=json.loads(task.answers or '{}')
        return templates.TemplateResponse(request,'auto_task.html',{
            'task':task,'posting':db.get(Posting,task.posting_id),'view':view,
            'values':values if values is not None else [answers.get(f['label'],'') for f in view['fields']],
            'error':error,'config':queue.settings(db),
        },status_code=422 if error else 200)

    @router.get('/autopilot/tasks/{task_id}')
    def task_detail(task_id:int, request:Request, db=Depends(get_db)):
        task=db.get(AutoApplication,task_id)
        if not task: raise HTTPException(404,'Application task not found.')
        response=task_page(request,db,task)
        db.commit()
        return response

    @router.post('/autopilot/tasks/{task_id}/answers')
    async def task_answers(task_id:int,request:Request,db=Depends(get_db)):
        cfg=queue.settings(db)
        task=db.scalar(select(AutoApplication).where(AutoApplication.id==task_id).with_for_update())
        if not task or task.state!='needs_input' or task.submission_started_at:
            raise HTTPException(409,'This application is no longer waiting for answers. Reload its task page.')
        view=question_view(task); form=await request.form()
        if form.get('version')!=view['version']:
            raise HTTPException(409,'The questions changed. Reload this task page before answering.')
        values=[str(form.get(f'answer_{i}','')).strip() for i in range(len(view['fields']))]
        error=None
        if not view['fields']: error='This is an employer-site issue. Use the manual review instructions below.'
        elif any(not value or len(value)>1500 for value in values): error='Answer each question using at most 1,500 characters.'
        elif any(field['options'] and value not in field['options'] for field,value in zip(view['fields'],values)):
            error='Choose one of the employer’s listed options for each dropdown.'
        if error:
            response=task_page(request,db,task,error,values);db.commit();return response
        updates={field['label']:value for field,value in zip(view['fields'],values)}
        answers=json.loads(task.answers or '{}');answers.update(updates)
        task.answers=json.dumps(answers)
        if form.get('remember')=='yes':
            saved=json.loads(cfg.answers);saved.update(updates)
            if len(saved)>100:
                db.rollback()
                return task_page(request,db,task,'Your saved-answer library has reached 100 questions. Uncheck remember to answer only this application.',values)
            cfg.answers=json.dumps(saved)
        task.state='queued';task.claim_token=None;task.questions='[]';task.updated_at=utcnow()
        task.detail='Answers saved. Waiting for your Mac to retry this application.'
        db.commit()
        return RedirectResponse(f'/autopilot/tasks/{task_id}',303)

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
        t.detail='Retry requested. Waiting for your Mac.';t.updated_at=utcnow()
        db.commit(); return RedirectResponse(f'/autopilot/tasks/{task_id}',303)

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
                    if action=='heartbeat' and result['allowed'] and data.get('phase') in PHASES:
                        detail=PHASES[data['phase']]
                        step=data.get('step')
                        if isinstance(step,int) and 1<=step<=10:
                            detail+=f' · Step {step}'
                        if detail!=task.detail:
                            task.detail=detail
                            task.updated_at=utcnow()
            elif action=='local_stop':
                queue.control(db,cfg,'stopped'); result['mode']='stopped'
            else: raise ValueError('Unknown worker action.')
        except (ValueError, TypeError, KeyError) as e:
            raise HTTPException(422,str(e) or 'Invalid worker request.') from None
        db.commit(); return result
    owner_app.include_router(router)
