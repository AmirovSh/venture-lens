from contextlib import asynccontextmanager
import time
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, update
from . import settings
from .db import init, Session, Run, Model, Job, Event, Invocation, uid, dump, read
from .schemas import ResearchInput, Decision

@asynccontextmanager
async def lifespan(app):
    init();yield

app=FastAPI(title='Venture Lens',version='0.1.0',lifespan=lifespan)

@app.middleware('http')
async def local_guard(request:Request,call_next):
    # Local prototype: same-origin browser requests only; bind services to loopback.
    origin=request.headers.get('origin')
    if request.method not in ('GET','HEAD','OPTIONS') and origin and origin not in ('http://127.0.0.1:3100','http://localhost:3100'):
        return JSONResponse({'detail':'Недопустимый Origin'},status_code=403)
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Cache-Control']='no-store'
    return response

def get_run(s,id):
    r=s.get(Run,id)
    if not r:raise HTTPException(404,'Исследование не найдено')
    return r

def serialize_model(m,detail=True):
    result={k:getattr(m,k) for k in ['id','run_id','name','company','status','stage','score','score_upper','coverage','decision','note','error']}
    if detail:result['data']=read(m.data)
    return result

def serialize_run(s,r,detail=False):
    models=s.scalars(select(Model).where(Model.run_id==r.id)).all()
    counts={k:sum(m.status==k for m in models) for k in ['passed','rejected','review','error','queued','running']}
    conf=read(r.settings)
    result={'id':r.id,'title':r.title,'status':r.status,'created':r.created,'updated':r.updated,'settings':{k:v for k,v in conf.items() if k!='discovery_sources'},'calls':r.calls,'tokens':r.tokens,'error':r.error,'counts':counts,'total':len(models),'done':sum(counts[k] for k in ['passed','rejected','review','error'])}
    if detail:
        result['models']=[serialize_model(m) for m in models];result['policy']=read(r.policy)
        result['discovery_sources']=conf.get('discovery_sources',[])
    return result

@app.get('/api/health')
def health():
    return {'status':'ok','llm_configured':bool(settings.BASE_URL and settings.API_KEY),'llm_model':settings.MODEL,'database':'sqlite' if settings.DATABASE_URL.startswith('sqlite') else 'postgresql','search':'searxng' if settings.setting('SEARXNG_URL') else 'exa' if settings.setting('EXA_API_KEY') else 'public_search','policy':settings.policy()}

@app.get('/api/runs')
def runs():
    with Session() as s:return [serialize_run(s,r) for r in s.scalars(select(Run).order_by(Run.created.desc()).limit(100))]

@app.post('/api/runs',status_code=201)
def create_run(body:ResearchInput):
    if not settings.BASE_URL or not settings.API_KEY:raise HTTPException(503,'Настройте подключение к корпоративному LLM')
    with Session.begin() as s:
        if s.query(Run).filter(Run.status.in_(['queued','running'])).count()>=3:raise HTTPException(429,'Уже выполняются три исследования')
        r=Run(id=uid(),title=body.title,settings=dump(body.model_dump()),policy=dump(settings.policy()))
        s.add(r);s.flush();s.add(Job(run_id=r.id,key=f'discovery:{r.id}'))
        return {'id':r.id}

@app.get('/api/runs/{id}')
def run_detail(id:str):
    with Session() as s:return serialize_run(s,get_run(s,id),True)

@app.get('/api/runs/{id}/events')
def events(id:str,after:int=0):
    with Session() as s:
        get_run(s,id)
        rows=s.scalars(select(Event).where(Event.run_id==id,Event.id>after).order_by(Event.id).limit(500))
        return [{k:getattr(e,k) for k in ['id','at','stage','message','model_id']}|{'payload':read(e.payload)} for e in rows]

@app.get('/api/runs/{id}/audit')
def audit(id:str):
    with Session() as s:
        get_run(s,id)
        return [{k:getattr(x,k) for k in ['id','model_id','role','model','prompt_version','input_hash','tokens','status','created']}|{'input':read(x.input),'output':x.output} for x in s.scalars(select(Invocation).where(Invocation.run_id==id).order_by(Invocation.created))]

@app.post('/api/runs/{id}/cancel')
def cancel(id:str):
    with Session.begin() as s:
        r=get_run(s,id)
        if r.status in ('queued','running'):
            r.status='cancelled';r.updated=time.time()
            s.execute(update(Job).where(Job.run_id==id,Job.status=='queued').values(status='cancelled'))
    return {'status':'cancelled'}

@app.post('/api/runs/{id}/retry')
def retry(id:str):
    with Session.begin() as s:
        r=get_run(s,id)
        if r.status not in ('partial','cancelled'):raise HTTPException(409,'Повтор доступен для остановленного или частично завершённого исследования')
        failed=s.scalars(select(Job).where(Job.run_id==id,Job.status.in_(['failed','cancelled']))).all()
        if not failed:raise HTTPException(409,'Нет этапов для повтора; дождитесь завершения текущего запроса')
        r.status='queued';r.error='';r.updated=time.time()
        for j in failed:
            j.status='queued';j.lease_until=0
            if j.model_id:
                m=s.get(Model,j.model_id);m.status='queued';m.error=''
    return {'status':'queued'}

@app.patch('/api/models/{id}/decision')
def decision(id:str,body:Decision):
    with Session.begin() as s:
        m=s.get(Model,id)
        if not m:raise HTTPException(404,'Карточка не найдена')
        m.decision=body.decision;m.note=body.note
    return {'status':'saved'}

@app.get('/api/runs/{id}/export')
def export(id:str):
    with Session() as s:
        r=get_run(s,id);result=serialize_run(s,r,True)
    return JSONResponse(result,headers={'Content-Disposition':f'attachment; filename="venture-lens-{id}.json"'})
