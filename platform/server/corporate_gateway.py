"""Restricted internal LLM adapter. VPN sidecar owns the network connection."""
import hmac
import json
import os
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPSHandler

MAX_BODY = 500_000
MAX_RESPONSE = 8_000_000
SLOTS = threading.BoundedSemaphore(2)


class UpstreamHealth:
    def __init__(self, upstream, context):
        self.target = urlsplit(upstream)
        self.context = context
        self.checked = 0
        self.ready = False
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            if time.monotonic() - self.checked < 2:
                return self.ready
            try:
                with socket.create_connection((self.target.hostname, self.target.port or 443), timeout=3) as raw:
                    with self.context.wrap_socket(raw, server_hostname=self.target.hostname):
                        pass
                self.ready = True
            except (OSError, ssl.SSLError):
                self.ready = False
            self.checked = time.monotonic()
            return self.ready


def tls_context():
    context = ssl.create_default_context()
    context.load_verify_locations(os.environ.get('LLM_CA_FILE', '/run/tls/corporate-ca.pem'))
    context.load_cert_chain(os.environ.get('LLM_CLIENT_CERT_FILE', '/run/tls/client.pem'),
                           os.environ.get('LLM_CLIENT_KEY_FILE', '/run/tls/client.key'))
    return context


class Gateway(BaseHTTPRequestHandler):
    def reply(self, status, body, retry=None):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        if retry is not None:
            self.send_header('Retry-After', str(retry))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == '/live':
            self.reply(200, {'status': 'alive'})
        elif self.path == '/ready':
            if self.server.upstream_ready():
                self.reply(200, {'status': 'ready'})
            else:
                self.reply(503, {'status': 'waiting', 'code': 'CORPORATE_CONNECTION_UNAVAILABLE'}, 10)
        else:
            self.reply(404, {'error': 'not_found'})

    def do_POST(self):
        if self.path != '/v1/chat/completions':
            self.reply(404, {'error': 'not_found'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= MAX_BODY or self.headers.get('Transfer-Encoding'):
                self.reply(413, {'error': 'request_too_large'})
                return
            # Drain a bounded request before replying so closing an HTTP/1.0
            # connection does not reset the socket while a client is sending.
            self.connection.settimeout(10)
            raw = self.rfile.read(length)
            if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + self.server.token):
                self.reply(401, {'error': 'unauthorized'})
                return
            payload = json.loads(raw)
            if not isinstance(payload, dict) or payload.get('model') not in self.server.models:
                self.reply(400, {'error': 'model_not_allowed'})
                return
        except (ValueError, UnicodeError, TimeoutError):
            self.reply(400, {'error': 'invalid_request'})
            return
        if not self.server.upstream_ready():
            self.reply(503, {'error': 'corporate_connection_unavailable'}, 10)
            return
        if not SLOTS.acquire(blocking=False):
            self.reply(429, {'error': 'adapter_busy'}, 3)
            return
        try:
            request = Request(self.server.upstream + '/chat/completions', data=json.dumps(payload).encode(),
                              headers={'Authorization': 'Bearer ' + self.server.api_key, 'Content-Type': 'application/json'})
            opener = build_opener(ProxyHandler({}), HTTPSHandler(context=self.server.tls))
            with opener.open(request, timeout=150) as response:
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise ValueError('upstream_response_too_large')
                result = json.loads(raw)
            self.reply(200, result)
        except HTTPError as error:
            retry = error.headers.get('Retry-After', '10')
            self.reply(error.code, {'error': 'corporate_gateway_unavailable'}, retry)
        except (OSError, URLError, ValueError):
            self.reply(503, {'error': 'corporate_gateway_unavailable'}, 10)
        finally:
            SLOTS.release()

    def log_message(self, *_):
        # Never log request headers, prompt bodies, API keys or upstream bodies.
        pass


def make_server(address=('0.0.0.0', 8080)):
    upstream = os.environ['LLM_BASE_URL'].rstrip('/')
    if urlsplit(upstream).scheme != 'https':
        raise ValueError('Corporate upstream must use HTTPS')
    token = os.environ['GATEWAY_TOKEN']
    api_key = os.environ['LLM_API_KEY']
    if not token or not api_key:
        raise ValueError('Both gateway and corporate credentials are required')
    context = tls_context()
    server = ThreadingHTTPServer(address, Gateway)
    server.upstream, server.token, server.api_key, server.tls = upstream, token, api_key, context
    server.upstream_ready = UpstreamHealth(upstream, context)
    server.models = set(os.environ.get('LLM_ALLOWED_MODELS', 'qwen3.8-27b,deepseek-v4-flash').split(','))
    return server


if __name__ == '__main__':
    make_server().serve_forever()
