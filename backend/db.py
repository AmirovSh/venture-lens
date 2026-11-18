import json
import time
import uuid
from sqlalchemy import create_engine, Column, String, Text, Integer, Float, ForeignKey, UniqueConstraint, event as sa_event
from sqlalchemy.orm import declarative_base, sessionmaker
from .settings import DATABASE_URL, ROOT, DATA

DATA.mkdir(parents=True,exist_ok=True)
engine = create_engine(DATABASE_URL, connect_args={'check_same_thread': False, 'timeout': 5} if DATABASE_URL.startswith('sqlite') else {'connect_timeout':5}, pool_pre_ping=True,pool_recycle=300)
if DATABASE_URL.startswith('sqlite'):
    @sa_event.listens_for(engine, 'connect')
    def sqlite_options(connection, _):
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA foreign_keys=ON')
Session = sessionmaker(engine, expire_on_commit=False)
Base = declarative_base()

def uid(): return uuid.uuid4().hex[:16]
def dump(value): return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
def read(value): return json.loads(value)

class Run(Base):
    __tablename__ = 'runs'
    id = Column(String, primary_key=True, default=uid)
    workspace = Column(String(64), nullable=False, default='main', server_default='main', index=True)
    title = Column(String, nullable=False)
    status = Column(String, default='queued', index=True)
    created = Column(Float, default=time.time)
    updated = Column(Float, default=time.time)
    settings = Column(Text, nullable=False)
    policy = Column(Text, nullable=False)
    calls = Column(Integer, default=0)
    tokens = Column(Integer, default=0)
    error = Column(Text, default='')
    generation = Column(Integer,default=0,server_default='0',nullable=False)
    active_seconds = Column(Float,default=0,server_default='0',nullable=False)
    waiting_reason = Column(Text,default='',server_default='',nullable=False)
    degraded = Column(Integer,default=0,server_default='0',nullable=False)

class Model(Base):
    __tablename__ = 'models'
    id = Column(String, primary_key=True, default=uid)
    run_id = Column(String, ForeignKey('runs.id'), nullable=False, index=True)
    name = Column(String, nullable=False)
    company = Column(String, nullable=False)
    status = Column(String, default='queued')
    stage = Column(String, default='dossier')
    data = Column(Text, default='{}')
    score = Column(Float, nullable=True)
    score_upper = Column(Float, nullable=True)
    coverage = Column(Float, default=0)
    decision = Column(String, nullable=True)
    note = Column(Text, default='')
    error = Column(Text, default='')

class Job(Base):
    __tablename__ = 'jobs'
    id = Column(String, primary_key=True, default=uid)
    run_id = Column(String, ForeignKey('runs.id'), nullable=False)
    model_id = Column(String, ForeignKey('models.id'), nullable=True)
    key = Column(String, unique=True, nullable=False)
    status = Column(String, default='queued', index=True)
    lease_owner = Column(String, nullable=True)
    lease_until = Column(Float, default=0, index=True)
    attempts = Column(Integer, default=0)
    epoch = Column(Integer,default=0,server_default='0',nullable=False)
    next_retry_at = Column(Float,default=0,server_default='0',nullable=False,index=True)
    last_error = Column(Text,default='',server_default='',nullable=False)

class Event(Base):
    __tablename__ = 'events'
    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String, ForeignKey('runs.id'), nullable=False, index=True)
    model_id = Column(String, nullable=True)
    at = Column(Float, default=time.time)
    stage = Column(String)
    message = Column(Text)
    payload = Column(Text, default='{}')

class Invocation(Base):
    __tablename__ = 'invocations'
    id = Column(String, primary_key=True, default=uid)
    run_id = Column(String, ForeignKey('runs.id'), nullable=False, index=True)
    model_id = Column(String, nullable=True)
    role = Column(String)
    model = Column(String)
    provider = Column(String,nullable=True)
    prompt_version = Column(String, default='1.1')
    prompt_text = Column(Text,nullable=True)
    input_hash = Column(String)
    input = Column(Text)
    output = Column(Text, nullable=True)
    tokens = Column(Integer, default=0)
    status = Column(String, default='running')
    created = Column(Float, default=time.time)

class ProviderState(Base):
    __tablename__='provider_state'
    id=Column(String,primary_key=True)
    failures=Column(Integer,default=0,nullable=False)
    blocked_until=Column(Float,default=0,nullable=False)
    last_error=Column(String,default='',nullable=False)
    updated=Column(Float,default=time.time,nullable=False)
    window_started=Column(Float,default=0,server_default='0',nullable=False)
    window_requests=Column(Integer,default=0,server_default='0',nullable=False)

class ProviderSlot(Base):
    __tablename__='provider_slots'
    id=Column(String,primary_key=True)
    provider=Column(String,index=True,nullable=False)
    holder=Column(String,nullable=False)
    expires=Column(Float,nullable=False,index=True)


class ServiceConfiguration(Base):
    __tablename__='service_configurations'
    version=Column(Integer,primary_key=True)
    data=Column(Text,nullable=False)
    created=Column(Float,nullable=False,default=time.time)
    actor=Column(String(90),nullable=False)


class EvidenceAction(Base):
    __tablename__='evidence_actions'
    model_id=Column(String,ForeignKey('models.id'),primary_key=True)
    task_key=Column(String(80),primary_key=True)
    status=Column(String(16),nullable=False,default='open')
    owner=Column(String(120),nullable=False,default='')
    note=Column(Text,nullable=False,default='')
    source_urls=Column(Text,nullable=False,default='[]')
    version=Column(Integer,nullable=False,default=1)
    updated=Column(Float,nullable=False,default=time.time)


class Pilot(Base):
    __tablename__='pilots'
    model_id=Column(String,ForeignKey('models.id'),primary_key=True)
    data=Column(Text,nullable=False,default='{}')
    version=Column(Integer,nullable=False,default=1)
    created=Column(Float,nullable=False,default=time.time)
    updated=Column(Float,nullable=False,default=time.time)

def init():
    if DATABASE_URL.startswith('sqlite'):Base.metadata.create_all(engine)

def emit(run_id, stage, message, model_id=None, payload=None):
    with Session.begin() as s:
        from .execution import assert_owner
        assert_owner(s,run_id)
        s.add(Event(run_id=run_id, model_id=model_id, stage=stage, message=message, payload=dump(payload or {})))
        r = s.get(Run, run_id)
        if r:
            r.updated = time.time()
            if stage=='source_warning':r.degraded=1
