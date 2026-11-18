"""External HTTPS/access checks. Credentials and cookies are never printed."""
import argparse
import http.client
import http.cookies
import json
import ssl
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit
import socks
from ssh_client import ROOT, connect, execute
from public_settings import public_origin


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--restart-auth', action='store_true', help='Also verify session survival across an auth deployment restart')
    args = parser.parse_args()
    access = json.loads((ROOT / '.platform/public-demo-credentials.json').read_text())
    ssh = json.loads((ROOT / '.platform/server.json').read_text())
    address = urlsplit(access['url'])
    if access['url'] != public_origin():
        raise ValueError('This verification is scoped to the configured canonical public origin')
    context = ssl.create_default_context()
    certificate = None

    def request(path, method='GET', body=None, token=None, tls=True, hostname=None):
        nonlocal certificate
        hostname = hostname or address.hostname
        sock = socks.socksocket()
        if ssh.get('socks'):
            sock.set_proxy(socks.SOCKS5, ssh['socks']['host'], ssh['socks']['port'])
        sock.settimeout(15)
        sock.connect((hostname, 443 if tls else 80))
        if tls:
            sock = context.wrap_socket(sock, server_hostname=hostname)
            if hostname == address.hostname:
                certificate = sock.getpeercert()
        client = http.client.HTTPConnection(hostname, timeout=15)
        client.sock = sock
        headers = {}
        if token:
            headers['Cookie'] = '__Host-venturelens_session=' + token
        if method != 'GET':
            headers['Origin'] = access['url']
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        try:
            client.request(method, path, body=body, headers=headers)
            response = client.getresponse()
            return response.status, dict(response.headers), response.read()
        finally:
            client.close()

    status, headers, _ = request('/auth/login?next=%2F', hostname=ssh['host'])
    assert status == 308 and headers.get('Location') == access['url'] + '/auth/login?next=%2F'
    status, headers, _ = request('/', tls=False)
    assert status in (301, 302, 307, 308) and headers.get('Location') == access['url'] + '/'
    assert request('/api/runs')[0] == 401
    assert request('/')[0] == 303
    status, headers, page = request('/auth/login')
    assert status == 200 and b'Venture Lens' in page

    def login(role):
        body = urlencode({'username': access[role]['username'], 'password': access[role]['password'], 'next': '/'}).encode()
        status, headers, _ = request('/auth/login', 'POST', body)
        assert status == 303, 'Login rejected for ' + role
        cookies = http.cookies.SimpleCookie()
        cookies.load(headers['Set-Cookie'])
        assert all(flag in headers['Set-Cookie'] for flag in ['Secure', 'HttpOnly', 'SameSite=lax'])
        return cookies['__Host-venturelens_session'].value

    guest, owner = login('viewer'), login('owner')
    assert json.loads(request('/auth/session', token=guest)[2])['role'] == 'viewer'
    assert json.loads(request('/auth/session', token=owner)[2])['role'] == 'owner'
    runs = json.loads(request('/api/runs', token=guest)[2])
    assert len(runs) >= 2
    assert request('/api/runs/' + runs[0]['id'] + '/export', token=guest)[0] == 200
    # An invalid research body must reach validation for the owner, but never for the guest.
    assert request('/api/runs', 'POST', {'count': 1}, owner)[0] == 422
    assert request('/api/runs', 'POST', {'count': 1}, guest)[0] == 403
    if args.restart_auth:
        with connect() as client:
            execute(client, 'k3s kubectl -n venture-lens-demo rollout restart deployment/demo-access\n'
                    'k3s kubectl -n venture-lens-demo rollout status deployment/demo-access --timeout=120s')
        status, _, body = request('/auth/session', token=guest)
        assert status == 200, 'Guest session unavailable after rollout: HTTP ' + str(status)
        assert json.loads(body)['role'] == 'viewer'
        status, _, body = request('/auth/session', token=owner)
        assert status == 200, 'Owner session unavailable after rollout: HTTP ' + str(status)
        assert json.loads(body)['role'] == 'owner'
    assert request('/auth/logout', 'POST', b'', owner)[0] == 303
    assert request('/auth/session', token=owner)[0] == 401
    assert request('/auth/logout', 'POST', b'', guest)[0] == 303
    proof = {'url': access['url'], 'checked_at': datetime.now(timezone.utc).isoformat(),
             'tls_verified': True, 'certificate_expires': certificate['notAfter'],
             'certificate_san': certificate.get('subjectAltName'), 'http_redirects_https': True,
             'legacy_ip_redirects_to_domain_preserving_path_and_query': True,
             'unauthenticated_api_blocked': True, 'owner_and_guest_login': True, 'viewer_read_export_only': True,
             'owner_reaches_backend_validation': True, 'logout_revokes_cookie': True,
             'session_survives_auth_restart': args.restart_auth, 'runs_preserved': len(runs)}
    (ROOT / '.platform/public-external-report.json').write_text(json.dumps(proof, indent=2) + '\n')
    print(json.dumps(proof, indent=2))


if __name__ == '__main__':
    main()
