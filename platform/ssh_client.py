"""Explicit server configuration and pinned host key; no ambient agent/key discovery."""
import json
import sys
from pathlib import Path
import paramiko
import socks

ROOT=Path(__file__).resolve().parent.parent

def connect():
    config=json.loads((ROOT/'.platform/server.json').read_text())
    client=paramiko.SSHClient()
    client.load_host_keys(str(ROOT/'.platform/adminvps_known_hosts'))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    route=config.get('socks')
    sock=None
    if route:
        sock=socks.socksocket();sock.set_proxy(socks.SOCKS5,route['host'],route['port'])
        sock.settimeout(20);sock.connect((config['host'],config.get('port',22)))
    client.connect(config['host'],port=config.get('port',22),username=config['user'],key_filename=str(ROOT/config['identity']),sock=sock,allow_agent=False,look_for_keys=False,timeout=20,auth_timeout=20,banner_timeout=20)
    return client

def execute(client,command,timeout=120):
    _,stdout,stderr=client.exec_command(command,timeout=timeout)
    result=stdout.read().decode(errors='replace');error=stderr.read().decode(errors='replace')
    code=stdout.channel.recv_exit_status()
    if code:raise RuntimeError(f'Remote command failed ({code}): '+error[:800])
    return result

if __name__=='__main__':
    with connect() as client:print(execute(client,sys.stdin.read()),end='')
