"""One canonical public origin shared by deployment and workstation utilities."""
from pathlib import Path
from urllib.parse import urlsplit
import yaml


def public_origin():
    root = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((root / 'deploy/components/public-demo/kustomization.yaml').read_text())
    generator = next(item for item in config['configMapGenerator'] if item['name'] == 'venture-lens-public-settings')
    value = next(item.split('=', 1)[1] for item in generator['literals'] if item.startswith('PUBLIC_ORIGIN='))
    address = urlsplit(value)
    if address.scheme != 'https' or not address.hostname or address.path or address.username or address.query or address.fragment:
        raise ValueError('A canonical HTTPS origin without path or credentials is required')
    return value
