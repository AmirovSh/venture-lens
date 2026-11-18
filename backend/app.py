from contextlib import asynccontextmanager
import time
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, update, text,func
from sqlalchemy.exc import SQLAlchemyError
from .execution import DependencyUnavailable
from . import settings
from .db import init, Session, Run, Model, Job, Event, Invocation, ProviderState,uid, dump, read
from .schemas import ResearchInput, Decision
from .auth_context import verify_identity

@asynccontextmanager
async def lifespan(app):
    init();yield

app=FastAPI(title='Venture Lens',version='0.1.0',lifespan=lifespan)

@app.middleware('http')
async def local_guard(request:Request,call_next):
    # Local prototype: same-origin browser requests only; bind services to loopback.
    origin=request.headers.get('origin')
    allowed=settings.setting('ALLOWED_ORIGINS','http://127.0.0.1:3100,http://localhost:3100').split(',')
    if request.method not in ('GET','HEAD','OPTIONS') and origin and origin not in allowed:
        return JSONResponse({'detail':'Недопустимый Origin'},status_code=403)
    secret = settings.setting('AUTH_CONTEXT_SECRET')
    required = settings.setting('AUTH_CONTEXT_REQUIRED') == 'true'
    if request.url.path not in ('/api/live', '/api/ready'):
        identity = verify_identity(request.headers, secret)
        if (required or request.headers.get('x-venture-identity') or request.headers.get('x-venture-signature')) and not identity:
            return JSONResponse({'detail':'Войдите в сервис'}, status_code=401)
        request.state.identity = identity or {'workspace':'main', 'role':'owner'}
        if request.method not in ('GET','HEAD','OPTIONS') and request.state.identity['role'] == 'viewer':
            return JSONResponse({'detail':'Гостевой доступ разрешает только просмотр'}, status_code=403)
    try:response=await call_next(request)
    except SQLAlchemyError:return JSONResponse({'detail':'База данных временно недоступна','code':'DATABASE_UNAVAILABLE'},status_code=503,headers={'Retry-After':'3'})
    except DependencyUnavailable:return JSONResponse({'detail':'Зависимость временно недоступна','code':'DEPENDENCY_UNAVAILABLE'},status_code=503,headers={'Retry-After':'30'})
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Cache-Control']='no-store'
    return response

def get_run(s,id,request):
    r=s.get(Run,id)
    if not r or r.workspace != request.state.identity['workspace']:raise HTTPException(404,'Исследование не найдено')
    return r


def lock_admission(s):
    if s.bind.dialect.name == 'postgresql':
        s.execute(text('SELECT pg_advisory_xact_lock(862319005)'))


def member_active_limit(s, request, excluding=None):
    if request.state.identity['role'] not in ('member','admin'):
        return
    query = select(Run).where(Run.workspace == request.state.identity['workspace'], Run.status.in_(['queued','running','waiting']))
    if excluding:
        query = query.where(Run.id != excluding)
    if s.scalar(query.limit(1)):
        raise HTTPException(429,'У вас уже есть активное исследование. Дождитесь завершения или остановите его.')

def serialize_model(m,detail=True):
    result={k:getattr(m,k) for k in ['id','run_id','name','company','status','stage','score','score_upper','coverage','decision','note','error']}
    if detail:result['data']=read(m.data)
    return result

def serialize_run(s,r,detail=False):
    models=s.scalars(select(Model).where(Model.run_id==r.id)).all()
    counts={k:sum(m.status==k for m in models) for k in ['passed','rejected','review','error','queued','running','waiting','cancelled']}
    conf=read(r.settings)
    result={'id':r.id,'title':r.title,'status':r.status,'created':r.created,'updated':r.updated,'settings':{k:v for k,v in conf.items() if k!='discovery_sources'},'calls':r.calls,'tokens':r.tokens,'error':r.error,'counts':counts,'total':len(models),'done':sum(counts[k] for k in ['passed','rejected','review','error'])}
    result.update(waiting_reason=r.waiting_reason,active_seconds=r.active_seconds,degraded=bool(r.degraded))
    if detail:
        result['models']=[serialize_model(m) for m in models];result['policy']=read(r.policy)
        result['discovery_sources']=conf.get('discovery_sources',[])
    return result

@app.get('/api/health')
def health():
    return {'status':'ok','llm_configured':bool(settings.BASE_URL and settings.API_KEY),'llm_model':settings.MODEL,'database':'sqlite' if settings.DATABASE_URL.startswith('sqlite') else 'postgresql','search':'searxng' if settings.setting('SEARXNG_URL') else 'exa' if settings.setting('EXA_API_KEY') else 'public_search','policy':settings.policy()}

@app.get('/api/live')
def live():return {'status':'alive'}

@app.get('/api/ready')
def ready():
    try:
        with Session() as session:
            session.execute(select(Run.generation,Run.active_seconds).limit(1));session.execute(select(Job.epoch).limit(1))
        return {'status':'ready'}
    except SQLAlchemyError:return JSONResponse({'status':'not_ready','reason':'DATABASE_OR_SCHEMA_UNAVAILABLE'},status_code=503)

@app.get('/api/dependencies')
def dependencies():
    with Session() as session:return [{'id':p.id,'status':'waiting' if p.blocked_until>time.time() else 'available','retry_at':p.blocked_until,'reason':p.last_error} for p in session.scalars(select(ProviderState))]

@app.get('/api/metrics')
def metrics(request:Request):
    from fastapi.responses import PlainTextResponse
    with Session() as session:
        counts=session.execute(select(Job.status,func.count()).join(Run, Job.run_id==Run.id).where(Run.workspace==request.state.identity['workspace']).group_by(Job.status)).all()
    return PlainTextResponse('\n'.join(f'venturelens_jobs{{status="{status}"}} {count}' for status,count in counts)+'\n')

@app.get('/api/sources/{checksum}')
def source_snapshot(checksum:str,request:Request):
    from fastapi.responses import PlainTextResponse
    from .storage import get_source,IntegrityError
    if len(checksum)!=64 or any(c not in '0123456789abcdef' for c in checksum):raise HTTPException(400,'Некорректная контрольная сумма')
    def contains(value):
        if isinstance(value,dict):return value.get('sha256')==checksum or any(contains(x) for x in value.values())
        return isinstance(value,list) and any(contains(x) for x in value)
    with Session() as s:
        workspace=request.state.identity['workspace']
        settings_rows=s.scalars(select(Run.settings).where(Run.workspace==workspace,Run.settings.contains(checksum)))
        model_rows=s.scalars(select(Model.data).join(Run,Model.run_id==Run.id).where(Run.workspace==workspace,Model.data.contains(checksum)))
        if not any(contains(read(value)) for value in settings_rows) and not any(contains(read(value)) for value in model_rows):
            raise HTTPException(404,'Источник не найден')
    try:return PlainTextResponse(get_source(checksum))
    except ValueError:raise HTTPException(400,'Некорректная контрольная сумма')
    except (OSError,IntegrityError):raise HTTPException(503,'Снимок источника недоступен или повреждён')

@app.get('/api/runs')
def runs(request:Request):
    with Session() as s:return [serialize_run(s,r) for r in s.scalars(select(Run).where(Run.workspace==request.state.identity['workspace']).order_by(Run.created.desc()).limit(100))]


@app.get('/api/account')
def account(request:Request):
    identity=request.state.identity
    if identity['role'] not in ('member','admin'):return {'role':identity['role'],'limits':None}
    with Session() as s:
        recent=s.scalars(select(Run.created).where(Run.workspace==identity['workspace'],Run.created>time.time()-86400).order_by(Run.created)).all()
        active=s.scalar(select(func.count()).select_from(Run).where(Run.workspace==identity['workspace'],Run.status.in_(['queued','running','waiting'])))
    return {'role':'member','limits':{'daily':3,'active':1,'used':len(recent),'remaining':max(0,3-len(recent)),
            'running':active,'next_slot_at':recent[0]+86400 if len(recent)>=3 else None}}

@app.post('/api/runs',status_code=201)
def create_run(body:ResearchInput,request:Request):
    if not settings.BASE_URL or not settings.API_KEY:raise HTTPException(503,'Настройте подключение к корпоративному LLM')
    with Session.begin() as s:
        lock_admission(s)
        member_active_limit(s,request)
        workspace=request.state.identity['workspace']
        if request.state.identity['role'] in ('member','admin'):
            used=s.scalar(select(func.count()).select_from(Run).where(Run.workspace==workspace,Run.created>time.time()-86400))
            if used>=3:raise HTTPException(429,'Лимит: три новых исследования за последние 24 часа. Попробуйте позже.')
        if s.query(Run).filter(Run.status.in_(['queued','running','waiting'])).count()>=int(settings.setting('MAX_ACTIVE_RESEARCH','10')):raise HTTPException(429,'Достигнут лимит одновременных исследований')
        r=Run(id=uid(),workspace=workspace,title=body.title,settings=dump(body.model_dump()),policy=dump(settings.policy()))
        s.add(r);s.flush();s.add(Job(run_id=r.id,key=f'discovery:{r.id}'))
        return {'id':r.id}

@app.get('/api/runs/{id}')
def run_detail(id:str,request:Request):
    with Session() as s:return serialize_run(s,get_run(s,id,request),True)

@app.get('/api/runs/{id}/events')
def events(id:str,request:Request,after:int=0):
    with Session() as s:
        get_run(s,id,request)
        rows=s.scalars(select(Event).where(Event.run_id==id,Event.id>after).order_by(Event.id).limit(500))
        return [{k:getattr(e,k) for k in ['id','at','stage','message','model_id']}|{'payload':read(e.payload)} for e in rows]

@app.get('/api/runs/{id}/audit')
def audit(id:str,request:Request):
    with Session() as s:
        get_run(s,id,request)
        return [{k:getattr(x,k) for k in ['id','model_id','role','model','prompt_version','input_hash','tokens','status','created']}|{'input':read(x.input),'output':x.output} for x in s.scalars(select(Invocation).where(Invocation.run_id==id).order_by(Invocation.created))]

@app.post('/api/runs/{id}/cancel')
def cancel(id:str,request:Request):
    with Session.begin() as s:
        query=select(Run).where(Run.id==id)
        if s.bind.dialect.name=='postgresql':query=query.with_for_update()
        r=s.scalar(query)
        if not r or r.workspace!=request.state.identity['workspace']:raise HTTPException(404,'Исследование не найдено')
        if r.status in ('queued','running','waiting'):
            r.status='cancelled';r.updated=time.time();r.generation+=1
            s.execute(update(Job).where(Job.run_id==id,Job.status.in_(['queued','running','waiting'])).values(status='cancelled',epoch=Job.epoch+1,lease_owner=None,lease_until=0))
            s.execute(update(Model).where(Model.run_id==id,Model.status.in_(['queued','running','waiting'])).values(status='cancelled'))
            s.execute(update(Invocation).where(Invocation.run_id==id,Invocation.status=='running').values(status='cancelled'))
    return {'status':'cancelled'}

@app.post('/api/runs/{id}/retry')
def retry(id:str,request:Request):
    with Session.begin() as s:
        lock_admission(s)
        query=select(Run).where(Run.id==id)
        if s.bind.dialect.name=='postgresql':query=query.with_for_update()
        r=s.scalar(query)
        if not r or r.workspace!=request.state.identity['workspace']:raise HTTPException(404,'Исследование не найдено')
        member_active_limit(s,request,excluding=id)
        if r.status not in ('partial','cancelled','waiting'):raise HTTPException(409,'Повтор доступен для остановленного или частично завершённого исследования')
        failed=s.scalars(select(Job).where(Job.run_id==id,Job.status.in_(['failed','cancelled','waiting']))).all()
        if not failed:raise HTTPException(409,'Нет этапов для повтора; дождитесь завершения текущего запроса')
        r.status='queued';r.error='';r.updated=time.time();r.waiting_reason=''
        for j in failed:
            j.status='queued';j.lease_until=0;j.lease_owner=None;j.epoch+=1;j.next_retry_at=0;j.attempts=0
            if j.model_id:
                m=s.get(Model,j.model_id);m.status='queued';m.error=''
    return {'status':'queued'}

@app.patch('/api/models/{id}/decision')
def decision(id:str,body:Decision,request:Request):
    with Session.begin() as s:
        m=s.get(Model,id)
        if not m:raise HTTPException(404,'Карточка не найдена')
        get_run(s,m.run_id,request)
        m.decision=body.decision;m.note=body.note
    return {'status':'saved'}

@app.get('/api/runs/{id}/export')
def export(id:str,request:Request):
    with Session() as s:
        r=get_run(s,id,request);result=serialize_run(s,r,True)
    return JSONResponse(result,headers={'Content-Disposition':f'attachment; filename="venture-lens-{id}.json"'})
