"""Short-lived identity assertion from the private access gateway to the API."""
import base64
import hashlib
import hmac
import json
import time

IDENTITY_HEADER = 'x-venture-identity'
SIGNATURE_HEADER = 'x-venture-signature'


def sign_identity(identity, secret, now=None):
    payload = {'workspace': identity['workspace'], 'role': identity['role'],
               'expires': int(now or time.time()) + 60}
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode()
    return {IDENTITY_HEADER: encoded, SIGNATURE_HEADER: hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()}


def verify_identity(headers, secret, now=None):
    encoded, signature = headers.get(IDENTITY_HEADER, ''), headers.get(SIGNATURE_HEADER, '')
    if not secret or len(encoded) > 512 or len(signature) != 64:
        return None
    expected = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(encoded))
        current = now or time.time()
        if not current < payload['expires'] <= current + 65:
            return None
        workspace = payload['workspace']
        if not isinstance(workspace, str) or not workspace or len(workspace) > 64:
            return None
        if payload['role'] not in ('owner', 'viewer', 'member', 'admin'):
            return None
        if payload['role'] in ('owner', 'viewer') and workspace != 'main':
            return None
        if payload['role'] in ('member', 'admin') and workspace == 'main':
            return None
        return payload
    except (ValueError, TypeError, KeyError):
        return None
