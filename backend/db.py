import json
import time
import uuid
from sqlalchemy import create_engine, Column, String, Text, Integer, Float, ForeignKey, UniqueConstraint, event as sa_event
from sqlalchemy.orm import declarative_base, sessionmaker
from .settings import DATABASE_URL, ROOT

(ROOT / 'data').mkdir(exist_ok=True)
engine = create_engine(DATABASE_URL, connect_args={'check_same_thread': False, 'timeout': 30} if DATABASE_URL.startswith('sqlite') else {}, pool_pre_ping=True)
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
    title = Column(String, nullable=False)
    status = Column(String, default='queued', index=True)
    created = Column(Float, default=time.time)
    updated = Column(Float, default=time.time)
    settings = Column(Text, nullable=False)
    policy = Column(Text, nullable=False)
    calls = Column(Integer, default=0)
    tokens = Column(Integer, default=0)
    error = Column(Text, default='')

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
    prompt_version = Column(String, default='1.0')
    input_hash = Column(String)
    input = Column(Text)
    output = Column(Text, nullable=True)
    tokens = Column(Integer, default=0)
    status = Column(String, default='running')
    created = Column(Float, default=time.time)

def init(): Base.metadata.create_all(engine)

def emit(run_id, stage, message, model_id=None, payload=None):
    with Session.begin() as s:
        s.add(Event(run_id=run_id, model_id=model_id, stage=stage, message=message, payload=dump(payload or {})))
        r = s.get(Run, run_id)
        if r: r.updated = time.time()
