"""Start the explicitly approved private VPS demo connection on Windows."""
import socket
import subprocess
import time
import urllib.request
from lab import ROOT, STATE, start_bridge


def listening(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=1):
            return True
    except OSError:
        return False


def main():
    if not (STATE / 'server.json').is_file():
        raise SystemExit('Missing ignored .platform/server.json SSH configuration')
    start_bridge()
    if not listening(3210):
        process = subprocess.Popen(
            [str(ROOT / '.venv/Scripts/python.exe'), str(ROOT / 'platform/demo_tunnel.py')],
            cwd=ROOT,
            stdout=(STATE / 'demo-tunnel.stdout.log').open('ab'),
            stderr=(STATE / 'demo-tunnel.stderr.log').open('ab'),
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        (STATE / 'demo-tunnel.pid').write_text(str(process.pid))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(20):
        try:
            with opener.open('http://127.0.0.1:3210/api/ready', timeout=2) as response:
                if response.status == 200:
                    print('Private demo: http://127.0.0.1:3210')
                    print('Argo CD: http://127.0.0.1:3090 (admin password: .platform/server-argocd-admin.txt)')
                    print('Keep this computer and its existing SOCKS gateway on for the temporary LLM connection.')
                    return
        except OSError:
            pass
        time.sleep(1)
    raise SystemExit('Tunnel not ready; inspect .platform/demo-tunnel.stderr.log and the SOCKS gateway on 18080')


if __name__ == '__main__':
    main()
