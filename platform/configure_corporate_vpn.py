"""Provision explicitly authorized corporate credentials over pinned SSH stdin.

Never writes decrypted local key files or prints credential contents.
"""
import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from ssh_client import connect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import settings


def documents(profile_path, passphrase_path, client_pem, trusted_ca, approved_host, approved_port):
    password = passphrase_path.read_bytes().strip()
    profile = profile_path.read_text(encoding='utf-8-sig')
    options = re.sub(r'<([\w-]+)>.*?</\1>', '', profile, flags=re.S)
    allowed = {'client', 'dev', 'proto', 'remote', 'resolv-retry', 'nobind', 'persist-key', 'persist-tun',
               'verb', 'script-security', 'verify-x509-name', 'remote-cert-tls', 'auth', 'auth-nocache',
               'cipher', 'data-ciphers', 'data-ciphers-fallback', 'tls-version-min', 'tls-cipher', 'tun-mtu',
               'key-direction'}
    for line in options.splitlines():
        line = line.strip()
        if line and not line.startswith(('#', ';')) and line.split()[0].lstrip('-') not in allowed:
            raise ValueError('Profile contains an unsupported directive; explicit review required')
    protocols = re.findall(r'(?m)^\s*proto[ \t]+([^\r\n]+)$', options)
    remotes = re.findall(r'(?m)^\s*remote[ \t]+([^\r\n]+)$', options)
    if [value.strip() for value in protocols] != ['udp'] or [value.split() for value in remotes] != [[approved_host, str(approved_port)]]:
        raise ValueError('VPN profile must specify exactly the approved UDP endpoint')
    profile = re.sub(r'(?m)^script-security\s+.*$', 'script-security 1', profile)
    profile += '\naskpass /run/vpn/passphrase.txt\npull-filter ignore "redirect-gateway"\n'
    pem = client_pem.read_bytes()
    key = serialization.load_pem_private_key(pem, password=password)
    cert = x509.load_pem_x509_certificate(pem)
    public = lambda value: value.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    if public(key) != public(cert):
        raise ValueError('Certificate and private key do not match')
    ca = trusted_ca.read_text(encoding='utf-8')
    material = {'profile.ovpn': profile, 'passphrase.txt': password.decode() + '\n',
                'client.pem': cert.public_bytes(serialization.Encoding.PEM).decode(),
                'client.key': key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(),
                'corporate-ca.pem': ca}
    target = urlsplit(settings.BASE_URL)
    if not settings.API_KEY or target.scheme != 'https' or not target.hostname:
        raise ValueError('Configure an HTTPS model server and API key locally')
    return [{'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': name, 'namespace': 'venture-lens-demo'}, 'type': 'Opaque', 'stringData': values}
            for name, values in [('venture-lens-vpn', material), ('venture-lens-corporate-access',
                {'LLM_API_KEY': settings.API_KEY, 'LLM_BASE_URL': settings.BASE_URL,
                 'LLM_MODEL': settings.MODEL, 'MODEL_SERVER_HOST': target.hostname})]]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--passphrase-file', type=Path, required=True)
    parser.add_argument('--client-pem', type=Path, required=True)
    parser.add_argument('--trusted-ca', type=Path, required=True)
    parser.add_argument('--approved-remote-host', required=True)
    parser.add_argument('--approved-remote-port', type=int, default=1194)
    args = parser.parse_args()
    docs = documents(args.profile, args.passphrase_file, args.client_pem, args.trusted_ca,
                     args.approved_remote_host, args.approved_remote_port)
    with connect() as client:
        for doc in docs:
            stdin, stdout, stderr = client.exec_command('k3s kubectl apply -f -')
            stdin.write(json.dumps(doc)); stdin.channel.shutdown_write()
            stdout.read(); stderr.read()
            if stdout.channel.recv_exit_status():
                raise RuntimeError('Secret provisioning failed; sensitive output suppressed')
    print('Corporate VPN/mTLS Secrets provisioned; restart corporate-llm after rotation.')


if __name__ == '__main__':
    main()
