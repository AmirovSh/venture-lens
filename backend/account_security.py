"""Transactional password changes and administrator-assisted recovery."""
import hashlib
import secrets
import time
from urllib.parse import parse_qs
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import MetaData, Table, Column, String, Float, select, delete, update, func, case
from starlette.concurrency import run_in_threadpool
from .passwords import password_hash, password_matches, password_error

metadata = MetaData()
security = Table('account_security', metadata, Column('username', String(64), primary_key=True),
                 Column('bootstrap_version', String(64)), Column('password_hash', String(256)), Column('password_changed', Float))
recovery_requests = Table('password_recovery_requests', metadata, Column('id', String(32), primary_key=True),
    Column('username', String(64), nullable=False), Column('contact', String(200), nullable=False), Column('message', String(500), nullable=False),
    Column('status', String(16), nullable=False), Column('created', Float, nullable=False), Column('updated', Float, nullable=False),
    Column('handled_by', String(64)), Column('token_hash', String(64), unique=True), Column('expires', Float), Column('credential_version', String(64)))
SECURITY_WINDOW = 900
RESET_TTL = 900
security_events = Table('account_security_events', metadata, Column('id', String(32), primary_key=True),
    Column('actor', String(64)), Column('target', String(64), nullable=False), Column('action', String(32), nullable=False),
    Column('created', Float, nullable=False), Column('request_id', String(32)))


class CredentialError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def credential_version(user):
    return hashlib.sha256(user['password_hash'].encode()).hexdigest()


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class SecurityStoreMixin:
    # The gateway supplies these tables; roles and workspace IDs are never accepted from the caller.
    accounts_table = None
    sessions_table = None

    def _audit(self, connection, actor, target, action, request_id=None):
        connection.execute(security_events.insert().values(id=secrets.token_hex(16), actor=actor, target=target,
                           action=action, created=time.time(), request_id=request_id))

    def configured_user(self, username, configured):
        base = configured[username]
        with self.engine.connect() as connection:
            row = connection.execute(select(security).where(security.c.username == username)).mappings().first()
        override = row['password_hash'] if row and row['bootstrap_version'] == credential_version(base) else None
        return base | {'workspace': 'main', 'password_hash': override or base['password_hash']}

    def _locked_user(self, connection, username, configured):
        query = select(self.accounts_table).where(self.accounts_table.c.username == username)
        if connection.dialect.name == 'postgresql':
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        if username in configured:
            base = configured[username]
            if base['role'] == 'viewer':
                return None
            version = credential_version(base)
            connection.execute(insert(security).values(username=username, bootstrap_version=version).on_conflict_do_nothing())
            query = select(security).where(security.c.username == username)
        if connection.dialect.name == 'postgresql':
            query = query.with_for_update()
        row = connection.execute(query).mappings().first()
        if not row:
            return None
        if username not in configured:
            return dict(row)
        if row['bootstrap_version'] != version:
            connection.execute(update(security).where(security.c.username == username).values(bootstrap_version=version, password_hash=None))
            return base | {'workspace': 'main'}
        return base | {'workspace': 'main', 'password_hash': row['password_hash'] or base['password_hash']}

    def _replace_password(self, connection, username, encoded, now, configured):
        if username in configured:
            connection.execute(update(security).where(security.c.username == username).values(password_hash=encoded, password_changed=now))
        else:
            connection.execute(update(self.accounts_table).where(self.accounts_table.c.username == username).values(password_hash=encoded))
        connection.execute(delete(self.sessions_table).where(self.sessions_table.c.username == username))
        connection.execute(update(recovery_requests).where(recovery_requests.c.username == username, recovery_requests.c.status.in_(['pending','approved'])).values(status='revoked', token_hash=None, updated=now))

    def change_password(self, username, current_password, encoded, configured, now):
        with self.engine.begin() as connection:
            user = self._locked_user(connection, username, configured)
            if not user or not password_matches(current_password, user['password_hash']):
                raise CredentialError('Текущий пароль неверен.')
            self._replace_password(connection, username, encoded, now, configured)
            self._audit(connection, username, username, 'password_changed')

    def request_recovery(self, username, contact, message, configured, now):
        with self.engine.begin() as c:
            user = self._locked_user(c, username, configured)
            if not user: return
            outstanding = c.scalar(select(recovery_requests.c.id).where(recovery_requests.c.username == username,
                (recovery_requests.c.status == 'pending') | ((recovery_requests.c.status == 'approved') & (recovery_requests.c.expires > now))))
            if not outstanding:
                c.execute(recovery_requests.insert().values(id=secrets.token_hex(16), username=username, contact=contact, message=message, status='pending', created=now, updated=now))

    def list_requests(self):
        with self.engine.connect() as c:
            rows = c.execute(select(recovery_requests).order_by(case((recovery_requests.c.status.in_(['pending','approved']),0),else_=1),recovery_requests.c.created.desc()).limit(250)).mappings().all()
        fields = ['id','username','contact','message','status','created','updated','handled_by','expires']
        return [{key:row[key] for key in fields} for row in rows]

    def list_users(self):
        t = self.accounts_table
        with self.engine.connect() as c:
            return [dict(row) for row in c.execute(select(t.c.username,t.c.name,t.c.role,t.c.created).order_by(t.c.created.desc()).limit(200)).mappings()]

    def _admin_target(self, c, actor, target, password, configured):
        # Stable lock order prevents reciprocal administrator requests from deadlocking.
        users = {name:self._locked_user(c,name,configured) for name in sorted({actor,target})}
        admin = users[actor]
        if not admin or admin['role'] not in ('owner','admin'): raise CredentialError('Недостаточно прав.',403)
        if not password_matches(password,admin['password_hash']): raise CredentialError('Текущий пароль администратора неверен.')
        if target in configured: raise CredentialError('Владелец и общий гостевой аккаунт защищены. Восстановление владельца выполняет оператор VPS.',403)
        if not users[target]: raise CredentialError('Аккаунт не найден.',404)
        return admin,users[target]

    def set_role(self, actor, target, role, password, configured):
        if role not in ('member','admin'): raise CredentialError('Недопустимая роль.',422)
        with self.engine.begin() as c:
            admin,_ = self._admin_target(c,actor,target,password,configured)
            if admin['role']!='owner': raise CredentialError('Назначать администраторов может только владелец.',403)
            c.execute(update(self.accounts_table).where(self.accounts_table.c.username==target).values(role=role))
            self._audit(c,actor,target,'role:'+role)

    def handle_recovery(self, actor, request_id, action, password, verified, configured, now, expected_updated=None):
        if action not in ('approve','deny'): raise CredentialError('Недопустимое действие.',422)
        if action=='approve' and verified is not True: raise CredentialError('Подтвердите проверку личности вне формы запроса.',422)
        with self.engine.begin() as c:
            target = c.scalar(select(recovery_requests.c.username).where(recovery_requests.c.id==request_id))
            if not target: raise CredentialError('Запрос не найден.',404)
            _,user = self._admin_target(c,actor,target,password,configured)
            query = select(recovery_requests).where(recovery_requests.c.id==request_id)
            if c.dialect.name=='postgresql': query=query.with_for_update()
            row = c.execute(query).mappings().one()
            if expected_updated is None or row['updated'] != expected_updated: raise CredentialError('Запрос уже изменён другим администратором. Обновите список.',409)
            if row['status'] not in ('pending','approved'): raise CredentialError('Запрос уже закрыт.',409)
            if action=='deny':
                c.execute(update(recovery_requests).where(recovery_requests.c.id==request_id).values(status='denied',token_hash=None,handled_by=actor,updated=now))
                self._audit(c,actor,target,'recovery_denied',request_id)
                return None
            token=secrets.token_urlsafe(32)
            c.execute(update(recovery_requests).where(recovery_requests.c.username==target,recovery_requests.c.id!=request_id,recovery_requests.c.status.in_(['pending','approved'])).values(status='revoked',token_hash=None,updated=now))
            c.execute(update(recovery_requests).where(recovery_requests.c.id==request_id).values(status='approved',token_hash=token_hash(token),expires=now+RESET_TTL,credential_version=credential_version(user),handled_by=actor,updated=now))
            self._audit(c,actor,target,'recovery_approved',request_id)
        return token

    def reset_password(self, token, encoded, configured, now):
        invalid='Ссылка недействительна или истекла. Запросите новую у администратора.'
        if not 32<=len(token)<=128: raise CredentialError(invalid)
        digest=token_hash(token)
        with self.engine.begin() as c:
            target=c.scalar(select(recovery_requests.c.username).where(recovery_requests.c.token_hash==digest))
            if not target or target in configured: raise CredentialError(invalid)
            user=self._locked_user(c,target,configured)
            query=select(recovery_requests).where(recovery_requests.c.token_hash==digest)
            if c.dialect.name=='postgresql': query=query.with_for_update()
            row=c.execute(query).mappings().first()
            if not user or not row or row['status']!='approved' or row['expires']<=now or row['credential_version']!=credential_version(user): raise CredentialError(invalid)
            c.execute(update(recovery_requests).where(recovery_requests.c.id==row['id']).values(status='used',token_hash=None,updated=now))
            self._replace_password(c,target,encoded,now,configured)
            self._audit(c,None,target,'password_reset',row['id'])


def install_security_routes(app,page,cookie_name):
    def identity(request):
        return app.state.store.identity(request.cookies.get(cookie_name),app.state.config.users,time.time())

    async def limited(request,username,action,window=900,maximum=10):
        address=request.headers.get('x-forwarded-for',request.client.host if request.client else 'unknown')
        first=await run_in_threadpool(app.state.store.attempt,action+':ip:'+address,time.time(),window,maximum)
        second=await run_in_threadpool(app.state.store.attempt,action+':user:'+username,time.time(),window,maximum)
        return first and second

    async def fields(request,json_body=False):
        body=bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body)>4096: raise CredentialError('Слишком большой запрос.',413)
        if json_body:
            import json
            result=json.loads(body)
            if not isinstance(result,dict) or any(not isinstance(v,(str,bool)) for v in result.values()): raise CredentialError('Проверьте введённые данные.')
            return result
        return {k:v[0] for k,v in parse_qs(body.decode(),max_num_fields=6).items()}

    def json_error(error):
        return JSONResponse({'detail':str(error) if isinstance(error,CredentialError) else 'Проверьте введённые данные.'},getattr(error,'status',400))

    @app.get('/auth/security')
    def security_info(request:Request):
        who=identity(request)
        return {'username':who['username'],'role':who['role'],'can_manage':who['role']!='viewer'} if who else JSONResponse({'detail':'Войдите в сервис'},401)

    @app.post('/auth/password')
    async def change_password(request:Request):
        if request.headers.get('origin')!=app.state.config.origin: return JSONResponse({'detail':'Недопустимый источник запроса'},403)
        who=await run_in_threadpool(identity,request)
        if not who: return JSONResponse({'detail':'Войдите в сервис'},401)
        if who['role']=='viewer': return JSONResponse({'detail':'Пароль общего гостевого аккаунта меняет владелец сервиса.'},403)
        if not await limited(request,who['username'],'security'): return JSONResponse({'detail':'Слишком много попыток. Попробуйте через 15 минут.'},429,headers={'Retry-After':'900'})
        try:
            data=await fields(request,True);current=data.get('current_password','');new=data.get('new_password','')
            if not isinstance(current,str) or not 1<=len(current)<=1024: raise CredentialError('Введите текущий пароль.',422)
            error=password_error(new,data.get('password_confirm',''))
            if error or new==current: raise CredentialError(error or 'Новый пароль должен отличаться от текущего.',422)
            encoded=await run_in_threadpool(password_hash,new)
            await run_in_threadpool(app.state.store.change_password,who['username'],current,encoded,app.state.config.users,time.time())
        except (ValueError,UnicodeError) as error: return json_error(error)
        response=JSONResponse({'detail':'Пароль изменён. Войдите снова.','redirect':'/auth/login?notice=password_changed'})
        response.delete_cookie(cookie_name,path='/',secure=True,httponly=True,samesite='lax');return response

    @app.get('/auth/recover',response_class=HTMLResponse)
    def recover_page(): return HTMLResponse(page(recovering=True))

    @app.post('/auth/recover')
    async def recover(request:Request):
        if request.headers.get('origin')!=app.state.config.origin: return JSONResponse({'detail':'Недопустимый источник запроса'},403)
        try:
            data=await fields(request);username=data.get('username','').strip().lower();contact=data.get('contact','').strip();message=data.get('message','').strip()
            if not 1<=len(username)<=64 or not 3<=len(contact)<=200 or len(message)>500: raise CredentialError('Укажите логин и способ связи (3–200 символов). Сообщение — до 500 символов.',422)
            if not await limited(request,username,'recover-request',3600,3): return HTMLResponse(page('Слишком много запросов. Попробуйте через час.',recovering=True),429,headers={'Retry-After':'3600'})
            await run_in_threadpool(app.state.store.request_recovery,username,contact,message,app.state.config.users,time.time())
        except (ValueError,UnicodeError) as error: return HTMLResponse(page(str(error) if isinstance(error,CredentialError) else 'Проверьте введённые данные.',recovering=True),getattr(error,'status',400))
        return HTMLResponse(page(recovering=True,notice='recovery_requested'),202)

    @app.get('/auth/reset',response_class=HTMLResponse)
    def reset_page(): return HTMLResponse(page(resetting=True))

    @app.post('/auth/reset')
    async def reset(request:Request):
        if request.headers.get('origin')!=app.state.config.origin: return JSONResponse({'detail':'Недопустимый источник запроса'},403)
        try:
            data=await fields(request)
            if not await limited(request,token_hash(data.get('token','')[:128]),'reset'): return HTMLResponse(page('Слишком много попыток. Попробуйте через 15 минут.',resetting=True),429,headers={'Retry-After':'900'})
            error=password_error(data.get('password',''),data.get('password_confirm',''))
            if error: raise CredentialError(error,422)
            encoded=await run_in_threadpool(password_hash,data['password'])
            await run_in_threadpool(app.state.store.reset_password,data.get('token',''),encoded,app.state.config.users,time.time())
        except (ValueError,UnicodeError) as error: return HTMLResponse(page(str(error) if isinstance(error,CredentialError) else 'Проверьте введённые данные.',resetting=True,reset_token=data.get('token','') if 'data' in locals() else ''),getattr(error,'status',400))
        response=RedirectResponse('/auth/login?notice=password_reset',303);response.delete_cookie(cookie_name,path='/',secure=True,httponly=True,samesite='lax');return response

    @app.get('/auth/admin/users')
    @app.get('/auth/admin/recovery-requests')
    def admin_lists(request:Request):
        who=identity(request)
        if not who: return JSONResponse({'detail':'Войдите в сервис'},401)
        if who['role'] not in ('owner','admin'): return JSONResponse({'detail':'Недостаточно прав'},403)
        if request.url.path.endswith('/users'):
            if who['role']!='owner': return JSONResponse({'detail':'Раздел доступен владельцу'},403)
            return app.state.store.list_users()
        return app.state.store.list_requests()

    @app.post('/auth/admin/users/{target}/role')
    @app.post('/auth/admin/recovery-requests/{target}/handle')
    async def admin_action(request:Request,target:str):
        if request.headers.get('origin')!=app.state.config.origin: return JSONResponse({'detail':'Недопустимый источник запроса'},403)
        who=await run_in_threadpool(identity,request)
        if not who: return JSONResponse({'detail':'Войдите в сервис'},401)
        if who['role'] not in ('owner','admin'): return JSONResponse({'detail':'Недостаточно прав'},403)
        if not await limited(request,who['username'],'admin-action'): return JSONResponse({'detail':'Слишком много попыток. Попробуйте через 15 минут.'},429,headers={'Retry-After':'900'})
        try:
            data=await fields(request,True);current=data.get('current_password','')
            if not isinstance(current,str) or not 1<=len(current)<=1024: raise CredentialError('Введите текущий пароль администратора.',422)
            if request.url.path.endswith('/role'):
                await run_in_threadpool(app.state.store.set_role,who['username'],target,data.get('role',''),current,app.state.config.users)
                return {'detail':'Права обновлены.'}
            token=await run_in_threadpool(app.state.store.handle_recovery,who['username'],target,data.get('action',''),current,data.get('identity_verified'),app.state.config.users,time.time(),float(data.get('expected_updated','nan')))
            if token: return JSONResponse({'detail':'Передайте ссылку владельцу аккаунта по проверенному каналу.','reset_url':app.state.config.origin+'/auth/reset#'+token,'expires_in':RESET_TTL})
            return {'detail':'Запрос отклонён.'}
        except (ValueError,UnicodeError) as error: return json_error(error)
