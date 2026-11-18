"""Supervise the Windows mTLS bridge and a private reverse SSH forward.

Run without arguments to start a hidden background connection. --stop only stops
this connection; it never terminates arbitrary processes or changes other VPNs.
"""
import argparse
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import ProxyHandler, build_opener
from pathlib import Path

from demo_tunnel import reverse
from lab import start_bridge
from ssh_client import ROOT, connect, execute

STATE = ROOT / '.platform'
STOP = STATE / 'workstation-llm.stop'
RECONNECT = STATE / 'workstation-llm.reconnect'
status = {'service': 'venture-lens-workstation-llm', 'connected': False, 'connections': 0}
opener = build_opener(ProxyHandler({}))


def get_status(path='/ready'):
    try:
        with opener.open('http://127.0.0.1:8113' + path, timeout=2) as response:
            value = json.load(response)
            return value if value.get('service') == status['service'] else None
    except (OSError, ValueError):
        return None


def bridge_alive():
    try:
        with opener.open('http://127.0.0.1:8112/live', timeout=2) as response:
            return json.load(response).get('status') == 'alive'
    except (OSError, ValueError):
        return False


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ('/live', '/ready'):
            self.send_error(404)
            return
        ready = status['connected'] and bridge_alive()
        payload = json.dumps({**status, 'bridge_alive': bridge_alive()}).encode()
        self.send_response(200 if self.path == '/live' or ready else 503)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_):
        pass


def serve():
    server = ThreadingHTTPServer(('127.0.0.1', 8113), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        while not STOP.exists():
            try:
                start_bridge()
                with connect() as client:
                    transport = client.get_transport()
                    transport.set_keepalive(15)
                    transport.request_port_forward('127.0.0.1', 18112,
                        handler=lambda *args: threading.Thread(target=reverse, args=args, daemon=True).start())
                    status.update(connected=True, connections=status['connections'] + 1)
                    print('WORKSTATION_LLM_CONNECTED', flush=True)
                    checked = 0
                    while transport.is_active() and not STOP.exists():
                        if RECONNECT.exists():
                            RECONNECT.unlink()
                            break
                        if not bridge_alive():
                            start_bridge()
                        if time.monotonic() - checked > 20:
                            execute(client, 'true', timeout=10)
                            checked = time.monotonic()
                        time.sleep(2)
            except Exception as error:
                print('WORKSTATION_LLM_RETRY: ' + type(error).__name__, flush=True)
            finally:
                status['connected'] = False
            for _ in range(5):
                if STOP.exists():
                    break
                time.sleep(1)
    finally:
        server.shutdown()
        server.server_close()


def ensure():
    if get_status():
        return
    # Binding is the singleton lock. A live but reconnecting supervisor is reused.
    if not get_status('/live'):
        STOP.unlink(missing_ok=True)
        process = subprocess.Popen([str(ROOT / '.venv/Scripts/python.exe'), str(Path(__file__).resolve()), '--run'],
            cwd=ROOT, stdout=(STATE / 'workstation-llm.stdout.log').open('ab'),
            stderr=(STATE / 'workstation-llm.stderr.log').open('ab'),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        (STATE / 'workstation-llm.pid').write_text(str(process.pid))
    for _ in range(30):
        if get_status():
            print('Workstation LLM tunnel ready; local control: http://127.0.0.1:8113/ready')
            return
        time.sleep(1)
    raise RuntimeError('Workstation LLM tunnel not ready; inspect .platform/workstation-llm.*.log')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--stop', action='store_true')
    args = parser.parse_args()
    if args.run:
        serve()
    elif args.stop:
        STOP.write_text('stop\n')
        for _ in range(15):
            if not get_status('/live'):
                break
            time.sleep(1)
    else:
        ensure()
