"""Owner controls and a narrowly scoped, revocable local-worker connection."""
import base64
import hashlib
import hmac
import json
import secrets
from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks, Query
from fastapi.responses import RedirectResponse, Response, JSONResponse
from sqlalchemy import select, func
from shared.db import AutoApplication, AutoApplySettings, StoredResume, Posting, ApplicantSettings, utcnow
from shared import auto_apply as queue
from shared.auto_input import question_view
from shared.employer_questions import greenhouse_questions_url, schema_questions, refresh_questions, question_key, SOURCE_KEY, discovery_answer
from shared.profile_answers import FIELDS as SHARED_FIELDS, PREFIX, profile_values, combined_answers
from shared.answer_inbox import task_view as shared_task_view, inbox
from shared.applying import resume_profile_links, validate_profile

PHASES = {'opening':'Opening employer application', 'checking':'Checking requirements',
          'filling':'Filling saved details and attaching your resume', 'advancing':'Moving to the next step',
          'ready':'Ready for final submission', 'submitting':'Sending application',
          'confirmation':'Waiting for employer confirmation'}


def register(owner_app, templates, get_db):
    def private(request: Request):
        if request.state.demo:
            raise HTTPException(404)
    router = APIRouter(dependencies=[Depends(private)])

    def current_profile(db):
        row=db.get(ApplicantSettings,1)
        profile=json.loads(row.data) if row else {}
        if not profile.get('github') or not profile.get('linkedin'):
            for resume in db.scalars(select(StoredResume).where(StoredResume.active.is_(True))):
                for key,value in resume_profile_links(resume.content).items():
                    if not profile.get(key): profile[key]=value
        return profile

    def view_for(db,task,cfg=None):
        cfg=cfg or queue.settings(db)
        return shared_task_view(task,current_profile(db),json.loads(cfg.answers))

    def inbox_data(db,cfg):
        tasks=list(db.scalars(select(AutoApplication).where(AutoApplication.state=='needs_input',AutoApplication.submission_started_at.is_(None))))
        postings={p.id:p for p in db.scalars(select(Posting).where(Posting.id.in_([t.posting_id for t in tasks])))}
        profile=current_profile(db)
        return inbox(tasks,postings,profile,json.loads(cfg.answers)),tasks,profile

    @router.get('/autopilot/answers')
    def answer_inbox(request:Request,db=Depends(get_db)):
        cfg=queue.settings(db);data,tasks,profile=inbox_data(db,cfg);db.commit()
        return templates.TemplateResponse(request,'answer_inbox.html',{
            'inbox':data,'profile_fields':SHARED_FIELDS,'values':profile_values(profile,json.loads(cfg.answers)),
            'saved':request.query_params.get('saved'),'queued':request.query_params.get('queued','0')})

    @router.post('/autopilot/answers')
    async def save_inbox(request:Request,db=Depends(get_db)):
        cfg=queue.settings(db);data,tasks,profile=inbox_data(db,cfg);form=await request.form()
        shared=json.loads(cfg.answers)
        for key in SHARED_FIELDS:
            value=str(form.get('profile_'+key,'')).strip()
            if len(value)>300: raise HTTPException(422,'Keep profile fields under 300 characters.')
            if not value: continue
            if key in ('github','linkedin') and not value.startswith('https://'):
                raise HTTPException(422,'Profile links must start with https://.')
            if key=='gpa':
                try: valid=0<=float(value)<=4
                except ValueError: valid=False
                if not valid: raise HTTPException(422,'Enter a GPA from 0 to 4.')
            shared[PREFIX+key]=value
            if key in ('first_name','last_name','email','phone','github','linkedin'): profile[key]=value
        for group in data['groups']:
            value=str(form.get('q_'+group['id'],'')).strip()
            if not value: continue
            if len(value)>1500 or (group['options'] and value not in group['options']):
                raise HTTPException(422,'Choose an exact employer option and keep answers under 1,500 characters.')
            if group['reusable']:
                for entry in group['entries']: shared[entry['label']]=value
        if len(shared)>1000: raise HTTPException(422,'The saved-answer library is full.')
        cfg.answers=json.dumps(shared)
        row=db.get(ApplicantSettings,1)
        if row:
            stored=json.loads(row.data)
            stored.update({k:profile[k] for k in ('first_name','last_name','email','phone','github','linkedin') if profile.get(k)})
            try: row.data=json.dumps(validate_profile(stored))
            except ValueError as e: raise HTTPException(422,str(e)) from None
        queued=0
        for task in tasks:
            task.answers=json.dumps(combined_answers(json.loads(task.answers),shared))
            # Apply the answers explicitly supplied to every matching waiting task.
            updates={entry['label']:str(form['q_'+group['id']]).strip() for group in data['groups']
                     if str(form.get('q_'+group['id'],'')).strip() for entry in group['entries'] if entry['task_id']==task.id}
            task.answers=json.dumps({**json.loads(task.answers),**updates})
            view=shared_task_view(task,profile,shared)
            if (view['resolved'] or view['known_issues']) and not view['fields']:
                task.answers=json.dumps({**json.loads(task.answers),**view['resolved'],**view['known_issues']})
                task.state='queued';task.questions='[]';task.claim_token=None;task.updated_at=utcnow()
                task.detail='Shared answers saved. Waiting for your Mac.';queued+=1
        db.commit()
        return RedirectResponse(f'/autopilot/answers?saved=1&queued={queued}',303)

    @router.get('/autopilot/activity')
    def activity(question_limit: int = Query(20, ge=1, le=1000), db=Depends(get_db)):
        cfg=db.get(AutoApplySettings,1) or queue.settings(db)
        profile=current_profile(db);shared=json.loads(cfg.answers)
        now=utcnow()
        counts=dict(db.execute(select(AutoApplication.state,func.count()).group_by(AutoApplication.state)).all())
        active=db.scalar(select(AutoApplication).where(AutoApplication.state.in_(['running','submitting'])).order_by(AutoApplication.claimed_at.desc()).limit(1))
        recent=list(db.scalars(select(AutoApplication).where(AutoApplication.state.not_in(['queued','waiting_link'])).order_by(AutoApplication.updated_at.desc(),AutoApplication.id.desc()).limit(10)))
        attention=[]; answer_ids=[]; manual_count=0
        for task, posting in db.execute(select(AutoApplication, Posting).join(Posting, Posting.id==AutoApplication.posting_id).where(
                AutoApplication.state=='needs_input', AutoApplication.submission_started_at.is_(None)
            ).order_by(AutoApplication.updated_at.desc(),AutoApplication.id.desc())):
            view=shared_task_view(task,profile,shared)
            if not view['fields']:
                manual_count+=1
                continue
            answer_ids.append(task.id)
            if len(attention)<question_limit:
                saved={question_key(k):v for k,v in json.loads(task.answers).items()}
                attention.append({'id':task.id,'company':posting.company_name,'title':posting.title,
                                  'fields':view['fields'],'blockers':view['blockers'],'version':view['version'],
                                  'can_refresh':bool(greenhouse_questions_url(task.target_url)),
                                  'values':[saved.get(question_key(f['label']),'') for f in view['fields']]})
        def item(task):
            if not task: return None
            posting=db.get(Posting,task.posting_id)
            view=shared_task_view(task,profile,shared)
            return {'id':task.id,'posting_id':task.posting_id,'company':posting.company_name,
                    'title':posting.title,'state':task.state,
                    'detail': ('Answer the employer questions below.' if view['fields'] else 'Employer-site issue; no answer to enter here.') if task.state=='needs_input' else task.detail,
                    'has_questions':bool(view['fields']),
                    'status_label':view['label'],'action_label':view['action'],
                    'task_url':f'/autopilot/tasks/{task.id}',
                    'updated_at':task.updated_at.isoformat()+'Z'}
        result={'mode':cfg.mode,'connected':bool(cfg.last_seen_at and cfg.last_seen_at>now-timedelta(seconds=30)),
                'last_seen_at':cfg.last_seen_at.isoformat()+'Z' if cfg.last_seen_at else None,
                'counts':counts,'total':sum(counts.values()),'active':item(active),
                'recent':[item(task) for task in recent],'checked_at':now.isoformat()+'Z',
                'attention':attention,'answer_ids':answer_ids,'answer_count':len(answer_ids),'manual_count':manual_count}
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
            'task_views':{t.id:view_for(db,t,cfg) for t in tasks},
        })

    def task_page(request, db, task, error=None, values=None):
        view=view_for(db,task)
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
        inline='application/json' in request.headers.get('accept','')
        def invalid(message, values):
            if inline: return JSONResponse({'detail':message},status_code=422)
            return task_page(request,db,task,message,values)
        cfg=queue.settings(db)
        task=db.scalar(select(AutoApplication).where(AutoApplication.id==task_id).with_for_update())
        if not task or task.state!='needs_input' or task.submission_started_at:
            raise HTTPException(409,'This application is no longer waiting for answers. Reload its task page.')
        view=view_for(db,task,cfg); form=await request.form()
        if form.get('version')!=view['version']:
            raise HTTPException(409,'The questions changed. Reload this task page before answering.')
        values=[str(form.get(f'answer_{i}','')).strip() for i in range(len(view['fields']))]
        error=None
        if not view['fields'] and not view['resolved']: error='This is an employer-site issue. Use the manual review instructions below.'
        elif any(not value or len(value)>1500 for value in values): error='Answer each question using at most 1,500 characters.'
        elif any(field['options'] and value not in field['options'] for field,value in zip(view['fields'],values)):
            error='Choose one of the employer’s listed options for each dropdown.'
        if error:
            response=invalid(error,values);db.commit();return response
        updates={**view['resolved'],**{field['label']:value for field,value in zip(view['fields'],values)}}
        answers=json.loads(task.answers or '{}');answers.update(updates)
        task.answers=json.dumps(answers)
        if form.get('remember')=='yes':
            saved=json.loads(cfg.answers);saved.update(updates)
            if len(saved)>1000:
                db.rollback()
                return invalid('Your saved-answer library has reached 1,000 questions. Uncheck remember to answer only this application.',values)
            cfg.answers=json.dumps(saved)
        task.state='queued';task.claim_token=None;task.questions='[]';task.updated_at=utcnow()
        task.detail='Answers saved. Waiting for your Mac to retry this application.'
        db.commit()
        if inline:
            return {'id':task.id,'state':task.state,'message': 'Answers saved. Your connected Mac will continue this application from the queue.' if cfg.mode=='running' else 'Answers saved and queued. Use Start / Resume and connect your Mac to continue.'}
        return RedirectResponse(f'/autopilot/tasks/{task_id}',303)

    @router.post('/autopilot/tasks/{task_id}/refresh-questions')
    async def refresh_task_questions(task_id:int,request:Request,db=Depends(get_db)):
        import requests
        cfg=queue.settings(db)
        task=db.scalar(select(AutoApplication).where(AutoApplication.id==task_id).with_for_update())
        if not task or task.state!='needs_input' or task.submission_started_at:
            raise HTTPException(409,'Only unsubmitted applications waiting for input can refresh questions.')
        view=view_for(db,task,cfg);form=await request.form()
        if form.get('version')!=view['version']: raise HTTPException(409,'Questions changed. Reload before refreshing.')
        url=greenhouse_questions_url(task.target_url)
        if not url: raise HTTPException(422,'This employer does not provide supported question metadata.')
        try:
            response=requests.get(url,timeout=12,allow_redirects=False)
            response.raise_for_status();schema=schema_questions(response.json())
            if not schema: raise ValueError('No question metadata')
        except (requests.RequestException,ValueError):
            raise HTTPException(503,'Could not retrieve employer choices. Your draft is unchanged.') from None
        saved=json.loads(task.answers)
        for i,field in enumerate(view['fields']):
            value=str(form.get(f'answer_{i}','')).strip()
            if len(value)>1500: raise HTTPException(422,'Answers must be at most 1,500 characters.')
            if value: saved[field['label']]=value
        source=json.loads(cfg.answers).get(SOURCE_KEY)
        if source: saved[SOURCE_KEY]=source
        updated=refresh_questions(json.loads(task.questions),schema)
        for q in updated:
            if isinstance(q,dict):
                default=discovery_answer(q['label'],q['options'],saved)
                if default and not saved.get(q['label']): saved[q['label']]=default
        task.answers=json.dumps(saved);task.questions=json.dumps(updated)
        task.detail='Employer choices refreshed. Review your answers, then Save and continue.'
        task.updated_at=utcnow();db.commit()
        return {'message':'Choices refreshed and your draft saved. Review dropdown selections before continuing.'}

    @router.post('/autopilot/backfill')
    def backfill(db=Depends(get_db)):
        cfg = queue.settings(db)
        try:
            queue.replenish(db, cfg, include_existing=True)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        db.commit()
        return RedirectResponse('/autopilot', 303)

    @router.post('/autopilot/retry-attention')
    async def retry_attention(request:Request,db=Depends(get_db)):
        form=await request.form()
        cfg=queue.settings(db)
        try: queue.ready(db,cfg)
        except ValueError as e: raise HTTPException(422,str(e)) from None
        defaults=json.loads(cfg.answers)
        for task in db.scalars(select(AutoApplication).where(AutoApplication.state=='needs_input',AutoApplication.submission_started_at.is_(None)).with_for_update()):
            # Keep each task's resume, applicant snapshot, and specific answers.
            specific=json.loads(task.answers)
            task.answers=json.dumps({**specific,**defaults} if form.get('replace_saved')=='yes' else {**defaults,**specific})
            task.state='queued';task.questions='[]';task.claim_token=None;task.updated_at=utcnow()
            task.detail='Retry requested with saved answers and updated form handling.'
        db.commit()
        return RedirectResponse('/autopilot',303)

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
            if len(answers)>1000: raise ValueError('Use at most 1,000 saved answers.')
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
                    task.applicant=json.dumps(current_profile(db))
                    task.answers=json.dumps(combined_answers(json.loads(task.answers),json.loads(cfg.answers)))
                    result['task']={'id':task.id,'claim_token':task.claim_token,'url':task.target_url,
                        'title':p.title,'company':p.company_name,'profile':json.loads(task.applicant),
                        'answers':json.loads(task.answers),'resume':base64.b64encode(r.content).decode(),
                        'filename':r.filename}
                    source=json.loads(cfg.answers).get(SOURCE_KEY)
                    if source: result['task']['answers'][SOURCE_KEY]=source
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
