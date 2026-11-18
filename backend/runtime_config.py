"""Versioned administrator settings; credentials never enter API views or run snapshots."""
import base64
import copy
import hashlib
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
import yaml
from cryptography.fernet import Fernet,InvalidToken
from pydantic import Field,SecretStr,model_validator
from sqlalchemy import select,text
from . import db,settings
from .schemas import Strict

ROLES=('discovery','screening','critic','verification_search','verifier','launch_brief')
PROFILES={'quick':{'max_candidates':4,'rounds':1},'balanced':{'max_candidates':8,'rounds':2},'deep':{'max_candidates':16,'rounds':4}}
_lock=threading.RLock()

class ConfigurationError(ValueError):pass

def default_prompts():
    with (settings.ROOT/'config/agent_prompts.yaml').open(encoding='utf-8') as stream:return yaml.safe_load(stream)

def default_prompt(role):return default_prompts()[role]

def defaults():
    policy=settings.policy()
    return {'connection_mode':'default','base_url':'','encrypted_key':'',
        'models':{role:settings.setting('LLM_'+role.upper()+'_MODEL',settings.MODEL) for role in ROLES},
        'prompts':default_prompts(),'requests_per_minute':int(settings.setting('LLM_REQUESTS_PER_MINUTE','30')),
        'concurrency':int(settings.setting('LLM_MAX_CONCURRENCY','2')),
        'research_max_calls':policy['limits']['max_llm_calls'],'research_max_tokens':policy['limits']['max_total_tokens']}

def current(session=None):
    if session is None:
        with db.Session() as s:return current(s)
    row=session.scalar(select(db.ServiceConfiguration).order_by(db.ServiceConfiguration.version.desc()).limit(1))
    return (row.version,db.read(row.data)) if row else (0,defaults())

def cipher():
    secret=settings.setting('CONFIG_ENCRYPTION_KEY') or settings.setting('AUTH_CONTEXT_SECRET')
    if not secret:raise ConfigurationError('Хранилище ключей сервера моделей не настроено')
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(('venture-lens:model-settings:v1:'+secret).encode()).digest()))

def key_for(config):
    if config['connection_mode']=='default':return settings.API_KEY
    try:return cipher().decrypt(config.get('encrypted_key','').encode()).decode()
    except (InvalidToken,ValueError):raise ConfigurationError('Ключ сервера моделей недоступен. Обновите его в настройках.')

def public_view():
    version,config=current()
    result={k:v for k,v in config.items() if k!='encrypted_key'}
    result.update(version=version,key_configured=bool(settings.API_KEY if config['connection_mode']=='default' else config.get('encrypted_key')),
                  encryption_ready=bool(settings.setting('CONFIG_ENCRYPTION_KEY') or settings.setting('AUTH_CONTEXT_SECRET')))
    with db.Session() as s:
        result['history']=[{'version':r.version,'created':r.created,'actor':r.actor} for r in s.scalars(select(db.ServiceConfiguration).order_by(db.ServiceConfiguration.version.desc()).limit(12))]
    return result

class ModelSettingsInput(Strict):
    version:int=Field(ge=0)
    connection_mode:str=Field(pattern='^(default|custom)$')
    base_url:str=Field(default='',max_length=400)
    api_key:SecretStr|None=None
    models:dict[str,str]
    prompts:dict[str,str]
    requests_per_minute:int=Field(ge=1,le=300)
    concurrency:int=Field(ge=1,le=8)
    research_max_calls:int=Field(ge=10,le=300)
    research_max_tokens:int=Field(ge=30000,le=2000000)
    @model_validator(mode='after')
    def validate_settings(self):
        if set(self.models)!=set(ROLES) or set(self.prompts)!=set(ROLES):raise ValueError('Настройте все роли агентов')
        if any(not re.fullmatch(r'[A-Za-z0-9._:/-]{1,120}',m) for m in self.models.values()):raise ValueError('Проверьте имена моделей')
        if any(not 20<=len(p.strip())<=16000 for p in self.prompts.values()):raise ValueError('Промпт каждой роли должен содержать от 20 до 16000 символов')
        if self.api_key and (not 8<=len(self.api_key.get_secret_value())<=4096 or re.search(r'[\s\x00-\x1f\x7f-\uffff]',self.api_key.get_secret_value())):raise ValueError('Проверьте API-ключ: он не должен содержать пробелы или управляющие символы')
        if self.connection_mode=='custom':
            p=urlsplit(self.base_url)
            if p.scheme!='https' or not p.hostname or p.username or p.password or p.query or p.fragment or re.search(r'\s',self.base_url):raise ValueError('Укажите HTTPS-адрес API без ключа в адресе')
        return self

def save(body,actor):
    if body.connection_mode=='custom':
        from .evidence import public_url
        try:public_url(body.base_url)
        except Exception:raise ConfigurationError('Укажите публичный HTTPS-адрес сервера моделей')
    with _lock,db.Session.begin() as s:
        if s.bind.dialect.name=='postgresql':s.execute(text('SELECT pg_advisory_xact_lock(862319006)'))
        version,old=current(s)
        if version!=body.version:raise ConfigurationError('Настройки уже изменены. Обновите страницу и повторите сохранение.')
        known_keys=[settings.API_KEY,body.api_key.get_secret_value() if body.api_key else '']
        if old.get('encrypted_key'):
            try:known_keys.append(key_for(old))
            except ConfigurationError:pass
        if any(len(key)>=16 and any(key in prompt for prompt in body.prompts.values()) for key in known_keys if key):
            raise ConfigurationError('Промпт содержит API-ключ. Уберите его: ключи задаются только в поле подключения.')
        value=body.model_dump(exclude={'version','api_key'})
        value['base_url']=value['base_url'].rstrip('/') if body.connection_mode=='custom' else ''
        value['encrypted_key']=''
        if body.connection_mode=='custom':
            if body.api_key:value['encrypted_key']=cipher().encrypt(body.api_key.get_secret_value().encode()).decode()
            elif old['connection_mode']=='custom' and old['base_url']==value['base_url']:value['encrypted_key']=old['encrypted_key']
            else:raise ConfigurationError('Для нового сервера введите отдельный API-ключ')
            if not value['encrypted_key']:raise ConfigurationError('Введите API-ключ сервера моделей')
        row=db.ServiceConfiguration(version=version+1,data=db.dump(value),created=time.time(),actor=actor)
        s.add(row)
    return public_view()

def snapshot(session=None):
    version,config=current(session)
    return {'version':version,'models':copy.deepcopy(config['models']),'prompts':copy.deepcopy(config['prompts'])}

def configured():
    _,config=current()
    return bool(settings.BASE_URL and settings.API_KEY) if config['connection_mode']=='default' else bool(config['base_url'] and config['encrypted_key'])

def connection(snapshot_value):
    version=snapshot_value['version']
    if not version:return defaults()
    with db.Session() as s:
        row=s.get(db.ServiceConfiguration,version)
        if not row:raise ConfigurationError('Версия настроек сервера моделей не найдена')
        return db.read(row.data)

def prepare_research(body,session):
    config=body.model_dump()
    config['adaptive_selection']=body.count is None
    config['count']=body.count or PROFILES[body.sampling]['max_candidates']
    config['manual_sources']=[{'url':url,'model_id':None,'added':time.time()} for url in body.source_urls]
    _,runtime=current(session);policy=settings.policy()
    policy['limits'].update(max_llm_calls=runtime['research_max_calls'],max_total_tokens=runtime['research_max_tokens'])
    if body.count is None:policy['limits'].update(max_sources_per_model={'quick':4,'balanced':6,'deep':8}[body.sampling],context_chars={'quick':18000,'balanced':24000,'deep':32000}[body.sampling])
    policy['agent_config']=snapshot(session)
    return config,policy

def market_label(config):
    return config.get('market','').strip() if config.get('region')=='Custom' else {'US':'США','UK':'Великобритания','EU':'Евросоюз','Global':'Международный рынок'}.get(config.get('region'),'Международный рынок')
