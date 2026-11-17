import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import select, update, or_
from .db import Session, Run, Model, Job, uid, init, emit
from .pipeline import discover, process_model, finish_run
from .llm import Cancelled

def claim_job(owner):
    with Session.begin() as s:
        query=select(Job).join(Run,Run.id==Job.run_id).where(Run.status.in_(['queued','running']),or_(Job.status=='queued',(Job.status=='running') & (Job.lease_until<time.time()))).order_by(Job.id).limit(1)
        if s.bind.dialect.name=='postgresql':query=query.with_for_update(skip_locked=True,of=Job)
        job=s.scalar(query)
        if not job:return None
        changed=s.execute(update(Job).where(Job.id==job.id,or_(Job.status=='queued',(Job.status=='running') & (Job.lease_until<time.time()))).values(status='running',lease_owner=owner,lease_until=time.time()+120,attempts=Job.attempts+1))
        if not changed.rowcount:return None
        r=s.get(Run,job.run_id);r.status='running'
        return job.id,job.run_id,job.model_id

def heartbeat(job_id,owner,stop):
    while not stop.wait(20):
        with Session.begin() as s:
            s.execute(update(Job).where(Job.id==job_id,Job.lease_owner==owner,Job.status=='running').values(lease_until=time.time()+120))

def execute(job,owner):
    job_id,run_id,model_id=job
    stop=threading.Event();t=threading.Thread(target=heartbeat,args=(job_id,owner,stop),daemon=True);t.start()
    try:
        process_model(model_id) if model_id else discover(run_id)
        status='completed'
    except Cancelled:status='cancelled'
    except Exception as e:
        # Only application-safe errors; no HTTP request objects/headers are logged.
        message=str(e)[:500] if isinstance(e,(RuntimeError,ValueError)) or e.__class__.__module__.startswith('backend') else type(e).__name__
        emit(run_id,'error',message,model_id)
        with Session.begin() as s:
            if model_id:
                m=s.get(Model,model_id);m.status='error';m.error=message
            else:s.get(Run,run_id).error=message
        status='failed'
    finally:
        stop.set();t.join(timeout=2)
    with Session.begin() as s:
        s.execute(update(Job).where(Job.id==job_id,Job.lease_owner==owner).values(status=status,lease_until=0))
    finish_run(run_id)

def main():
    init();stop=threading.Event()
    signal.signal(signal.SIGINT,lambda *_:stop.set())
    signal.signal(signal.SIGTERM,lambda *_:stop.set())
    owner=uid()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=set()
        while not stop.is_set():
            futures={f for f in futures if not f.done()}
            if len(futures)<2:
                job=claim_job(owner)
                if job:futures.add(pool.submit(execute,job,owner));continue
            stop.wait(1)

if __name__=='__main__':main()
