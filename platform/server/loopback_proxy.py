"""Temporary demo relay: pod network -> loopback SSH reverse forward.

Corporate credentials and certificate remain on the Windows workstation.
The caller's bridge token is passed through and verified by the Windows adapter.
"""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.request import Request,build_opener,ProxyHandler
from urllib.error import HTTPError,URLError
import os

class Proxy(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path!='/v1/chat/completions':self.send_error(404);return
        length=int(self.headers.get('Content-Length','0'))
        if not 0<length<=500000:self.send_error(413);return
        data=self.rfile.read(length)
        request=Request('http://127.0.0.1:18112'+self.path,data=data,headers={'Authorization':self.headers.get('Authorization',''),'Content-Type':'application/json'})
        try:
            with build_opener(ProxyHandler({})).open(request,timeout=160) as response:
                body=response.read();self.send_response(response.status);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(body)
        except HTTPError as error:
            self.send_response(error.code);self.send_header('Retry-After',error.headers.get('Retry-After','10'));self.end_headers();self.wfile.write(b'{"error":"gateway_unavailable"}')
        except (URLError,TimeoutError):
            self.send_response(503);self.send_header('Retry-After','10');self.end_headers();self.wfile.write(b'{"error":"demo_tunnel_unavailable"}')
    def log_message(self,*_):pass

if __name__=='__main__':ThreadingHTTPServer((os.environ.get('POD_GATEWAY','127.0.0.1'),18113),Proxy).serve_forever()
