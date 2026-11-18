"""Public demonstration gateway: PostgreSQL sessions, roles, CSRF and login limits.

The original application stays private. Only this gateway is reachable from TLS edge.
"""
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import hmac
import html
import json
import os
from pathlib import Path
import secrets
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy import MetaData, Table, Column, String, Float, Integer, create_engine, select, delete, case
from sqlalchemy.exc import SQLAlchemyError
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

COOKIE = '__Host-venturelens_session'
TTL = 8 * 60 * 60
WINDOW = 300
MAX_ATTEMPTS = 10
MAX_BODY = 1_000_000
metadata = MetaData()
sessions = Table('demo_sessions', metadata, Column('token_hash', String(64), primary_key=True),
                 Column('username', String(64), nullable=False), Column('credential_version', String(64), nullable=False),
                 Column('expires', Float, nullable=False, index=True))
limits = Table('demo_login_limits', metadata, Column('key_hash', String(64), primary_key=True),
               Column('window_start', Float, nullable=False, index=True), Column('attempts', Integer, nullable=False))


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600_000).hex()
    return 'pbkdf2_sha256$600000$' + salt + '$' + digest


def password_matches(password, encoded):
    try:
        algorithm, iterations, salt, expected = encoded.split('$')
        if algorithm != 'pbkdf2_sha256' or int(iterations) != 600_000:
            return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(iterations)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def safe_next(value):
    decoded = unquote(unquote(value))
    if not decoded.startswith('/') or decoded.startswith('//') or '\\' in decoded or any(ord(c) < 32 for c in decoded) or decoded.startswith('/auth/'):
        return '/'
    return value


@dataclass
class Config:
    origin: str
    users: dict
    database_url: str
    upstream: str = 'http://web:3100'

    @classmethod
    def environment(cls):
        return cls(os.environ['PUBLIC_ORIGIN'].rstrip('/'), json.loads(os.environ['DEMO_AUTH_USERS']), os.environ['DATABASE_URL'])

    def validate(self):
        parsed = urlsplit(self.origin)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.path or not self.users:
            raise ValueError('Public HTTPS origin and configured users are required')
        for username, user in self.users.items():
            if not username or len(username) > 64 or user.get('role') not in ('owner', 'viewer') or not user.get('password_hash', '').startswith('pbkdf2_sha256$600000$'):
                raise ValueError('Invalid demo account configuration')


class Store:
    def __init__(self, engine):
        self.engine = engine

    def ready(self):
        with self.engine.connect() as connection:
            connection.execute(select(sessions.c.token_hash).limit(1))

    def attempt(self, address, now):
        # One atomic counter shared by all replicas. No in-memory bypass on restart.
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        insert = pg_insert if self.engine.dialect.name == 'postgresql' else sqlite_insert
        key = hashlib.sha256(address.encode()).hexdigest()
        expired = limits.c.window_start <= now - WINDOW
        statement = insert(limits).values(key_hash=key, window_start=now, attempts=1).on_conflict_do_update(
            index_elements=[limits.c.key_hash], set_={
                'window_start': case((expired, now), else_=limits.c.window_start),
                'attempts': case((expired, 1), else_=limits.c.attempts + 1),
            }).returning(limits.c.attempts)
        with self.engine.begin() as connection:
            count = connection.execute(statement).scalar_one()
            connection.execute(delete(limits).where(limits.c.window_start < now - WINDOW * 2))
            connection.execute(delete(sessions).where(sessions.c.expires <= now))
        return count <= MAX_ATTEMPTS

    def issue(self, username, user, now):
        token = secrets.token_urlsafe(32)
        with self.engine.begin() as connection:
            connection.execute(sessions.insert().values(token_hash=hashlib.sha256(token.encode()).hexdigest(), username=username,
                               credential_version=hashlib.sha256(user['password_hash'].encode()).hexdigest(), expires=now + TTL))
        return token

    def identity(self, token, users, now):
        if not token or len(token) > 128:
            return None
        with self.engine.connect() as connection:
            row = connection.execute(select(sessions).where(sessions.c.token_hash == hashlib.sha256(token.encode()).hexdigest(), sessions.c.expires > now)).mappings().first()
        if not row:
            return None
        user = users.get(row['username'])
        if not user or not hmac.compare_digest(row['credential_version'], hashlib.sha256(user['password_hash'].encode()).hexdigest()):
            return None
        return {'username': row['username'], 'name': user.get('name', row['username']), 'role': user['role'], 'expires': row['expires']}

    def revoke(self, token):
        with self.engine.begin() as connection:
            connection.execute(delete(sessions).where(sessions.c.token_hash == hashlib.sha256(token.encode()).hexdigest()))


def login_page(message='', username='', next_path='/'):
    template = (Path(__file__).parent / 'templates/demo_login.html').read_text(encoding='utf-8')
    return template.replace('{{ERROR}}', '<div class="error" role="alert">' + html.escape(message) + '</div>' if message else '') \
                   .replace('{{USERNAME}}', html.escape(username, quote=True)).replace('{{NEXT}}', html.escape(safe_next(next_path), quote=True))


def create_app(config=None, store=None, transport=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.config = config or Config.environment()
        app.state.config.validate()
        app.state.store = store or Store(create_engine(app.state.config.database_url, pool_pre_ping=True, connect_args={'connect_timeout': 5}))
        app.state.store.ready()
        async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=5), trust_env=False, follow_redirects=False, transport=transport) as client:
            app.state.client = client
            yield
        if store is None:
            app.state.store.engine.dispose()

    app = FastAPI(title='Venture Lens demo access', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.middleware('http')
    async def guarded(request, call_next):
        try:
            response = await call_next(request)
        except SQLAlchemyError:
            response = JSONResponse({'detail': 'Проверка доступа временно недоступна. Попробуйте позже.'}, 503, headers={'Retry-After': '5'})
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @app.get('/auth/live')
    def live():
        return {'status': 'alive'}

    @app.get('/auth/ready')
    def ready():
        app.state.store.ready()
        return {'status': 'ready'}

    @app.get('/auth/login', response_class=HTMLResponse)
    def login(request: Request, next: str = '/'):
        identity = app.state.store.identity(request.cookies.get(COOKIE), app.state.config.users, time.time())
        return RedirectResponse(safe_next(next), 303) if identity else HTMLResponse(login_page(next_path=next))

    @app.post('/auth/login')
    async def authenticate(request: Request):
        cfg, database = app.state.config, app.state.store
        if request.headers.get('origin') != cfg.origin:
            return JSONResponse({'detail': 'Недопустимый источник запроса'}, 403)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                return JSONResponse({'detail': 'Слишком большой запрос'}, 413)
        try:
            fields = parse_qs(body.decode(), max_num_fields=4)
        except (ValueError, UnicodeError):
            return HTMLResponse(login_page('Проверьте введённые данные.'), 400)
        username = fields.get('username', [''])[0].strip()[:64]
        password = fields.get('password', [''])[0]
        next_path = safe_next(fields.get('next', ['/'])[0])
        # NetworkPolicy accepts callers only from Caddy, which overwrites this header.
        address = request.headers.get('x-forwarded-for', request.client.host if request.client else 'unknown')
        if not await run_in_threadpool(database.attempt, address, time.time()):
            return HTMLResponse(login_page('Слишком много попыток. Попробуйте через 5 минут.', username, next_path), 429, headers={'Retry-After': str(WINDOW)})
        user = cfg.users.get(username)
        expected = (user or next(iter(cfg.users.values())))['password_hash']
        valid = await run_in_threadpool(password_matches, password, expected)
        if not user or not valid:
            return HTMLResponse(login_page('Неверное имя пользователя или пароль.', username, next_path), 401)
        token = await run_in_threadpool(database.issue, username, user, time.time())
        response = RedirectResponse(next_path, 303)
        response.set_cookie(COOKIE, token, max_age=TTL, secure=True, httponly=True, samesite='lax', path='/')
        return response

    @app.get('/auth/session')
    def session(request: Request):
        identity = app.state.store.identity(request.cookies.get(COOKIE), app.state.config.users, time.time())
        return identity if identity else JSONResponse({'detail': 'Войдите в демонстрацию'}, 401)

    @app.post('/auth/logout')
    def logout(request: Request):
        if request.headers.get('origin') != app.state.config.origin:
            return JSONResponse({'detail': 'Недопустимый источник запроса'}, 403)
        app.state.store.revoke(request.cookies.get(COOKIE, ''))
        response = RedirectResponse('/auth/login', 303)
        response.delete_cookie(COOKIE, path='/', secure=True, httponly=True, samesite='lax')
        return response

    @app.api_route('/{path:path}', methods=['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
    async def proxy(request: Request, path: str):
        if path.startswith('auth/'):
            return JSONResponse({'detail': 'Страница не найдена'}, 404)
        identity = await run_in_threadpool(app.state.store.identity, request.cookies.get(COOKIE), app.state.config.users, time.time())
        if not identity:
            if path.startswith('api/'):
                return JSONResponse({'detail': 'Сессия завершена. Войдите снова.'}, 401)
            return RedirectResponse('/auth/login?next=' + quote(safe_next(str(request.url.path) + ('?' + request.url.query if request.url.query else '')), safe=''), 303)
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            if request.headers.get('origin') != app.state.config.origin:
                return JSONResponse({'detail': 'Недопустимый источник запроса'}, 403)
            if identity['role'] != 'owner':
                return JSONResponse({'detail': 'Гостевой доступ разрешает только просмотр'}, 403)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY:
                return JSONResponse({'detail': 'Слишком большой запрос'}, 413)
        hop = {'host', 'connection', 'keep-alive', 'transfer-encoding', 'te', 'trailer', 'upgrade', 'proxy-authenticate', 'proxy-authorization', 'cookie', 'authorization', 'content-length'}
        headers = {key: value for key, value in request.headers.items() if key.lower() not in hop}
        target = app.state.config.upstream.rstrip('/') + '/' + path
        if request.url.query:
            target += '?' + request.url.query
        try:
            upstream = await app.state.client.send(app.state.client.build_request(request.method, target, headers=headers, content=bytes(body)), stream=True)
        except httpx.HTTPError:
            return JSONResponse({'detail': 'Приложение временно недоступно. Повторите запрос.'}, 503, headers={'Retry-After': '5'})
        response_headers = {key: value for key, value in upstream.headers.items() if key.lower() not in hop | {'set-cookie'}}
        return StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code, headers=response_headers, background=BackgroundTask(upstream.aclose))

    return app


app = create_app()
