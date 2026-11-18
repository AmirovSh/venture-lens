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
import re
from pathlib import Path
import secrets
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy import MetaData, Table, Column, String, Float, Integer, create_engine, select, delete, case, func, text
from sqlalchemy.exc import SQLAlchemyError, IntegrityError
from .auth_context import sign_identity
from .passwords import password_hash, password_matches
from .schemas import OnboardingPreferences
from .account_security import metadata, SecurityStoreMixin, install_security_routes, security
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

COOKIE = '__Host-venturelens_session'
TTL = 8 * 60 * 60
WINDOW = 300
MAX_ATTEMPTS = 10
MAX_BODY = 1_000_000
sessions = Table('demo_sessions', metadata, Column('token_hash', String(64), primary_key=True),
                 Column('username', String(64), nullable=False), Column('credential_version', String(64), nullable=False),
                 Column('expires', Float, nullable=False, index=True))
limits = Table('demo_login_limits', metadata, Column('key_hash', String(64), primary_key=True),
               Column('window_start', Float, nullable=False, index=True), Column('attempts', Integer, nullable=False))
accounts = Table('registered_users', metadata, Column('username', String(64), primary_key=True),
                 Column('name', String(60), nullable=False), Column('password_hash', String(256), nullable=False),
                 Column('workspace', String(64), nullable=False, unique=True), Column('created', Float, nullable=False),
                 Column('role', String(16), nullable=False, server_default='member', default='member'))
preferences = Table('user_preferences',metadata,Column('username',String(64),primary_key=True),
                    Column('onboarding_enabled',Integer,nullable=False,server_default='1'),
                    Column('onboarding_seen_version',Integer,nullable=False,server_default='0'),Column('updated',Float,nullable=False))


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
    context_secret: str = ''
    registration_enabled: bool = False
    security_enabled: bool = True

    @classmethod
    def environment(cls):
        return cls(os.environ['PUBLIC_ORIGIN'].rstrip('/'), json.loads(os.environ['DEMO_AUTH_USERS']), os.environ['DATABASE_URL'],
                   context_secret=os.environ.get('AUTH_CONTEXT_SECRET', ''),
                   registration_enabled=os.environ.get('REGISTRATION_ENABLED') == 'true',
                   security_enabled=os.environ.get('ACCOUNT_SECURITY_ENABLED') == 'true')

    def validate(self):
        parsed = urlsplit(self.origin)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.path or not self.users:
            raise ValueError('Public HTTPS origin and configured users are required')
        if self.registration_enabled and len(self.context_secret) < 32:
            raise ValueError('Registration requires a private identity signing secret')
        if sum(user.get('role') == 'owner' for user in self.users.values()) != 1:
            raise ValueError('Exactly one configured owner is required')
        for username, user in self.users.items():
            if not username or len(username) > 64 or user.get('role') not in ('owner', 'viewer') or not user.get('password_hash', '').startswith('pbkdf2_sha256$600000$'):
                raise ValueError('Invalid demo account configuration')


class Store(SecurityStoreMixin):
    accounts_table = accounts
    sessions_table = sessions
    def __init__(self, engine):
        self.engine = engine

    def ready(self):
        with self.engine.connect() as connection:
            connection.execute(select(sessions.c.token_hash).limit(1))
            connection.execute(select(accounts.c.username).limit(1))
            connection.execute(select(security.c.username).limit(1))

    def attempt(self, address, now, window=WINDOW, maximum=MAX_ATTEMPTS):
        # One atomic counter shared by all replicas. No in-memory bypass on restart.
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        insert = pg_insert if self.engine.dialect.name == 'postgresql' else sqlite_insert
        key = hashlib.sha256(address.encode()).hexdigest()
        expired = limits.c.window_start <= now - window
        statement = insert(limits).values(key_hash=key, window_start=now, attempts=1).on_conflict_do_update(
            index_elements=[limits.c.key_hash], set_={
                'window_start': case((expired, now), else_=limits.c.window_start),
                'attempts': case((expired, 1), else_=limits.c.attempts + 1),
            }).returning(limits.c.attempts)
        with self.engine.begin() as connection:
            count = connection.execute(statement).scalar_one()
            connection.execute(delete(limits).where(limits.c.window_start < now - 7200))
            connection.execute(delete(sessions).where(sessions.c.expires <= now))
        return count <= maximum

    def user(self, username, configured):
        if username in configured:
            return self.configured_user(username, configured)
        with self.engine.connect() as connection:
            row = connection.execute(select(accounts).where(accounts.c.username == username)).mappings().first()
        return dict(row) if row else None

    def register(self, username, name, encoded, now):
        user = {'username': username, 'name': name, 'password_hash': encoded,
                'workspace': secrets.token_hex(16), 'created': now}
        with self.engine.begin() as connection:
            if self.engine.dialect.name == 'postgresql':
                connection.execute(text('SELECT pg_advisory_xact_lock(862319004)'))
            if connection.scalar(select(func.count()).select_from(accounts)) >= 200:
                raise ValueError('Регистрация временно приостановлена: достигнут лимит демонстрационного сервера.')
            connection.execute(accounts.insert().values(**user))
        return user | {'role': 'member'}

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
        user = self.user(row['username'], users)
        if not user or not hmac.compare_digest(row['credential_version'], hashlib.sha256(user['password_hash'].encode()).hexdigest()):
            return None
        return {'username': row['username'], 'name': user.get('name', row['username']), 'role': user['role'],
                'workspace': user['workspace'], 'expires': row['expires'],
                'limits': {'active': 1, 'daily': 3} if user['role'] in ('member', 'admin') else None}

    def revoke(self, token):
        with self.engine.begin() as connection:
            connection.execute(delete(sessions).where(sessions.c.token_hash == hashlib.sha256(token.encode()).hexdigest()))


def login_page(message='', username='', next_path='/', registration_enabled=False, registering=False, name='', recovering=False, resetting=False, notice='', reset_token='', security_enabled=True):
    template = (Path(__file__).parent / 'templates/demo_login.html').read_text(encoding='utf-8')
    esc = lambda value: html.escape(value, quote=True)
    action, title, intro, submit = '/auth/login', 'Добро пожаловать', 'Войдите, чтобы продолжить свои исследования или посмотреть демонстрацию.', 'Войти в рабочее пространство'
    fields = '<input type="hidden" name="next" value="' + esc(safe_next(next_path)) + '">'
    if registering:
        action, title, intro, submit = '/auth/register', 'Создайте своё пространство', 'Личные исследования и проверяемые источники. Одно исследование одновременно, до трёх новых запусков за сутки.', 'Создать аккаунт'
        fields += '<label for="name">Как вас зовут</label><input id="name" name="name" autocomplete="name" minlength="2" maxlength="60" required value="' + esc(name) + '" placeholder="Ваше имя">'
    if not resetting:
        fields += '<label for="username">Имя пользователя</label><input id="username" name="username" autocomplete="username" maxlength="64" required value="' + esc(username) + '" placeholder="Имя пользователя">'
    if registering:
        fields += '<small>3–32 символа: латиница, цифры, точка, дефис и подчёркивание.</small>'
    if recovering:
        action, title, intro, submit = '/auth/recover', 'Верните доступ к своим исследованиям', 'Укажите логин и способ связи. Администратор проверит запрос и передаст ссылку для смены пароля.', 'Отправить запрос'
        fields += '<label for="contact">Как с вами связаться</label><input id="contact" name="contact" minlength="3" maxlength="200" required placeholder="Например, ваш Telegram или email"><label for="message">Сообщение администратору · необязательно</label><textarea id="message" name="message" maxlength="500" rows="3" placeholder="Что поможет проверить, что аккаунт принадлежит вам"></textarea>'
    else:
        fresh = registering or resetting
        if resetting:
            action, title, intro, submit = '/auth/reset', 'Задайте новый пароль', 'Ссылка от администратора действует 15 минут и используется один раз. После сохранения все сессии будут завершены.', 'Сохранить новый пароль'
            fields += '<input id="reset-token" type="hidden" name="token" value="' + esc(reset_token[:128]) + '">'
        fields += '<label for="password">' + ('Новый пароль' if resetting else 'Пароль') + '</label><input id="password" type="password" name="password" autocomplete="' + ('new-password' if fresh else 'current-password') + '" minlength="' + ('12' if fresh else '1') + '" maxlength="' + ('128' if fresh else '1024') + '" required placeholder="Введите пароль">'
        if fresh:
            fields += '<small>От 12 до 128 символов. Можно использовать длинную фразу.</small><label for="password_confirm">Повторите пароль</label><input id="password_confirm" type="password" name="password_confirm" autocomplete="new-password" minlength="12" maxlength="128" required placeholder="Повторите пароль">'
    notices = {'password_changed': 'Пароль изменён. Все сессии завершены — войдите с новым паролем.', 'password_reset': 'Пароль восстановлен. Все сессии завершены — войдите с новым паролем.', 'recovery_requested': 'Если такой аккаунт существует, запрос передан администраторам. Дождитесь связи и одноразовой ссылки. Повторный запрос не ускоряет обработку.'}
    form = '<form id="auth-form" method="post" action="' + action + '">' + fields + '<button type="submit">' + submit + ' <span aria-hidden="true">→</span></button></form>'
    if notice == 'recovery_requested': form = ''
    if registering:
        switch = '<p class="switch">Уже есть аккаунт? <a href="/auth/login">Войти</a></p>'
        note = 'Ваши исследования видны только вам. Пароль можно изменить в настройках аккаунта, а восстановить — через запрос администратору.'
    elif recovering or resetting:
        switch = '<p class="switch"><a href="/auth/login">Вернуться ко входу</a></p>'
        note = 'Контакт из заявки сам по себе не подтверждает личность. Администратор может запросить дополнительные сведения.' if recovering else 'Не передавайте эту ссылку другим людям. Если она истекла, попросите администратора создать новую.'
    else:
        switch = '<p class="switch"><a href="/auth/recover">Забыли пароль?</a></p>' if security_enabled else ''
        if registration_enabled: switch += '<p class="switch">Впервые здесь? <a href="/auth/register">Создать аккаунт</a></p>'
        note = 'Сессия действует 8 часов. Доступ к демонстрации — по реквизитам автора проекта.'
    script = ''
    if resetting:
        script = """<script>const field=document.getElementById('reset-token');const fragment=location.hash.slice(1);if(fragment){if(/^[A-Za-z0-9_-]{43}$/.test(fragment))field.value=fragment;history.replaceState(null,'',location.pathname);}if(!field.value){const alert=document.createElement('div');alert.className='error';alert.setAttribute('role','alert');alert.textContent='Ссылка неполная. Откройте ссылку, которую передал администратор.';document.getElementById('auth-form').prepend(alert);document.querySelector('button[type=submit]').disabled=true;}</script>"""
    values = {'TITLE':title, 'PAGE_TITLE':'Восстановление пароля' if recovering or resetting else 'Регистрация' if registering else 'Вход', 'INTRO':intro,
        'ERROR':'<div class="error" role="alert">'+esc(message)+'</div>' if message else '',
        'NOTICE':'<div class="success" role="status">'+notices[notice]+'</div>' if notice in notices else '',
        'FORM':form,'SWITCH':switch,'NOTE':note,'SCRIPT':script}
    for key,value in values.items(): template=template.replace('{{'+key+'}}',value)
    return template


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

    def page(*args, **kwargs):
        return login_page(*args, registration_enabled=app.state.config.registration_enabled, security_enabled=app.state.config.security_enabled, **kwargs)

    def signed_in(username, user, next_path='/'):
        token = app.state.store.issue(username, user, time.time())
        response = RedirectResponse(next_path, 303)
        response.set_cookie(COOKIE, token, max_age=TTL, secure=True, httponly=True, samesite='lax', path='/')
        return response

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
    def login(request: Request, next: str = '/', notice: str = ''):
        identity = app.state.store.identity(request.cookies.get(COOKIE), app.state.config.users, time.time())
        return RedirectResponse(safe_next(next), 303) if identity else HTMLResponse(page(next_path=next, notice=notice))

    @app.get('/auth/register', response_class=HTMLResponse)
    def register_page(request: Request):
        if not app.state.config.registration_enabled:
            return JSONResponse({'detail': 'Регистрация пока недоступна'}, 404)
        identity = app.state.store.identity(request.cookies.get(COOKIE), app.state.config.users, time.time())
        return RedirectResponse('/', 303) if identity else HTMLResponse(page(registering=True))

    @app.post('/auth/register')
    async def register(request: Request):
        cfg, database = app.state.config, app.state.store
        if not cfg.registration_enabled:
            return JSONResponse({'detail': 'Регистрация пока недоступна'}, 404)
        if request.headers.get('origin') != cfg.origin:
            return JSONResponse({'detail': 'Недопустимый источник запроса'}, 403)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                return JSONResponse({'detail': 'Слишком большой запрос'}, 413)
        address = request.headers.get('x-forwarded-for', request.client.host if request.client else 'unknown')
        if not await run_in_threadpool(database.attempt, 'register:' + address, time.time(), 3600, 5):
            return HTMLResponse(page('Слишком много попыток регистрации. Попробуйте через час.', registering=True), 429, headers={'Retry-After': '3600'})
        try:
            fields = parse_qs(body.decode(), max_num_fields=6)
        except (ValueError, UnicodeError):
            return HTMLResponse(page('Проверьте введённые данные.', registering=True), 400)
        username = fields.get('username', [''])[0].strip().lower()
        name = fields.get('name', [''])[0].strip()
        password = fields.get('password', [''])[0]
        confirmation = fields.get('password_confirm', [''])[0]
        def invalid(message, status=422):
            return HTMLResponse(page(message, username, registering=True, name=name), status)
        if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{2,31}', username):
            return invalid('Логин: от 3 до 32 символов, латинские буквы, цифры, точка, дефис или подчёркивание.')
        if not 2 <= len(name) <= 60 or any(ord(c) < 32 for c in name):
            return invalid('Укажите имя длиной от 2 до 60 символов.')
        if not 12 <= len(password) <= 128 or not password.strip():
            return invalid('Пароль должен содержать от 12 до 128 символов.')
        if not hmac.compare_digest(password.encode(), confirmation.encode()):
            return invalid('Пароли не совпадают.')
        if username in {key.lower() for key in cfg.users}:
            return invalid('Этот логин уже занят. Выберите другой.', 409)
        encoded = await run_in_threadpool(password_hash, password)
        try:
            user = await run_in_threadpool(database.register, username, name, encoded, time.time())
        except IntegrityError:
            return invalid('Этот логин уже занят. Выберите другой.', 409)
        except ValueError as error:
            return invalid(str(error), 429)
        return await run_in_threadpool(signed_in, username, user)

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
            return HTMLResponse(page('Проверьте введённые данные.'), 400)
        username = fields.get('username', [''])[0].strip().lower()[:64]
        password = fields.get('password', [''])[0]
        next_path = safe_next(fields.get('next', ['/'])[0])
        # NetworkPolicy accepts callers only from Caddy, which overwrites this header.
        address = request.headers.get('x-forwarded-for', request.client.host if request.client else 'unknown')
        if not await run_in_threadpool(database.attempt, address, time.time()):
            return HTMLResponse(page('Слишком много попыток. Попробуйте через 5 минут.', username, next_path), 429, headers={'Retry-After': str(WINDOW)})
        user = await run_in_threadpool(database.user, username, cfg.users)
        expected = (user or next(iter(cfg.users.values())))['password_hash']
        valid = await run_in_threadpool(password_matches, password, expected)
        if not user or not valid:
            return HTMLResponse(page('Неверное имя пользователя или пароль.', username, next_path), 401)
        return await run_in_threadpool(signed_in, username, user, next_path)

    @app.get('/auth/session')
    def session(request: Request):
        identity = app.state.store.identity(request.cookies.get(COOKIE), app.state.config.users, time.time())
        return identity if identity else JSONResponse({'detail': 'Войдите в демонстрацию'}, 401)

    @app.get('/auth/preferences')
    def get_preferences(request:Request):
        identity=app.state.store.identity(request.cookies.get(COOKIE),app.state.config.users,time.time())
        if not identity:return JSONResponse({'detail':'Войдите в сервис'},401)
        with app.state.store.engine.connect() as connection:
            row=connection.execute(select(preferences).where(preferences.c.username==identity['username'])).mappings().first()
        return {'onboarding_enabled':bool(row['onboarding_enabled']) if row else True,
                'onboarding_seen_version':row['onboarding_seen_version'] if row else 0}

    @app.patch('/auth/preferences')
    def set_preferences(body:OnboardingPreferences,request:Request):
        if request.headers.get('origin')!=app.state.config.origin:return JSONResponse({'detail':'Недопустимый источник запроса'},403)
        identity=app.state.store.identity(request.cookies.get(COOKIE),app.state.config.users,time.time())
        if not identity:return JSONResponse({'detail':'Войдите в сервис'},401)
        engine=app.state.store.engine
        insert=__import__('sqlalchemy.dialects.postgresql' if engine.dialect.name=='postgresql' else 'sqlalchemy.dialects.sqlite',fromlist=['insert']).insert
        values={'username':identity['username'],**body.model_dump(),'updated':time.time()}
        with engine.begin() as connection:
            connection.execute(insert(preferences).values(**values).on_conflict_do_update(index_elements=[preferences.c.username],set_={key:value for key,value in values.items() if key!='username'}))
        return body.model_dump()

    @app.post('/auth/logout')
    def logout(request: Request):
        if request.headers.get('origin') != app.state.config.origin:
            return JSONResponse({'detail': 'Недопустимый источник запроса'}, 403)
        app.state.store.revoke(request.cookies.get(COOKIE, ''))
        response = RedirectResponse('/auth/login', 303)
        response.delete_cookie(COOKIE, path='/', secure=True, httponly=True, samesite='lax')
        return response

    if (config.security_enabled if config else os.environ.get('ACCOUNT_SECURITY_ENABLED') == 'true'):
        install_security_routes(app, page, COOKIE)

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
            if identity['role'] not in ('owner', 'member', 'admin'):
                return JSONResponse({'detail': 'Гостевой доступ разрешает только просмотр'}, 403)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY:
                return JSONResponse({'detail': 'Слишком большой запрос'}, 413)
        hop = {'host', 'connection', 'keep-alive', 'transfer-encoding', 'te', 'trailer', 'upgrade', 'proxy-authenticate', 'proxy-authorization', 'cookie', 'authorization', 'content-length'}
        headers = {key: value for key, value in request.headers.items() if key.lower() not in hop and not key.lower().startswith('x-venture-')}
        if app.state.config.context_secret:
            headers.update(sign_identity(identity, app.state.config.context_secret))
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
