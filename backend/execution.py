"""Fenced execution: every persisted result is checked in the same transaction."""
from contextvars import ContextVar
from dataclasses import dataclass,field
import threading
import time
from sqlalchemy import select

class Cancelled(Exception):pass
class LeaseLost(Cancelled):pass
class DependencyUnavailable(Exception):
    def __init__(self,message,retry_after=30,count_attempt=True):
        super().__init__(message);self.retry_after=max(1,float(retry_after));self.count_attempt=count_attempt

draining=threading.Event()

@dataclass
class Claim:
    job_id:str
    run_id:str
    model_id:str|None
    owner:str
    epoch:int
    generation:int
    lost:threading.Event=field(default_factory=threading.Event)
    accounted:float=field(default_factory=time.time)
    accounting_lock:threading.Lock=field(default_factory=threading.Lock)

current:ContextVar[Claim|None]=ContextVar('job_claim',default=None)

def assert_owner(session,run_id):
    from .db import Run,Job
    claim=current.get()
    query=select(Run).where(Run.id==run_id)
    if session.bind.dialect.name=='postgresql':query=query.with_for_update()
    run=session.scalar(query)
    if not run or run.status=='cancelled':raise Cancelled('Исследование остановлено')
    if claim is None:return run
    if claim.run_id!=run_id or claim.lost.is_set():raise LeaseLost('Исполнитель потерял право записи')
    query=select(Job).where(Job.id==claim.job_id)
    if session.bind.dialect.name=='postgresql':query=query.with_for_update()
    job=session.scalar(query)
    if not job or job.status!='running' or job.lease_owner!=claim.owner or job.epoch!=claim.epoch or job.lease_until<=time.time() or run.generation!=claim.generation:
        claim.lost.set();raise LeaseLost('Задание передано другому исполнителю или отменено')
    return run
