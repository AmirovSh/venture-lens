"""Forward requests to an upstream on the private host network."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.request import Request,build_opener,ProxyHandler
from urllib.error import HTTPError,URLError
import os

UPSTREAM=os.environ.get('MODEL_RELAY_UPSTREAM','http://127.0.0.1:18112')

class Proxy(BaseHTTPRequestHandler):
    def reply(self,status,body,retry=None):
        self.send_response(status)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body)))
        if retry:self.send_header('Retry-After',retry)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path not in ('/live','/ready'):self.send_error(404);return
        # Readiness checks reachability of the upstream process.
        try:
            with build_opener(ProxyHandler({})).open(UPSTREAM+'/live',timeout=3) as response:
                self.reply(response.status,response.read())
        except (URLError,TimeoutError,OSError):
            self.reply(503,b'{"error":"demo_tunnel_unavailable"}','10')

    def do_POST(self):
        if self.path!='/v1/chat/completions':self.send_error(404);return
        try:length=int(self.headers.get('Content-Length','0'))
        except ValueError:self.send_error(400);return
        if not 0<length<=500000:self.send_error(413);return
        data=self.rfile.read(length)
        request=Request(UPSTREAM+self.path,data=data,headers={'Authorization':self.headers.get('Authorization',''),'Content-Type':'application/json'})
        try:
            with build_opener(ProxyHandler({})).open(request,timeout=160) as response:
                self.reply(response.status,response.read())
        except HTTPError as error:
            self.reply(error.code,b'{"error":"gateway_unavailable"}',error.headers.get('Retry-After','10'))
        except (URLError,TimeoutError,OSError):
            self.reply(503,b'{"error":"demo_tunnel_unavailable"}','10')
    def log_message(self,*_):pass

if __name__=='__main__':
    host = os.environ['POD_GATEWAY']
    port = int(os.environ.get('MODEL_RELAY_PORT', '18113'))
    ThreadingHTTPServer((host, port), Proxy).serve_forever()
