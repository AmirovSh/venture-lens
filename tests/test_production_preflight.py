"""Production templates and rendered releases require the signing Secret."""
import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
import yaml

from backend import settings
from backend.app import app


ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('production_preflight',ROOT/'platform/preflight.py')
preflight=importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


@pytest.mark.parametrize('setting', ['AUTH_CONTEXT_REQUIRED','AUTH_CONTEXT_SECRET'])
def test_preflight_rejects_missing_auth_even_with_release_placeholders(setting):
    manifest=yaml.safe_load((ROOT/'deploy/overlays/production/api-auth.yaml').read_text(encoding='utf-8'))
    container=manifest['spec']['template']['spec']['containers'][0]
    container['image']='ghcr.io/REPLACE_OWNER/venture-lens-api:REPLACE_RELEASE_SHA'
    preflight.validate(yaml.safe_dump(manifest),template=True)
    container['env']=[item for item in container['env'] if item['name']!=setting]
    with pytest.raises(ValueError,match='requires signed identity'):
        preflight.validate(yaml.safe_dump(manifest),template=True)


def test_production_mode_rejects_unsigned_api_requests_with_or_without_signing_secret(monkeypatch):
    client=TestClient(app)
    try:
        for secret in ('','test-only-authentication-key'):
            monkeypatch.setattr(settings,'env',{**settings.env,'AUTH_CONTEXT_REQUIRED':'true','AUTH_CONTEXT_SECRET':secret})
            assert client.get('/api/runs').status_code==401
            assert client.get('/api/live').status_code==200
    finally:
        client.close()
