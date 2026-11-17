"""Shared circuit breakers and expiring concurrency slots across all worker replicas."""
from contextlib import contextmanager
import hashlib
import time
from sqlalchemy import select,delete,func
from . import db
from .execution import DependencyUnavailable

def identity(kind,url):return kind+':'+hashlib.sha256(url.rstrip('/').encode()).hexdigest()[:16]

def state_row(session,name):
    table=db.ProviderState
    insert=__import__('sqlalchemy.dialects.postgresql' if session.bind.dialect.name=='postgresql' else 'sqlalchemy.dialects.sqlite',fromlist=['insert']).insert
    session.execute(insert(table).values(id=name,failures=0,blocked_until=0,last_error='',updated=time.time(),window_started=0,window_requests=0).on_conflict_do_nothing(index_elements=['id']))
    query=select(table).where(table.id==name)
    if session.bind.dialect.name=='postgresql':query=query.with_for_update()
    return session.scalar(query)

@contextmanager
def lane(name,timeout,concurrency=2,rpm=30):
    holder=db.uid();now=time.time()
    with db.Session.begin() as session:
        state=state_row(session,name)
        if state.blocked_until>now:raise DependencyUnavailable('Провайдер временно недоступен; ожидаем восстановления',state.blocked_until-now,False)
        session.execute(delete(db.ProviderSlot).where(db.ProviderSlot.provider==name,db.ProviderSlot.expires<=now))
        if session.scalar(select(func.count()).select_from(db.ProviderSlot).where(db.ProviderSlot.provider==name))>=concurrency:
            raise DependencyUnavailable('Достигнут общий лимит одновременных запросов провайдера',2,False)
        if now-state.window_started>=60:state.window_started=now;state.window_requests=0
        if rpm and state.window_requests>=rpm:raise DependencyUnavailable('Достигнут лимит запросов провайдера в минуту',60-(now-state.window_started),False)
        state.window_requests+=1
        session.add(db.ProviderSlot(id=holder,provider=name,holder=holder,expires=now+timeout+15))
    try:yield name
    finally:
        with db.Session.begin() as session:session.execute(delete(db.ProviderSlot).where(db.ProviderSlot.id==holder,db.ProviderSlot.holder==holder))

def success(name):
    with db.Session.begin() as session:
        state=state_row(session,name);state.failures=0;state.blocked_until=0;state.last_error='';state.updated=time.time()

def failure(name,error,cooldown=0,credentials=False):
    with db.Session.begin() as session:
        state=state_row(session,name);state.failures+=1;state.last_error=error[:120];state.updated=time.time()
        wait=300 if credentials else max(cooldown,30 if state.failures>=3 else 0)
        if wait:state.blocked_until=max(state.blocked_until,time.time()+wait)
