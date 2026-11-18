import hashlib
import time
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool
from backend.demo_auth import Config, Store, COOKIE, TTL, WINDOW, MAX_ATTEMPTS, create_app, metadata, password_hash, sessions, safe_next


@pytest.fixture
def access():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    metadata.create_all(engine)
    users = {'owner': {'role': 'owner', 'name': 'Владелец', 'password_hash': password_hash('owner-test-password')},
             'demo': {'role': 'viewer', 'name': 'Гость', 'password_hash': password_hash('viewer-test-password')}}
    config = Config('https://demo.test', users, 'sqlite://')
    forwarded = []
    def upstream(request):
        forwarded.append(request)
        return httpx.Response(200, headers={'content-type': 'application/json'}, stream=httpx.ByteStream(b'{"status":"upstream"}'))
    store = Store(engine)
    app = create_app(config, store, httpx.MockTransport(upstream))
    with TestClient(app, base_url='https://demo.test', follow_redirects=False) as client:
        yield client, store, config, forwarded
    engine.dispose()


def login(client, username='owner', password='owner-test-password', next='/'):
    return client.post('/auth/login', data={'username': username, 'password': password, 'next': next}, headers={'Origin': 'https://demo.test'})


def test_public_pages_and_api_require_login(access):
    client, _, _, forwarded = access
    assert client.get('/').status_code == 303
    assert client.get('/api/runs').status_code == 401
    assert client.get('/auth/session').status_code == 401
    assert client.get('/auth/login').status_code == 200
    assert not forwarded


def test_owner_session_cookie_proxy_and_csrf(access):
    client, store, _, forwarded = access
    response = login(client)
    assert response.status_code == 303
    cookie = response.headers['set-cookie']
    assert all(flag in cookie for flag in ['Secure', 'HttpOnly', 'SameSite=lax', 'Path=/'])
    token = client.cookies.get(COOKIE)
    with store.engine.connect() as connection:
        row = connection.execute(select(sessions)).mappings().one()
        assert row['token_hash'] == hashlib.sha256(token.encode()).hexdigest() and row['token_hash'] != token
    assert client.get('/auth/session').json()['role'] == 'owner'
    assert client.post('/api/runs', json={}, headers={'Origin': 'https://attacker.invalid'}).status_code == 403
    assert not forwarded
    assert client.post('/api/runs', json={}, headers={'Origin': 'https://demo.test'}).status_code == 200
    assert forwarded[-1].url.path == '/api/runs'
    assert 'cookie' not in forwarded[-1].headers
    assert 'authorization' not in forwarded[-1].headers


def test_viewer_can_read_export_but_cannot_mutate(access):
    client, _, _, forwarded = access
    assert login(client, 'demo', 'viewer-test-password').status_code == 303
    assert client.get('/api/runs/example/export').status_code == 200
    before = len(forwarded)
    for path, method in [('/api/runs', 'POST'), ('/api/models/example/decision', 'PATCH'), ('/api/runs/example/cancel', 'POST')]:
        response = client.request(method, path, json={}, headers={'Origin': 'https://demo.test'})
        assert response.status_code == 403
    assert len(forwarded) == before


def test_logout_revokes_cookie_in_database(access):
    client, store, config, _ = access
    login(client)
    token = client.cookies.get(COOKIE)
    assert client.post('/auth/logout', headers={'Origin': config.origin}).status_code == 303
    assert store.identity(token, config.users, time.time()) is None
    assert client.get('/auth/session', headers={'Cookie': COOKIE + '=' + token}).status_code == 401


def test_expiry_and_password_rotation_invalidate_session(access):
    _, store, config, _ = access
    now = time.time()
    token = store.issue('owner', config.users['owner'], now)
    assert store.identity(token, config.users, now)
    assert store.identity(token, config.users, now + TTL + 1) is None
    config.users['owner']['password_hash'] = password_hash('new-password')
    assert store.identity(token, config.users, now) is None


def test_login_rate_limit_is_shared_and_survives_store_recreation(access):
    _, store, _, _ = access
    now = time.time()
    for _ in range(MAX_ATTEMPTS):
        assert store.attempt('203.0.113.10', now)
    another_replica = Store(store.engine)
    assert not another_replica.attempt('203.0.113.10', now)
    assert another_replica.attempt('203.0.113.11', now)
    assert another_replica.attempt('203.0.113.10', now + WINDOW + 1)


def test_login_rejects_cross_origin_and_escapes_reflected_username(access):
    client, _, _, _ = access
    assert client.post('/auth/login', data={'username': 'owner', 'password': 'owner-test-password'}, headers={'Origin': 'https://attacker.invalid'}).status_code == 403
    response = login(client, '<script>alert(1)</script>', 'bad-password')
    assert response.status_code == 401
    assert '<script>alert(1)</script>' not in response.text
    assert '&lt;script&gt;' in response.text


def test_database_failure_denies_access_without_leaking_details(access, monkeypatch):
    from sqlalchemy.exc import OperationalError
    client, store, _, forwarded = access
    def unavailable(*_):
        raise OperationalError('private database configuration', {}, RuntimeError('must-not-leak'))
    monkeypatch.setattr(store, 'identity', unavailable)
    response = client.get('/api/runs')
    assert response.status_code == 503 and response.headers['Retry-After'] == '5'
    assert 'must-not-leak' not in response.text and 'private database' not in response.text
    assert not forwarded


@pytest.mark.parametrize('target', ['https://attacker.invalid', '//attacker.invalid', '/%2f/attacker.invalid', '/\\attacker.invalid', '/%0d%0aLocation:evil', '/auth/logout'])
def test_open_redirect_targets_are_rejected(target):
    assert safe_next(target) == '/'
