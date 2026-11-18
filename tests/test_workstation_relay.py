"""Exercise the private relay's HTTP contract without using corporate credentials."""
import importlib.util
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener

spec=importlib.util.spec_from_file_location('workstation_relay',Path(__file__).resolve().parents[1]/'platform/server/loopback_proxy.py')
relay=importlib.util.module_from_spec(spec);spec.loader.exec_module(relay)
opener=build_opener(ProxyHandler({}))


@contextmanager
def serving(handler):
    server=ThreadingHTTPServer(('127.0.0.1',0),handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield 'http://127.0.0.1:'+str(server.server_port)
    finally:server.shutdown();server.server_close();thread.join(timeout=2)


class Upstream(BaseHTTPRequestHandler):
    def do_GET(self):
        assert self.path=='/live'
        self.send_response(200);self.end_headers();self.wfile.write(b'{"status":"alive"}')

    def do_POST(self):
        assert self.path=='/v1/chat/completions'
        assert json.loads(self.rfile.read(int(self.headers['Content-Length'])))=={'model':'fixture'}
        token=self.headers.get('Authorization')
        status=200 if token=='Bearer fixture-private-token' else 401
        self.send_response(status);self.send_header('Retry-After','7');self.end_headers()
        self.wfile.write(b'{"choices":[{"message":{"content":"fixture"}}]}')

    def log_message(self,*_):pass


def test_private_relay_preserves_auth_payload_and_upstream_errors(monkeypatch):
    with serving(Upstream) as upstream,serving(relay.Proxy) as gateway:
        monkeypatch.setattr(relay,'UPSTREAM',upstream)
        with opener.open(gateway+'/ready',timeout=3) as response:assert json.load(response)=={'status':'alive'}
        body=json.dumps({'model':'fixture'}).encode()
        request=Request(gateway+'/v1/chat/completions',body,{'Authorization':'Bearer fixture-private-token'})
        with opener.open(request,timeout=3) as response:assert json.load(response)['choices'][0]['message']['content']=='fixture'
        try:opener.open(Request(gateway+'/v1/chat/completions',body),timeout=3)
        except HTTPError as error:
            assert error.code==401 and error.headers['Retry-After']=='7'
        else:raise AssertionError('Unauthenticated request unexpectedly succeeded')


def test_private_relay_reports_closed_tunnel_as_retryable(monkeypatch):
    with serving(Upstream) as upstream:pass
    monkeypatch.setattr(relay,'UPSTREAM',upstream)
    with serving(relay.Proxy) as gateway:
        for request in [gateway+'/ready',Request(gateway+'/v1/chat/completions',b'{}')]:
            try:opener.open(request,timeout=3)
            except HTTPError as error:
                assert error.code==503 and error.headers['Retry-After']=='10'
                assert json.load(error)['error']=='demo_tunnel_unavailable'
            else:raise AssertionError('Closed tunnel unexpectedly succeeded')
