"""Provision explicitly authorized corporate credentials over pinned SSH stdin.

Never writes decrypted local key files or prints credential contents.
"""
import argparse
import json
import re
import sys
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from ssh_client import connect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import settings


def documents(directory, trusted_ca):
    password = (directory / 'passphrase-file').read_bytes().strip()
    profile = (directory / 'openvpn/profile.ovpn').read_text(encoding='utf-8-sig')
    options = re.sub(r'<([\w-]+)>.*?</\1>', '', profile, flags=re.S)
    allowed = {'client', 'dev', 'proto', 'remote', 'resolv-retry', 'nobind', 'persist-key', 'persist-tun',
               'verb', 'script-security', 'verify-x509-name', 'remote-cert-tls', 'auth', 'auth-nocache',
               'cipher', 'data-ciphers', 'data-ciphers-fallback', 'tls-version-min', 'tls-cipher', 'tun-mtu',
               'key-direction'}
    for line in options.splitlines():
        line = line.strip()
        if line and not line.startswith(('#', ';')) and line.split()[0].lstrip('-') not in allowed:
            raise ValueError('Profile contains an unsupported directive; explicit review required')
    if not re.search(r'(?m)^proto\s+udp\s*$', profile) or not re.search(r'(?m)^remote\s+models.example.com\s+1198\s*$', profile):
        raise ValueError('Deployment expects the approved corporate UDP/1198 profile')
    profile = re.sub(r'(?m)^script-security\s+.*$', 'script-security 1', profile)
    profile += '\naskpass /run/vpn/passphrase.txt\npull-filter ignore "redirect-gateway"\n'
    pem = (directory / 'client.pem').read_bytes()
    key = serialization.load_pem_private_key(pem, password=password)
    cert = x509.load_pem_x509_certificate(pem)
    public = lambda value: value.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    if public(key) != public(cert):
        raise ValueError('Certificate and private key do not match')
    ca = '\n'.join([(directory / 'ca/ca.pem').read_text(), (directory / 'ca/models.example.com.pem').read_text(), trusted_ca.read_text()])
    material = {'profile.ovpn': profile, 'passphrase.txt': password.decode() + '\n',
                'client.pem': cert.public_bytes(serialization.Encoding.PEM).decode(),
                'client.key': key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(),
                'corporate-ca.pem': ca}
    if not settings.API_KEY or not settings.BASE_URL.startswith('https://'):
        raise ValueError('Working corporate HTTPS endpoint and API key must be configured locally')
    return [{'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': name, 'namespace': 'venture-lens-demo'}, 'type': 'Opaque', 'stringData': values}
            for name, values in [('venture-lens-vpn', material), ('venture-lens-corporate-access',
                {'LLM_API_KEY': settings.API_KEY, 'LLM_BASE_URL': settings.BASE_URL, 'LLM_MODEL': settings.MODEL})]]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--certificate-directory', type=Path, required=True)
    parser.add_argument('--trusted-ca', type=Path, required=True)
    args = parser.parse_args()
    docs = documents(args.certificate_directory, args.trusted_ca)
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
