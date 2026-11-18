"""Provision owner/viewer accounts and their restricted PostgreSQL role.

Run after the API migration. No plaintext login password is uploaded or logged.
Existing accounts keep their password hashes and sessions on a repeat invocation.
"""
import argparse
import json
import secrets
import shlex
import sys
from urllib.parse import urlsplit
from ssh_client import ROOT, connect, execute
from public_settings import public_origin

sys.path.insert(0, str(ROOT))
from backend.demo_auth import password_hash, password_matches

ROLE_SCRIPT = '''import psycopg,os,sys,json
from psycopg import sql
params=json.load(sys.stdin);name=params['username'];password=params['password']
with psycopg.connect(os.environ['DATABASE_URL'].replace('postgresql+psycopg://','postgresql://'),autocommit=True) as db:
    with db.cursor() as cursor:
        cursor.execute("SELECT shobj_description(oid, 'pg_authid') FROM pg_roles WHERE rolname=%s",(name,));existing=cursor.fetchone()
        if existing and existing[0]!='Venture Lens public demo sessions':raise RuntimeError('Refusing to replace an unrelated role')
        cursor.execute(sql.SQL(('ALTER ROLE {}' if existing else 'CREATE ROLE {}')+' LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS PASSWORD {}').format(sql.Identifier(name),sql.Literal(password)))
        cursor.execute(sql.SQL("COMMENT ON ROLE {} IS 'Venture Lens public demo sessions'").format(sql.Identifier(name)))
        cursor.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(db.info.dbname),sql.Identifier(name)))
        cursor.execute(sql.SQL('GRANT USAGE ON SCHEMA public TO {}').format(sql.Identifier(name)))
        cursor.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE,DELETE ON demo_sessions,demo_login_limits TO {}').format(sql.Identifier(name)))
        cursor.execute(sql.SQL('GRANT SELECT,INSERT ON registered_users TO {}').format(sql.Identifier(name)))
        cursor.execute(sql.SQL('GRANT UPDATE (password_hash,role) ON registered_users TO {}').format(sql.Identifier(name)))
        cursor.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE ON account_security,password_recovery_requests TO {}').format(sql.Identifier(name)))
        cursor.execute(sql.SQL('GRANT INSERT ON account_security_events TO {}').format(sql.Identifier(name)))
'''


def private_permissions(path):
    import os
    import subprocess
    if os.name == 'nt':
        owner = os.environ['USERDOMAIN'] + '\\' + os.environ['USERNAME']
        subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r', owner + ':(F)'], check=True, capture_output=True)
    else:
        path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    rotation = parser.add_mutually_exclusive_group()
    rotation.add_argument('--rotate', action='store_true', help='Generate new owner and viewer passwords; then restart demo-access')
    rotation.add_argument('--rotate-owner', action='store_true', help='Operator recovery: rotate only the owner bootstrap password, then restart demo-access')
    args = parser.parse_args()
    credentials_path = ROOT / '.platform/public-demo-credentials.json'
    state_path = ROOT / '.platform/public-demo-secret.json'
    if args.rotate or not credentials_path.exists():
        credentials = {'url': public_origin(),
                       'owner': {'username': 'owner', 'password': secrets.token_urlsafe(18)},
                       'viewer': {'username': 'demo', 'password': secrets.token_urlsafe(18)}}
        credentials_path.write_text(json.dumps(credentials, ensure_ascii=False, indent=2) + '\n')
    else:
        credentials = json.loads(credentials_path.read_text())
        if args.rotate_owner:
            credentials['owner']['password'] = secrets.token_urlsafe(18)
    credentials['url'] = public_origin()
    credentials_path.write_text(json.dumps(credentials, ensure_ascii=False, indent=2) + '\n')
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    previous = json.loads(state.get('DEMO_AUTH_USERS', '{}'))
    users = {}
    for role, name in [('owner', 'Владелец проекта'), ('viewer', 'Гость')]:
        account = credentials[role]
        old_hash = previous.get(account['username'], {}).get('password_hash')
        encoded = old_hash if old_hash and password_matches(account['password'], old_hash) else password_hash(account['password'])
        users[account['username']] = {'role': role, 'name': name, 'password_hash': encoded}
    state['DEMO_AUTH_USERS'] = json.dumps(users, ensure_ascii=False)
    state.setdefault('AUTH_CONTEXT_SECRET', secrets.token_urlsafe(48))
    state.setdefault('DATABASE_URL', 'postgresql+psycopg://venturelens_access:' + secrets.token_urlsafe(24) + '@postgres:5432/venturelens')
    state_path.write_text(json.dumps(state, ensure_ascii=False))
    private_permissions(credentials_path)
    private_permissions(state_path)
    for role, filename in [('owner', 'public-demo-owner.txt'), ('viewer', 'public-demo-guest.txt')]:
        account = credentials[role]
        access_file = ROOT / '.platform' / filename
        access_file.write_text('Venture Lens\nURL: ' + credentials['url'] + '\nLogin: ' + account['username'] + '\nPassword: ' + account['password']
                               + '\nAccess: ' + ('owner' if role == 'owner' else 'read-only guest') + '\n', encoding='utf-8')
        private_permissions(access_file)
    parsed = urlsplit(state['DATABASE_URL'])
    with connect() as client:
        command = 'k3s kubectl -n venture-lens-demo exec -i deployment/api -- python -c ' + shlex.quote(ROLE_SCRIPT)
        stdin, stdout, stderr = client.exec_command(command)
        stdin.write(json.dumps({'username': parsed.username, 'password': parsed.password})); stdin.channel.shutdown_write()
        stdout.read(); stderr.read()
        if stdout.channel.recv_exit_status():
            raise RuntimeError('Session-role setup failed; credential output suppressed. Check that migration 0002_demo_access has completed.')
        doc = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'venture-lens-demo-access', 'namespace': 'venture-lens-demo'}, 'type': 'Opaque', 'stringData': state}
        stdin, stdout, stderr = client.exec_command('k3s kubectl apply -f -')
        stdin.write(json.dumps(doc)); stdin.channel.shutdown_write(); stdout.read(); stderr.read()
        if stdout.channel.recv_exit_status():
            raise RuntimeError('Access Secret provisioning failed; credential output suppressed')
    print('Public demo access configured. Credentials: .platform/public-demo-credentials.json')
    print('Restart demo-access after credential changes. No plaintext login password was sent to the VPS.')


if __name__ == '__main__':
    main()
