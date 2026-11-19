"""Administrator model controls and workspace-scoped source links."""
import time
import httpx
from typing import Literal
from fastapi import APIRouter,HTTPException,Request
from pydantic import BaseModel,Field
from sqlalchemy import select,text
from . import db,runtime_config,settings,providers
from .runtime_config import ModelSettingsInput,ConfigurationError
from .schemas import SourceLinksInput,Strict

router=APIRouter(prefix='/api')

def admin(request):
    identity=request.state.identity
    if identity['role'] not in ('owner','admin'):raise HTTPException(403,'Настройки моделей доступны администраторам')
    return identity

@router.get('/admin/model-settings')
def model_settings(request:Request):
    admin(request)
    result=runtime_config.public_view();result['default_prompts']=runtime_config.default_prompts()
    return result

@router.put('/admin/model-settings')
def save_model_settings(body:ModelSettingsInput,request:Request):
    who=admin(request)
    try:return runtime_config.save(body,who['role']+':'+who['workspace'])
    except ConfigurationError as error:raise HTTPException(409,str(error))

class Probe(BaseModel):
    role:Literal['discovery','screening','critic','verification_search','verifier','launch_brief']='discovery'

@router.post('/admin/model-settings/test')
def test_model_server(body:Probe,request:Request):
    admin(request)
    from .llm import routes,LLMError
    from .transport import completion,TransportError
    version,config=runtime_config.current();model=config['models'][body.role]
    try:
        if config['connection_mode']=='default':route=routes(body.role)[0];route['model']=model
        else:route={'url':config['base_url'],'key':runtime_config.key_for(config),'model':model,'transport':'','isolated_tls':True}
        if not route['url'] or not route['key']:raise ConfigurationError('Сервер моделей не настроен. Сохраните адрес и API-ключ.')
        started=time.monotonic();name=providers.identity('llm',route['url'])
        with providers.lane(name,25,config['concurrency'],config['requests_per_minute']):
            value=completion({'model':model,'messages':[{'role':'user','content':'Ответь одним словом: OK'}],'max_tokens':32,'temperature':0,'thinking':{'type':'disabled'}},20,route)
            if not value.get('choices'):raise TransportError('Invalid model response')
        providers.success(name)
        return {'status':'ok','model':model,'configuration_version':version,'latency_ms':round((time.monotonic()-started)*1000),'notice':'Тест использовал один запрос сервера моделей.'}
    except (ConfigurationError,LLMError) as error:
        raise HTTPException(409,str(error))
    except (TransportError,httpx.HTTPError,ValueError) as error:
        providers.failure(name,'MODEL_SERVER_CHECK_FAILED',cooldown=15)
        status=getattr(error,'status',None)
        raise HTTPException(503,'Сервер моделей не подтвердил доступность. Проверьте адрес, ключ и имя модели.'+(f' HTTP {status}.' if status else ''))

def rotation_state(session,run,version,config):
    snapshot=db.read(run.policy).get('agent_config')
    value={'id':run.id,'title':run.title,'status':run.status,'generation':run.generation,
           'connection_version':snapshot.get('connection_version',snapshot['version']) if snapshot else None,
           'analysis_version':snapshot['version'] if snapshot else None,'can_rotate':False,'reason':''}
    if not snapshot:value['reason']='Историческое исследование использует основное подключение без сохранённого ключа. Для новой конфигурации создайте новое исследование.'
    elif value['connection_version']==version:value['reason']='Исследование уже использует текущую версию подключения.'
    elif run.status not in ('cancelled','partial','completed','completed_with_gaps') or session.scalar(select(db.Job.id).where(db.Job.run_id==run.id,db.Job.status=='running').limit(1)) or session.scalar(select(db.Invocation.id).where(db.Invocation.run_id==run.id,db.Invocation.status=='running').limit(1)):
        value['reason']='Сначала остановите исследование и дождитесь завершения текущего запроса.'
    else:
        try:
            old=runtime_config.connection(snapshot,session)
            if runtime_config.server_id(old)!=runtime_config.server_id(config):
                raise ConfigurationError('Ротация разрешена только для того же адреса API и типа подключения. Для другого сервера создайте новое исследование.')
            if not runtime_config.key_for(config):raise ConfigurationError('Сначала настройте действующий API-ключ.')
            value['can_rotate']=True
        except ConfigurationError as error:value['reason']=str(error)
    return value

@router.get('/admin/model-settings/researches')
def rotation_researches(request:Request):
    admin(request)
    with db.Session() as s:
        version,config=runtime_config.current(s)
        rows=s.scalars(select(db.Run).where(db.Run.workspace==request.state.identity['workspace']).order_by(db.Run.created.desc()).limit(100))
        return {'configuration_version':version,'researches':[rotation_state(s,row,version,config) for row in rows]}

class RotationInput(Strict):
    configuration_version:int=Field(ge=0)
    connection_version:int=Field(ge=0)
    generation:int=Field(ge=0)

@router.post('/admin/model-settings/researches/{id}/rotate')
def rotate_research_connection(id:str,body:RotationInput,request:Request):
    who=admin(request)
    from .app import lock_admission
    with db.Session.begin() as s:
        lock_admission(s)
        if s.bind.dialect.name=='postgresql':s.execute(text('SELECT pg_advisory_xact_lock(862319006)'))
        version,config=runtime_config.current(s)
        query=select(db.Run).where(db.Run.id==id,db.Run.workspace==who['workspace'])
        if s.bind.dialect.name=='postgresql':query=query.with_for_update()
        run=s.scalar(query)
        if not run:raise HTTPException(404,'Исследование не найдено')
        state=rotation_state(s,run,version,config)
        if version!=body.configuration_version or state['connection_version']!=body.connection_version or run.generation!=body.generation:
            raise HTTPException(409,'Настройки или исследование уже изменены. Обновите список и повторите действие.')
        if not state['can_rotate']:raise HTTPException(409,state['reason'])
        policy=db.read(run.policy);snapshot=policy['agent_config']
        snapshot['connection_version']=version;snapshot['server_id']=runtime_config.server_id(config)
        run.policy=db.dump(policy);run.generation+=1;run.updated=time.time()
        s.add(db.Event(run_id=run.id,stage='connection_rotation',message='Администратор обновил реквизиты того же сервера моделей. Модели, промпты и готовые результаты сохранены.',
                       payload=db.dump({'from_connection_version':body.connection_version,'to_connection_version':version,
                                        'analysis_version':snapshot['version'],'actor':who['role']+':'+who['workspace'],'generation':run.generation})))
    return {'status':'rotated','connection_version':version,'notice':'Реквизиты обновлены. Продолжение исследования запускается отдельно; готовые результаты не изменены.'}

@router.post('/runs/{id}/source-links',status_code=201)
def add_source_links(id:str,body:SourceLinksInput,request:Request):
    from .app import get_run
    from .evidence import public_url,EvidenceError
    with db.Session() as s:
        get_run(s,id,request)
        if body.model_id:
            model=s.get(db.Model,body.model_id)
            if not model or model.run_id!=id:raise HTTPException(404,'Карточка не найдена')
    for url in body.urls:
        try:public_url(url)
        except (EvidenceError,ValueError):raise HTTPException(422,'Добавляйте только доступные публичные адреса источников')
    with db.Session.begin() as s:
        query=select(db.Run).where(db.Run.id==id,db.Run.workspace==request.state.identity['workspace'])
        if s.bind.dialect.name=='postgresql':query=query.with_for_update()
        run=s.scalar(query)
        if not run:raise HTTPException(404,'Исследование не найдено')
        config=db.read(run.settings);links=config.get('manual_sources',[]);added=0
        for url in body.urls:
            if not any(item['url']==url and item.get('model_id')==body.model_id for item in links):
                links.append({'url':url,'model_id':body.model_id,'added':time.time()});added+=1
        if len(links)>50:raise HTTPException(422,'Для исследования уже сохранено максимально допустимое число ссылок')
        config['manual_sources']=links;run.settings=db.dump(config);run.updated=time.time()
        has_cards=body.model_id is not None or s.scalar(select(db.Model.id).where(db.Model.run_id==id).limit(1)) is not None
        if has_cards:
            advice='Используйте повторный отбор карточки, чтобы проверить материалы.'
        elif run.status in ('partial','cancelled'):
            advice='Нажмите «Продолжить» в обзоре, чтобы учесть материалы в первичном поиске.'
        else:
            advice='Материалы будут учтены при следующем проходе первичного поиска.'
        notice='Ссылки сохранены. '+advice
        s.add(db.Event(run_id=id,model_id=body.model_id,stage='source_links',message=notice,payload=db.dump({'urls':body.urls,'added':added})))
    return {'status':'saved','added':added,'notice':notice}
