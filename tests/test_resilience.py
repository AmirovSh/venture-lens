import json
import time
from pathlib import Path
from contextlib import contextmanager
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine,text,select
from backend import db,pipeline,worker,llm,providers,storage
from backend.execution import current,LeaseLost,Cancelled,DependencyUnavailable
from backend.schemas import Discovery
from backend.app import app
from test_pipeline import store,seed

def test_expired_owner_cannot_overwrite_new_attempt(store):
    seed(store);first=worker.claim_job('first')
    token=current.set(first)
    try:pipeline.save_model('model',{'checkpoint':'original'})
    finally:current.reset(token)
    with store.begin() as s:s.get(db.Job,'job').lease_until=time.time()-1
    second=worker.claim_job('second')
    assert second.epoch>first.epoch and second.owner!=first.owner
    token=current.set(first)
    try:
        with pytest.raises(LeaseLost):pipeline.save_model('model',{'checkpoint':'stale'})
    finally:current.reset(token)
    with store() as s:assert db.read(s.get(db.Model,'model').data)=={'checkpoint':'original'}

def test_cancel_resume_fences_a_late_response(store):
    seed(store);first=worker.claim_job('worker')
    client=TestClient(app)
    assert client.post('/api/runs/run/cancel').status_code==200
    assert client.post('/api/runs/run/retry').status_code==200
    token=current.set(first)
    try:
        with pytest.raises(LeaseLost):pipeline.save_model('model',{'late':'response'},status='passed')
    finally:current.reset(token)
    next_attempt=worker.claim_job('new')
    assert next_attempt.generation==first.generation+1
    with store() as s:assert s.get(db.Model,'model').status=='queued'

def test_queue_wait_does_not_expire_research(store):
    seed(store)
    with store.begin() as s:s.get(db.Run,'run').created=time.time()-86400
    pipeline.guard('run')
    with store.begin() as s:s.get(db.Run,'run').active_seconds=2701
    with pytest.raises(RuntimeError,match='активного'):pipeline.guard('run')

def test_exhausted_job_moves_to_terminal_failure(store):
    seed(store)
    with store.begin() as s:s.get(db.Job,'job').attempts=99
    assert worker.claim_job('worker') is None
    with store() as s:
        assert s.get(db.Job,'job').status=='failed'
        assert s.get(db.Run,'run').status=='partial'

def test_dependency_wait_is_scheduled_without_losing_artifact(store,monkeypatch):
    seed(store);claim=worker.claim_job('worker')
    def pause(_):
        pipeline.save_model('model',{'checkpoint':'saved'})
        raise DependencyUnavailable('Search unavailable',60,False)
    monkeypatch.setattr(worker,'process_model',pause)
    worker.execute(claim)
    with store() as s:
        job=s.get(db.Job,'job');assert job.status=='waiting' and job.next_retry_at>time.time()+50
        assert job.attempts==0
        assert s.get(db.Run,'run').status=='waiting'
        assert db.read(s.get(db.Model,'model').data)['checkpoint']=='saved'
    assert worker.claim_job('other') is None

def test_shared_provider_slots_and_expiration(store):
    with providers.lane('provider',10,concurrency=1):
        with pytest.raises(DependencyUnavailable):
            with providers.lane('provider',10,concurrency=1):pass
        # An independent provider is not blocked by the first one's concurrency limit.
        with providers.lane('other',10,concurrency=1):pass
    with store.begin() as s:s.add(db.ProviderSlot(id='old',provider='provider',holder='lost-worker',expires=time.time()-1))
    with providers.lane('provider',10,concurrency=1):pass

def test_circuit_breaker_stops_calls_and_recovers(store):
    for _ in range(3):providers.failure('provider','503')
    with pytest.raises(DependencyUnavailable):
        with providers.lane('provider',10):pass
    with store.begin() as s:s.get(db.ProviderState,'provider').blocked_until=time.time()-1
    with providers.lane('provider',10):providers.success('provider')
    with store() as s:assert s.get(db.ProviderState,'provider').failures==0

def test_independent_llm_fallback_preserves_separate_credentials(store,monkeypatch):
    from backend.transport import TransportError
    seed(store)
    monkeypatch.setattr(llm.settings,'BASE_URL','https://primary.example/v1')
    monkeypatch.setattr(llm.settings,'API_KEY','PRIMARY_TEST_KEY')
    monkeypatch.setattr(llm.settings,'FALLBACK_MODEL','fallback-model')
    original=llm.settings.setting
    monkeypatch.setattr(llm.settings,'setting',lambda key,default='':{'LLM_FALLBACK_BASE_URL':'https://backup.example/v1','LLM_FALLBACK_API_KEY':'BACKUP_TEST_KEY'}.get(key,original(key,default)))
    calls=[]
    def completion(payload,timeout,route=None):
        calls.append(route.copy())
        if 'primary' in route['url']:raise TransportError('HTTP 503',503)
        return {'choices':[{'message':{'content':'{"candidates":[]}'}}],'usage':{'total_tokens':50}}
    monkeypatch.setattr(llm,'completion',completion)
    assert llm.invoke('run','discovery',{},Discovery,'Return JSON')=={'candidates':[]}
    assert calls[0]['key']=='PRIMARY_TEST_KEY' and calls[1]['key']=='BACKUP_TEST_KEY'
    with store() as s:assert s.query(db.Invocation).filter_by(status='completed').one().provider!=s.query(db.Invocation).filter_by(status='failed').one().provider

def test_old_cancelled_llm_output_is_not_persisted(store,monkeypatch):
    seed(store);claim=worker.claim_job('worker')
    def completion(*_,**__):
        with store.begin() as s:
            run=s.get(db.Run,'run');run.status='cancelled';run.generation+=1
        return {'choices':[{'message':{'content':'{"candidates":[]}'}}],'usage':{'total_tokens':50}}
    monkeypatch.setattr(llm,'completion',completion)
    token=current.set(claim)
    try:
        with pytest.raises(Cancelled):llm.invoke('run','discovery',{},Discovery,'Return JSON')
    finally:current.reset(token)
    with store() as s:assert s.query(db.Invocation).one().output is None

def test_readiness_detects_database_failure_but_liveness_stays_up(monkeypatch):
    from sqlalchemy.exc import OperationalError
    import backend.app as module
    def unavailable():raise OperationalError('SELECT 1',{},Exception('offline'))
    monkeypatch.setattr(module,'Session',unavailable)
    client=TestClient(app)
    assert client.get('/api/live').status_code==200
    assert client.get('/api/ready').status_code==503

def test_storage_detects_corruption_and_cache_is_disposable(tmp_path,monkeypatch):
    monkeypatch.setattr(storage,'DATA',tmp_path);monkeypatch.setattr(storage,'enabled',lambda:False)
    checksum,_=storage.put_source('Original source')
    assert storage.get_source(checksum)=='Original source'
    (tmp_path/'sources'/f'{checksum}.txt').write_text('corrupt')
    with pytest.raises(storage.IntegrityError):storage.get_source(checksum)
    storage.put_source('Original source');assert storage.get_source(checksum)=='Original source'
    storage.put_cache('search','query',{'results':[1]})
    assert storage.get_cache('search','query')=={'results':[1]}
    (tmp_path/'cache/search/query.json').write_text('{broken')
    assert storage.get_cache('search','query') is None


def test_object_store_network_failure_becomes_retryable_dependency(monkeypatch):
    from botocore.exceptions import EndpointConnectionError
    class Offline:
        def get_object(self,**_):raise EndpointConnectionError(endpoint_url='http://test.invalid')
    monkeypatch.setattr(storage,'enabled',lambda:True)
    monkeypatch.setattr(storage,'client',lambda:Offline())
    with pytest.raises(DependencyUnavailable,match='хранилище') as failure:storage.get_source('0'*64)
    assert failure.value.retry_after>=1


def test_draining_worker_defers_without_starting_another_llm_call(store,monkeypatch):
    from backend.execution import draining
    seed(store);claim=worker.claim_job('worker');token=current.set(claim)
    monkeypatch.setattr(llm,'completion',lambda *_a,**_k:pytest.fail('A draining worker sent another paid call'))
    draining.set()
    try:
        with pytest.raises(DependencyUnavailable) as failure:llm.invoke('run','discovery',{},Discovery,'Return JSON')
        assert failure.value.count_attempt is False
        with store() as s:assert s.query(db.Invocation).count()==0
    finally:draining.clear();current.reset(token)

def test_migration_adopts_legacy_database_without_losing_runs(tmp_path):
    from backend.migrate import upgrade
    engine=create_engine('sqlite:///'+str(tmp_path/'legacy.db'))
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE runs (id VARCHAR PRIMARY KEY,title VARCHAR,status VARCHAR,created FLOAT,updated FLOAT,settings TEXT,policy TEXT,calls INTEGER,tokens INTEGER,error TEXT)'))
        connection.execute(text("INSERT INTO runs VALUES ('legacy','Saved','completed',0,0,'{}','{}',0,0,'')"))
    with engine.begin() as connection:upgrade(connection)
    with engine.connect() as connection:
        row=connection.execute(text("SELECT title,generation,active_seconds FROM runs WHERE id='legacy'")).one()
        assert tuple(row)==('Saved',0,0)
        assert connection.execute(text('SELECT version_num FROM alembic_version')).scalar()=='0003_registration'
        assert connection.execute(text('SELECT COUNT(*) FROM demo_sessions')).scalar()==0
        assert connection.execute(text('SELECT COUNT(*) FROM demo_login_limits')).scalar()==0
        assert connection.execute(text("SELECT workspace FROM runs WHERE id='legacy'")).scalar()=='main'
        assert connection.execute(text('SELECT COUNT(*) FROM registered_users')).scalar()==0
    engine.dispose()
