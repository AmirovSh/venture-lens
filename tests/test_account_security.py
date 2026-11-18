"""Persistent password changes, owner/admin permissions and one-time recovery."""
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import select, update
from backend.demo_auth import COOKIE
from backend.account_security import recovery_requests, security_events, token_hash
from test_registration import registered, store, register, write, PASSWORD, ORIGIN

NEW_PASSWORD = 'another long passphrase 456'


def login(client, username, password=PASSWORD):
    return client.post('/auth/login', data={'username': username, 'password': password}, headers={'Origin': ORIGIN})


def request_reset(client, username='alice', contact='qa-contact', **extra):
    return client.post('/auth/recover', data={'username': username, 'contact': contact} | extra, headers={'Origin': ORIGIN})


def approve(client, request_id, **extra):
    row=next(row for row in client.get('/auth/admin/recovery-requests').json() if row['id']==request_id)
    return write(client, '/auth/admin/recovery-requests/' + request_id + '/handle',
                 {'action': 'approve', 'identity_verified': True, 'current_password': PASSWORD, 'expected_updated': str(row['updated'])} | extra)


def reset(client, token, password=NEW_PASSWORD):
    return client.post('/auth/reset', data={'token': token, 'password': password, 'password_confirm': password}, headers={'Origin': ORIGIN})


def pending(registered):
    client, database, _, _ = registered
    assert register(client).status_code == 303
    old_token = client.cookies.get(COOKIE)
    assert request_reset(client).status_code == 202
    assert login(client, 'owner').status_code == 303
    request_id = client.get('/auth/admin/recovery-requests').json()[0]['id']
    return client, database, request_id, old_token


def test_password_change_revokes_every_session_and_preserves_workspace(registered):
    client, database, _, _ = registered
    register(client)
    who = client.get('/auth/session').json()
    old = client.cookies.get(COOKIE)
    other = database.issue('alice', database.user('alice', {}), time.time())
    assert write(client, '/auth/password', {'current_password': 'wrong', 'new_password': NEW_PASSWORD, 'password_confirm': NEW_PASSWORD}).status_code == 400
    assert write(client, '/auth/password', {'current_password': PASSWORD, 'new_password': NEW_PASSWORD, 'password_confirm': 'different'}).status_code == 422
    response = write(client, '/auth/password', {'current_password': PASSWORD, 'new_password': NEW_PASSWORD, 'password_confirm': NEW_PASSWORD})
    assert response.status_code == 200
    assert database.identity(old, {}, time.time()) is None and database.identity(other, {}, time.time()) is None
    assert login(client, 'alice').status_code == 401
    assert login(client, 'alice', NEW_PASSWORD).status_code == 303
    assert client.get('/auth/session').json()['workspace'] == who['workspace']


def test_owner_changes_password_and_bootstrap_rotation_recovers_owner(registered):
    client, database, _, _ = registered
    login(client, 'owner')
    assert write(client, '/auth/password', {'current_password': PASSWORD, 'new_password': NEW_PASSWORD, 'password_confirm': NEW_PASSWORD}).status_code == 200
    assert login(client, 'owner').status_code == 401
    assert login(client, 'owner', NEW_PASSWORD).status_code == 303
    # Operator rotation of the configured bootstrap hash supersedes the DB override.
    from backend.passwords import password_hash
    changed = {'owner': {'role': 'owner', 'password_hash': password_hash('operator emergency password')}}
    assert database.user('owner', changed)['password_hash'] == changed['owner']['password_hash']


def test_request_is_generic_private_and_cannot_replace_contact(registered):
    client, database, _, _ = registered
    register(client)
    assert 'Забыли пароль?' in client.get('/auth/login').text or client.get('/auth/login').status_code == 303
    known = request_reset(client)
    unknown = request_reset(client, 'nonexistent')
    assert known.status_code == unknown.status_code == 202 and known.text == unknown.text
    assert request_reset(client, contact='attacker-new-contact').status_code == 202
    with database.engine.connect() as c:
        rows = c.execute(select(recovery_requests)).mappings().all()
        assert len(rows) == 1 and rows[0]['contact'] == 'qa-contact' and rows[0]['token_hash'] is None
    assert client.get('/auth/admin/recovery-requests').status_code == 403
    assert client.get('/auth/admin/users').status_code == 403
    assert client.get('/auth/recover').status_code == 200
    assert client.get('/auth/reset').status_code == 200


def test_admin_link_single_use_and_all_old_sessions_revoked(registered):
    client, database, request_id, old = pending(registered)
    assert approve(client, request_id, identity_verified=False).status_code == 422
    result = approve(client, request_id)
    assert result.status_code == 200 and result.json()['expires_in'] == 900
    token = result.json()['reset_url'].split('#')[1]
    assert len(token) == 43
    with database.engine.connect() as c:
        row = c.execute(select(recovery_requests)).mappings().one()
        assert row['token_hash'] == token_hash(token) and token not in str(dict(row))
    assert token not in client.get('/auth/admin/recovery-requests').text
    assert reset(client, token).status_code == 303
    assert reset(client, token).status_code == 400
    assert database.identity(old, {}, time.time()) is None
    assert login(client, 'alice').status_code == 401
    assert login(client, 'alice', NEW_PASSWORD).status_code == 303
    with database.engine.connect() as c:
        assert c.execute(select(recovery_requests.c.status)).scalar() == 'used'
        assert c.execute(select(security_events.c.action).where(security_events.c.target == 'alice')).scalars().all() == ['recovery_approved', 'password_reset']


def test_only_owner_assigns_admin_and_admin_can_handle_requests(registered):
    client, database, _, _ = registered
    register(client)
    register(client, 'helper')
    assert write(client, '/auth/admin/users/alice/role', {'role': 'admin', 'current_password': PASSWORD}).status_code == 403
    login(client, 'owner')
    assert write(client, '/auth/admin/users/helper/role', {'role': 'owner', 'current_password': PASSWORD}).status_code == 422
    assert write(client, '/auth/admin/users/helper/role', {'role': 'admin', 'current_password': PASSWORD}).status_code == 200
    assert request_reset(client).status_code == 202
    login(client, 'helper')
    identity = client.get('/auth/session').json()
    assert identity['role'] == 'admin' and identity['workspace'] != 'main'
    assert client.get('/api/runs').json() == []
    assert client.get('/api/account').json()['limits']['daily'] == 3
    assert write(client, '/auth/admin/users/alice/role', {'role': 'admin', 'current_password': PASSWORD}).status_code == 403
    request_id = client.get('/auth/admin/recovery-requests').json()[0]['id']
    assert approve(client, request_id).status_code == 200
    login(client, 'owner')
    assert write(client, '/auth/admin/users/helper/role', {'role': 'member', 'current_password': PASSWORD}).status_code == 200
    login(client, 'helper')
    assert client.get('/auth/admin/recovery-requests').status_code == 403


def test_expired_reissued_denied_and_password_changed_links_fail(registered):
    client, database, request_id, _ = pending(registered)
    first = approve(client, request_id).json()['reset_url'].split('#')[1]
    second = approve(client, request_id).json()['reset_url'].split('#')[1]
    assert reset(client, first).status_code == 400
    with database.engine.begin() as c:
        c.execute(update(recovery_requests).values(expires=time.time()-1))
    assert reset(client, second).status_code == 400
    third = approve(client, request_id).json()['reset_url'].split('#')[1]
    login(client, 'alice')
    assert write(client, '/auth/password', {'current_password': PASSWORD, 'new_password': NEW_PASSWORD, 'password_confirm': NEW_PASSWORD}).status_code == 200
    assert reset(client, third).status_code == 400


def test_guest_and_owner_protection_csrf_and_limits(registered):
    client, _, _, _ = registered
    assert client.post('/auth/recover', data={}, headers={'Origin': 'https://evil.test'}).status_code == 403
    assert client.post('/auth/reset', data={}, headers={'Origin': 'https://evil.test'}).status_code == 403
    login(client, 'demo')
    assert write(client, '/auth/password', {'current_password': PASSWORD, 'new_password': NEW_PASSWORD, 'password_confirm': NEW_PASSWORD}).status_code == 403
    assert request_reset(client, 'owner').status_code == 202
    login(client, 'owner')
    request_id = client.get('/auth/admin/recovery-requests').json()[0]['id']
    assert approve(client, request_id).status_code == 403
    assert client.post('/auth/password', json={}, headers={'Origin': 'https://evil.test'}).status_code == 403
    assert request_reset(client, 'unknown-1').status_code == 202
    assert request_reset(client, 'unknown-2').status_code == 202
    assert request_reset(client, 'unknown-3').status_code == 429


def test_denial_and_stale_admin_action_cannot_replace_valid_link(registered):
    client, _, request_id, _ = pending(registered)
    row = client.get('/auth/admin/recovery-requests').json()[0]
    token = approve(client, request_id).json()['reset_url'].split('#')[1]
    assert approve(client, request_id, expected_updated=str(row['updated'])).status_code == 409
    assert approve(client, request_id, action='deny').status_code == 200
    assert reset(client, token).status_code == 400


def test_postgresql_reset_link_consumed_once_under_concurrency(registered, store):
    if store.kw['bind'].dialect.name != 'postgresql': pytest.skip('Requires PostgreSQL row locks')
    client, _, request_id, _ = pending(registered)
    token = approve(client, request_id).json()['reset_url'].split('#')[1]
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: reset(client, token).status_code, range(2)))
    assert sorted(statuses) == [303, 400]
