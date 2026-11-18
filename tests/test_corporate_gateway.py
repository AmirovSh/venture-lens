"""Exercise the internal gateway over real local HTTP connections."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import http.client
import json
from pathlib import Path
import ssl
import threading
import urllib.error
import urllib.request
import pytest

spec = importlib.util.spec_from_file_location('corporate_gateway', Path(__file__).parents[1] / 'platform/server/corporate_gateway.py')
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


@contextmanager
def serve(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def adapter(upstream):
    server = ThreadingHTTPServer(('127.0.0.1', 0), gateway.Gateway)
    server.upstream = upstream
    server.token = 'internal-token'
    server.api_key = 'corporate-token'
    server.models = {'allowed-model'}
    server.tls = ssl.create_default_context()
    return server


def request(url, body, token='internal-token'):
    req = urllib.request.Request(url + '/v1/chat/completions', data=body,
                                 headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open(req, timeout=3)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.code, dict(response.headers), json.load(response)


def test_gateway_rejects_untrusted_requests_before_upstream():
    with serve(adapter('http://127.0.0.1:1')) as url:
        assert request(url, b'{}', token='wrong')[0] == 401
        assert request(url, b'{')[0] == 400
        assert request(url, b'[]')[0] == 400
        assert request(url, b'{"model":"other"}')[0] == 400
        conn = http.client.HTTPConnection('127.0.0.1', int(url.rsplit(':', 1)[1]), timeout=3)
        conn.putrequest('POST', '/v1/chat/completions')
        conn.putheader('Content-Length', str(gateway.MAX_BODY + 1))
        conn.putheader('Authorization', 'Bearer internal-token')
        conn.endheaders()
        assert conn.getresponse().status == 413
        conn.close()


def test_gateway_preserves_payload_and_uses_separate_corporate_credential():
    captured = []
    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self):
            captured.append((self.path, self.headers['Authorization'], json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"choices":[{"message":{"content":"OK"}}]}')
        def log_message(self, *_): pass
    payload = {'model': 'allowed-model', 'messages': [{'role': 'user', 'content': 'original prompt'}], 'response_format': {'type': 'json_schema'}}
    with serve(ThreadingHTTPServer(('127.0.0.1', 0), Upstream)) as upstream:
        with serve(adapter(upstream)) as url:
            status, _, result = request(url, json.dumps(payload).encode())
    assert status == 200 and result['choices'][0]['message']['content'] == 'OK'
    assert captured == [('/chat/completions', 'Bearer corporate-token', payload)]


def test_gateway_sanitizes_upstream_errors_and_preserves_retry_after():
    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(503)
            self.send_header('Retry-After', '42')
            self.end_headers()
            self.wfile.write(b'private upstream diagnostic')
        def log_message(self, *_): pass
    with serve(ThreadingHTTPServer(('127.0.0.1', 0), Upstream)) as upstream:
        with serve(adapter(upstream)) as url:
            status, headers, result = request(url, b'{"model":"allowed-model"}')
    assert status == 503 and headers['Retry-After'] == '42'
    assert result == {'error': 'corporate_gateway_unavailable'}


def test_gateway_connection_failure_releases_concurrency_slot():
    with serve(adapter('http://127.0.0.1:1')) as url:
        for _ in range(3):
            status, headers, result = request(url, b'{"model":"allowed-model"}')
            assert status == 503 and headers['Retry-After'] == '10'


def test_gateway_configuration_rejects_plain_http(monkeypatch):
    monkeypatch.setenv('LLM_BASE_URL', 'http://example.invalid/v1')
    with pytest.raises(ValueError, match='HTTPS'):
        gateway.make_server(('127.0.0.1', 0))
