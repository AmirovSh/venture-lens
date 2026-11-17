import argparse
import json
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.request import urlopen
from sqlalchemy import select,update,or_
from sqlalchemy.exc import SQLAlchemyError
from .db import Session,Run,Model,Job,Invocation,uid,read
from .pipeline import discover,process_model,finish_run
from .execution import Claim,current,assert_owner,Cancelled,DependencyUnavailable,draining
from .settings import setting

STATE={'tick':time.time(),'database':False,'active':0,'draining':False}
def lease_seconds():return float(setting('JOB_LEASE_SECONDS','120'))

def claim_job(owner):
    expired_run=None
    with Session.begin() as s:
        query=select(Job).join(Run,Run.id==Job.run_id).where(Run.status.in_(['queued','running','waiting']),Job.next_retry_at<=time.time(),or_(Job.status.in_(['queued','waiting']),(Job.status=='running') & (Job.lease_until<time.time()))).order_by(Job.next_retry_at,Job.id).limit(1)
        if s.bind.dialect.name=='postgresql':query=query.with_for_update(skip_locked=True,of=Job)
        job=s.scalar(query)
        if not job:return None
        run=s.get(Run,job.run_id)
        if job.attempts>=read(run.policy)['limits'].get('max_job_attempts',8):
            job.status='failed';job.last_error='Достигнут лимит попыток задания'
            if job.model_id:
                model=s.get(Model,job.model_id);model.status='error';model.error=job.last_error
            expired_run=job.run_id
        else:
            token=uid();epoch=job.epoch+1
            changed=s.execute(update(Job).where(Job.id==job.id,Job.epoch==job.epoch,or_(Job.status.in_(['queued','waiting']),(Job.status=='running') & (Job.lease_until<time.time()))).values(status='running',lease_owner=token,epoch=epoch,lease_until=time.time()+lease_seconds(),attempts=Job.attempts+1))
            if not changed.rowcount:return None
            claim=Claim(job.id,job.run_id,job.model_id,token,epoch,run.generation)
    if expired_run:
        finish_run(expired_run);return None
    with Session.begin() as s:
        s.execute(update(Run).where(Run.id==claim.run_id,Run.generation==claim.generation,Run.status.in_(['queued','waiting'])).values(status='running',waiting_reason=''))
    return claim

def account(claim,renew=False):
    with claim.accounting_lock:
        with Session.begin() as s:
            run=assert_owner(s,claim.run_id)
            now=time.time();run.active_seconds+=max(0,now-claim.accounted)
            if renew:s.get(Job,claim.job_id).lease_until=now+lease_seconds()
        claim.accounted=now

def heartbeat(claim,stop):
    context=current.set(claim)
    try:
        while not stop.wait(max(.2,min(20,lease_seconds()/3))):
            try:account(claim,True);STATE['database']=True
            except (SQLAlchemyError,Cancelled):
                STATE['database']=False;claim.lost.set();return
    finally:current.reset(context)

def execute(claim,owner=None):
    context=current.set(claim)
    stop=threading.Event();thread=threading.Thread(target=heartbeat,args=(claim,stop),daemon=True);thread.start()
    status='completed';retry=None;message=''
    try:
        with Session.begin() as s:
            assert_owner(s,claim.run_id)
            for invocation in s.scalars(select(Invocation).where(Invocation.run_id==claim.run_id,Invocation.model_id==claim.model_id,Invocation.status=='running')):invocation.status='interrupted'
        process_model(claim.model_id) if claim.model_id else discover(claim.run_id)
    except DependencyUnavailable as error:
        status='waiting';retry=error;message=str(error)
    except Cancelled:status='cancelled'
    except SQLAlchemyError:
        STATE['database']=False;claim.lost.set();return
    except Exception as error:
        status='failed';message=str(error)[:500] if isinstance(error,(ValueError,RuntimeError)) or error.__class__.__module__.startswith('backend') else type(error).__name__
    finally:
        stop.set();thread.join(timeout=2)
        try:
            if not claim.lost.is_set():account(claim)
        except (SQLAlchemyError,Cancelled):claim.lost.set()
        current.reset(context)
    if claim.lost.is_set():return
    context=current.set(claim)
    try:
        with Session.begin() as s:
            run=assert_owner(s,claim.run_id);job=s.get(Job,claim.job_id)
            if status=='waiting':
                job.next_retry_at=time.time()+retry.retry_after;job.last_error=message
                if not retry.count_attempt:job.attempts=max(0,job.attempts-1)
                run.waiting_reason=message
            elif status=='failed':
                job.last_error=message
                if claim.model_id:
                    model=s.get(Model,claim.model_id);model.status='error';model.error=message
                else:run.error=message
            if status=='waiting' and claim.model_id:
                model=s.get(Model,claim.model_id);model.status='waiting';model.error=message
            from .db import Event,dump
            if message:s.add(Event(run_id=claim.run_id,model_id=claim.model_id,stage='waiting' if retry else 'error',message=message,payload=dump({'retry_at':job.next_retry_at} if retry else {})))
            job.status=status;job.lease_until=0;job.lease_owner=None
    except (SQLAlchemyError,Cancelled):return
    finally:current.reset(context)
    finish_run(claim.run_id)

class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        live=time.time()-STATE['tick']<45
        ready=live and STATE['database'] and not STATE['draining']
        ok=live if self.path=='/live' else ready
        self.send_response(200 if ok else 503);self.send_header('Content-Type','application/json');self.end_headers()
        self.wfile.write(json.dumps(STATE).encode())
    def log_message(self,*args):pass

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--health-check',choices=['live','ready']);args=parser.parse_args()
    port=int(setting('WORKER_HEALTH_PORT','8091'))
    if args.health_check:
        with urlopen(f'http://127.0.0.1:{port}/{args.health_check}',timeout=3) as response:raise SystemExit(0 if response.status==200 else 1)
    stop=threading.Event();draining.clear()
    def shutdown(*_):STATE['draining']=True;draining.set();stop.set()
    signal.signal(signal.SIGINT,shutdown);signal.signal(signal.SIGTERM,shutdown)
    server=ThreadingHTTPServer(('0.0.0.0',port),Health);threading.Thread(target=server.serve_forever,daemon=True).start()
    concurrency=int(setting('WORKER_CONCURRENCY','2'));owner=uid()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures=set()
        while not stop.is_set():
            STATE['tick']=time.time()
            for future in list(futures):
                if future.done():
                    try:future.result()
                    except Exception:print('WORKER_TASK_FAILURE',flush=True)
                    futures.remove(future)
            STATE['active']=len(futures)
            if len(futures)<concurrency:
                try:
                    claim=claim_job(owner);STATE['database']=True
                    if claim:futures.add(pool.submit(execute,claim));continue
                except SQLAlchemyError:STATE['database']=False
            stop.wait(1 if STATE['database'] else 3)
    server.shutdown()

if __name__=='__main__':main()
