"""Administrator model controls and workspace-scoped source links."""
import time
import httpx
from typing import Literal
from fastapi import APIRouter,HTTPException,Request
from pydantic import BaseModel
from sqlalchemy import select
from . import db,runtime_config,settings,providers
from .runtime_config import ModelSettingsInput,ConfigurationError
from .schemas import SourceLinksInput

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
    from .llm import routes
    from .transport import completion,TransportError
    version,config=runtime_config.current();model=config['models'][body.role]
    if config['connection_mode']=='default':route=routes(body.role)[0];route['model']=model
    else:route={'url':config['base_url'],'key':runtime_config.key_for(config),'model':model,'transport':'','isolated_tls':True}
    if not route['url'] or not route['key']:raise HTTPException(409,'Сервер моделей не настроен')
    started=time.monotonic();name=providers.identity('llm',route['url'])
    try:
        with providers.lane(name,25,config['concurrency'],config['requests_per_minute']):
            value=completion({'model':model,'messages':[{'role':'user','content':'Ответь одним словом: OK'}],'max_tokens':32,'temperature':0,'thinking':{'type':'disabled'}},20,route)
            if not value.get('choices'):raise TransportError('Invalid model response')
        providers.success(name)
        return {'status':'ok','model':model,'configuration_version':version,'latency_ms':round((time.monotonic()-started)*1000),'notice':'Тест использовал один запрос сервера моделей.'}
    except (TransportError,httpx.HTTPError,ValueError) as error:
        providers.failure(name,'MODEL_SERVER_CHECK_FAILED',cooldown=15)
        status=getattr(error,'status',None)
        raise HTTPException(503,'Сервер моделей не подтвердил доступность. Проверьте адрес, ключ и имя модели.'+(f' HTTP {status}.' if status else ''))

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
        s.add(db.Event(run_id=id,model_id=body.model_id,stage='source_links',message='Добавлены ссылки пользователя. Используйте повторный отбор, чтобы проверить материалы.',payload=db.dump({'urls':body.urls,'added':added})))
    return {'status':'saved','added':added,'notice':'Ссылки сохранены. Они будут загружены и проверены при повторном отборе; допуск пока не изменён.'}
