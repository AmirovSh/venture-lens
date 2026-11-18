"""Exercise real gateway -> API -> persistent workspace boundaries."""
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from backend import db, settings
from backend.app import app as api_app
from backend.auth_context import sign_identity, verify_identity
from backend.demo_auth import Config, Store, accounts, create_app, metadata, password_hash, password_matches, COOKIE
from test_pipeline import store, seed

ORIGIN = 'https://demo.test'
SECRET = 'test-only-identity-signing-secret-123456789'
PASSWORD = 'a long test passphrase 123'


@pytest.fixture
def registered(store, monkeypatch):
    engine = store.kw['bind']
    metadata.create_all(engine)
    monkeypatch.setattr(settings, 'env', settings.env | {'AUTH_CONTEXT_SECRET': SECRET, 'AUTH_CONTEXT_REQUIRED': 'true', 'ALLOWED_ORIGINS': ORIGIN})
    configured = {'owner': {'role': 'owner', 'password_hash': password_hash(PASSWORD)},
                  'demo': {'role': 'viewer', 'password_hash': password_hash(PASSWORD)}}
    config = Config(ORIGIN, configured, 'unused', context_secret=SECRET, registration_enabled=True)
    database = Store(engine)
    forwarded = []
    backend = TestClient(api_app)
    def upstream(request):
        forwarded.append(request)
        response = backend.request(request.method, request.url.raw_path.decode(), content=request.content, headers=dict(request.headers))
        return httpx.Response(response.status_code, headers=dict(response.headers), stream=httpx.ByteStream(response.content))
    gateway = create_app(config, database, httpx.MockTransport(upstream))
    with TestClient(gateway, base_url=ORIGIN, follow_redirects=False) as client:
        yield client, database, forwarded, backend
    backend.close()


def register(client, username='alice', **changes):
    fields = {'username': username, 'name': 'Алиса', 'password': PASSWORD, 'password_confirm': PASSWORD}
    fields.update(changes)
    return client.post('/auth/register', data=fields, headers={'Origin': ORIGIN})


def write(client, path, body=None, method='POST', **headers):
    return client.request(method, path, json=body or {}, headers={'Origin': ORIGIN} | headers)


def test_registration_persists_hashed_account_and_signs_in(registered):
    client, database, forwarded, _ = registered
    assert 'Создать аккаунт' in client.get('/auth/login').text
    page = client.get('/auth/register')
    assert page.status_code == 200 and 'через запрос администратору' in page.text
    assert '{{' not in page.text
    response = register(client, ' Alice ')
    assert response.status_code == 303 and response.headers['location'] == '/'
    identity = client.get('/auth/session').json()
    assert identity['username'] == 'alice' and identity['role'] == 'member' and identity['workspace'] != 'main'
    assert identity['limits'] == {'active': 1, 'daily': 3}
    with database.engine.connect() as connection:
        user = connection.execute(select(accounts)).mappings().one()
        assert password_matches(PASSWORD, user['password_hash']) and PASSWORD not in user['password_hash']
    assert client.get('/api/runs').json() == []
    assert verify_identity(forwarded[-1].headers, SECRET)['workspace'] == identity['workspace']
    token = client.cookies.get(COOKIE)
    assert write(client, '/auth/logout').status_code == 303
    assert database.identity(token, {}, time.time()) is None
    login = client.post('/auth/login', data={'username': 'ALICE', 'password': PASSWORD}, headers={'Origin': ORIGIN})
    assert login.status_code == 303
    assert client.get('/auth/session').json()['workspace'] == identity['workspace']


@pytest.mark.parametrize('changes,message', [
    ({'username': 'aa'}, 'Логин'), ({'username': 'admin@example.com'}, 'Логин'),
    ({'name': 'a'}, 'Укажите имя'), ({'password': 'short', 'password_confirm': 'short'}, 'Пароль должен'),
    ({'password_confirm': 'different value'}, 'не совпадают')])
def test_invalid_registration_has_specific_errors_and_no_account(registered, changes, message):
    client, database, _, _ = registered
    response = register(client, **changes)
    assert response.status_code == 422 and message in response.text and PASSWORD not in response.text
    with database.engine.connect() as connection:
        assert connection.execute(select(accounts)).first() is None


def test_registration_rejects_reserved_duplicate_csrf_and_spoofed_roles(registered):
    client, _, _, _ = registered
    assert client.post('/auth/register', data={}, headers={'Origin': 'https://evil.test'}).status_code == 403
    assert register(client, 'OWNER').status_code == 409
    assert register(client).status_code == 303
    workspace = client.get('/auth/session').json()['workspace']
    assert register(client, 'ALICE').status_code == 409
    assert client.get('/auth/session').json()['workspace'] == workspace
    assert write(client, '/api/runs', {'workspace': 'main'}).status_code == 422


def test_two_users_and_demo_are_isolated_for_all_paths(registered, store, monkeypatch):
    client, _, _, _ = registered
    seed(store)
    assert register(client).status_code == 303
    alice = client.get('/auth/session').json()
    assert client.get('/api/runs').json() == []
    created = write(client, '/api/runs', {'title': 'Private Alice', 'count': 10})
    assert created.status_code == 201
    run_id = created.json()['id']
    checksum = hashlib.sha256(b'private source').hexdigest()
    with store.begin() as session:
        session.add(db.Model(id='private-model', run_id=run_id, name='Private', company='Private', data=db.dump({'sources': [{'sha256': checksum}]})))
        session.add(db.Event(run_id=run_id, stage='private', message='private event'))
    monkeypatch.setattr('backend.storage.get_source', lambda _: 'private source')
    assert client.get('/api/sources/' + checksum).text == 'private source'
    assert write(client, '/api/runs/run/cancel').status_code == 404
    assert write(client, '/api/models/model/decision', {'decision': 'shortlist', 'note': ''}, method='PATCH').status_code == 404
    write(client, '/auth/logout')
    assert register(client, 'bob').status_code == 303
    assert client.get('/auth/session').json()['workspace'] != alice['workspace']
    forged = sign_identity({'workspace': 'main', 'role': 'owner'}, SECRET)
    assert client.get('/api/runs', headers=forged).json() == []  # Gateway discards caller assertions.
    for suffix in ['', '/export', '/events', '/audit']:
        assert client.get('/api/runs/' + run_id + suffix).status_code == 404
    for suffix in ['/cancel', '/retry']:
        assert write(client, '/api/runs/' + run_id + suffix).status_code == 404
    assert write(client, '/api/models/private-model/decision', {'decision': 'shortlist', 'note': ''}, method='PATCH').status_code == 404
    assert client.get('/api/sources/' + checksum).status_code == 404
    write(client, '/auth/logout')
    assert client.post('/auth/login', data={'username': 'demo', 'password': PASSWORD}, headers={'Origin': ORIGIN}).status_code == 303
    assert [r['id'] for r in client.get('/api/runs').json()] == ['run']
    assert client.get('/api/runs/' + run_id + '/export').status_code == 404
    assert write(client, '/api/runs', {'title': 'No access'}).status_code == 403


def test_member_active_and_rolling_daily_limits_persist(registered, store):
    client, _, _, _ = registered
    register(client)
    workspace = client.get('/auth/session').json()['workspace']
    for index in range(3):
        response = write(client, '/api/runs', {'title': 'Quota research ' + str(index), 'count': 10})
        assert response.status_code == 201
        run_id = response.json()['id']
        with store() as session:
            assert session.get(db.Run, run_id).workspace == workspace
            assert session.scalar(select(db.Job).where(db.Job.run_id == run_id)) is not None
        assert write(client, '/api/runs', {'title': 'Too many active'}).status_code == 429
        assert write(client, '/api/runs/' + run_id + '/cancel').status_code == 200
    assert write(client, '/api/runs', {'title': 'Over daily quota'}).status_code == 429
    limits = client.get('/api/account').json()['limits']
    assert limits['remaining'] == 0 and limits['used'] == 3 and limits['running'] == 0 and limits['next_slot_at'] > time.time()
    with store.begin() as session:
        oldest = session.scalar(select(db.Run).where(db.Run.workspace == workspace).order_by(db.Run.created))
        oldest.created = time.time() - 86401
    assert write(client, '/api/runs', {'title': 'New rolling slot'}).status_code == 201


def test_registration_throttle_shared_between_replicas(registered):
    client, database, _, _ = registered
    for _ in range(5):
        assert register(client, 'bad!').status_code == 422
    response = register(client)
    assert response.status_code == 429 and response.headers['retry-after'] == '3600'
    assert not Store(database.engine).attempt('register:testclient', time.time(), 3600, 5)
    assert Store(database.engine).attempt('register:other-address', time.time(), 3600, 5)


def test_api_rejects_missing_forged_expired_identity_and_viewer_writes(registered):
    _, _, _, backend = registered
    assert backend.get('/api/runs').status_code == 401
    assert backend.get('/api/runs', headers={'x-venture-identity': 'invalid', 'x-venture-signature': '0' * 64}).status_code == 401
    expired = sign_identity({'workspace': 'main', 'role': 'owner'}, SECRET, time.time() - 70)
    assert backend.get('/api/runs', headers=expired).status_code == 401
    headers = sign_identity({'workspace': 'main', 'role': 'viewer'}, SECRET)
    assert backend.post('/api/runs', json={}, headers=headers).status_code == 403
    assert backend.get('/api/live').status_code == 200


def test_postgresql_concurrent_member_admission(registered, store):
    if store.kw['bind'].dialect.name != 'postgresql':
        pytest.skip('Concurrent admission is verified against PostgreSQL')
    _, _, _, backend = registered
    def launch(_):
        headers = sign_identity({'workspace': 'concurrent-member', 'role': 'member'}, SECRET)
        return backend.post('/api/runs', json={'title': 'Concurrent request'}, headers=headers).status_code
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(launch, range(4)))
    assert sorted(results) == [201, 429, 429, 429]
