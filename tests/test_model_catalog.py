import pytest
from fastapi.testclient import TestClient
from cos import config, model_catalog, server


class Response:
    status_code = 200
    def __init__(self, data):
        self.data = data
    def json(self):
        return self.data


@pytest.fixture
def catalog(monkeypatch):
    monkeypatch.setattr(model_catalog, '_cache', {})
    monkeypatch.setattr(model_catalog, '_active', None)
    monkeypatch.setattr(model_catalog, 'gateway_key', lambda: 'synthetic-key')
    calls = []
    data = {'object': 'list', 'data': [
        {'id': 'DeepSeek-example', 'mode': 'chat', 'context_length': 1000000,
         'capabilities': {'chat': True, 'vision': False}, 'keyName': 'private-deployment-key', 'stats': {'private': 1}},
        {'id': 'decision-example', 'mode': 'decision', 'capabilities': {'chat': False, 'decision': True}},
        {'id': 'realtime-example', 'mode': 'realtime', 'capabilities': {'chat': False}},
        {'id': 'DeepSeek-example', 'mode': 'chat', 'context_length': 1000000}, None, {'id': ''}]}
    def get(url, **kw):
        calls.append((url, kw))
        return Response(data)
    monkeypatch.setattr(model_catalog.httpx, 'get', get)
    return calls, data


def test_catalog_discovers_all_types_caches_and_refreshes_without_exposing_deployments(catalog):
    calls, _ = catalog
    result = model_catalog.catalog()
    assert len(result['models']) == 3
    assert 'private' not in str(result)
    assert calls[0][0] == config.DEFAULTS['gateway_url'] + '/models'
    assert calls[0][1]['follow_redirects'] is False
    assert calls[0][1]['headers']['Authorization'] == 'Bearer synthetic-key'
    assert model_catalog.catalog() == result
    assert len(calls) == 1
    assert model_catalog.snapshot() == result['models']
    result['models'].clear()
    assert len(model_catalog.snapshot()) == 3
    model_catalog.catalog(refresh=True)
    assert len(calls) == 2


def test_catalog_failure_keeps_stale_models_and_new_credentials_do_not_share_cache(catalog, monkeypatch):
    model_catalog.catalog()
    def fail(*args, **kwargs):
        raise model_catalog.httpx.ConnectError('synthetic-key must not be returned')
    monkeypatch.setattr(model_catalog.httpx, 'get', fail)
    failed = model_catalog.catalog(refresh=True)
    assert failed['stale'] and len(failed['models']) == 3
    assert 'synthetic-key' not in str(failed)
    monkeypatch.setattr(model_catalog, 'gateway_key', lambda: 'different-key')
    result = model_catalog.catalog()
    assert result['models'] == [] and result['error']
    assert model_catalog.snapshot() == []


def test_catalog_never_sends_credentials_to_unverified_endpoint_or_without_key(catalog, monkeypatch):
    calls, _ = catalog
    monkeypatch.setattr(model_catalog, 'settings', lambda: {**config.DEFAULTS, 'gateway_url': 'https://outside.example'})
    assert model_catalog.catalog()['error']
    assert not calls
    monkeypatch.setattr(model_catalog, 'settings', config.settings)
    monkeypatch.setattr(model_catalog, 'gateway_key', lambda: '')
    assert model_catalog.catalog()['error']
    assert not calls


@pytest.mark.parametrize('payload', [{}, {'data': {}}, {'data': []}, {'data': [None]}])
def test_invalid_catalog_is_not_reported_as_success(catalog, payload):
    _, data = catalog
    data.clear()
    data.update(payload)
    assert model_catalog.catalog()['error']


def test_model_catalog_api_requires_session_and_does_not_modify_settings(catalog):
    client = TestClient(server.app)
    assert client.get('/api/gateway/models').status_code == 401
    client.cookies.set('cos_session', server.TOKEN)
    result = client.get('/api/gateway/models?refresh=true')
    assert result.status_code == 200 and len(result.json()['models']) == 3
    assert not (config.ROOT/'config.local.json').exists()
