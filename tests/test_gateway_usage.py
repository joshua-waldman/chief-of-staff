import json
import httpx
import pytest
from cos import gateway_usage as gateway


@pytest.fixture
def remote(monkeypatch):
    monkeypatch.setattr(gateway, '_cache', {})
    monkeypatch.setattr(gateway, '_running', set())
    monkeypatch.setattr(gateway, 'gateway_key', lambda: 'private-key')
    pending, calls = [], []
    class Thread:
        def __init__(self, target, args, **kwargs):
            self.target, self.args = target, args
        def start(self):
            pending.append(lambda: self.target(*self.args))
    monkeypatch.setattr(gateway.threading, 'Thread', Thread)
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json={
            'application': {'apiKey': 'private-key', 'name': 'Private team'},
            'totalRequests': 12, 'totalTokens': 600, 'approxChargeInUSD': 0.7,
            'stats': {'private': [{'secret': 'must-not-leak'}]}, 'unexpected': 'must-not-leak'})
    monkeypatch.setattr(gateway.httpx, 'get', get)
    return pending, calls


def test_refresh_is_async_cached_scoped_and_uses_only_verified_endpoint(remote, monkeypatch):
    pending, calls = remote
    assert gateway.snapshot()['state'] == 'loading'
    assert gateway.snapshot()['refreshing'] and len(pending) == 1
    pending.pop()()
    result = gateway.snapshot()
    assert result['total_tokens'] == 600 and result['approx_charge_usd'] == 0.7
    assert result['balance'] is None and result['scope'] == 'api_key'
    assert len(calls) == 1
    url, kwargs = calls[0]
    assert url == 'https://llm-api.amd.com/api/UsageStats'
    assert kwargs['headers']['Authorization'] == 'Bearer private-key'
    assert kwargs['follow_redirects'] is False and kwargs['timeout'] == 30
    assert not any(v in json.dumps(result) for v in ('private-key', 'must-not-leak', 'Private team'))
    result['total_tokens'] = -1
    assert gateway.snapshot()['total_tokens'] == 600
    monkeypatch.setattr(gateway, 'gateway_key', lambda: 'other-key')
    assert gateway.snapshot()['state'] == 'loading'
    assert gateway.snapshot().get('total_tokens') is None


def test_failure_keeps_stale_report_and_sanitizes_errors(remote, monkeypatch):
    pending, calls = remote
    gateway.snapshot(); pending.pop()()
    def fail(*args, **kw):
        raise RuntimeError('private-key must-not-leak')
    monkeypatch.setattr(gateway.httpx, 'get', fail)
    gateway.snapshot(refresh=True); pending.pop()()
    result = gateway.snapshot()
    assert result['stale'] and result['total_tokens'] == 600 and result['error']
    assert not any(v in json.dumps(result) for v in ('private-key', 'must-not-leak'))
    assert not pending


@pytest.mark.parametrize('response', [
    httpx.Response(302, headers={'location': 'https://outside.example'}),
    httpx.Response(401, text='private-key'),
    httpx.Response(200, json={'totalRequests': '12', 'totalTokens': -1}),
    httpx.Response(200, text='invalid JSON'),
    httpx.Response(200, json=[]),
])
def test_bad_remote_reports_are_unavailable(remote, monkeypatch, response):
    pending, _ = remote
    monkeypatch.setattr(gateway.httpx, 'get', lambda *a, **kw: response)
    gateway.snapshot(); pending.pop()()
    result = gateway.snapshot()
    assert result['state'] == 'unavailable' and result['error']
    assert 'total_tokens' not in result
    assert 'private-key' not in json.dumps(result)


def test_no_credential_leaves_verified_gateway_or_runs_without_key(remote, monkeypatch):
    pending, calls = remote
    monkeypatch.setattr(gateway, 'settings', lambda: {'gateway_url': 'https://outside.example'})
    assert gateway.snapshot()['error'] and not pending and not calls
    monkeypatch.setattr(gateway, 'settings', lambda: gateway.DEFAULTS)
    monkeypatch.setattr(gateway, 'gateway_key', lambda: '')
    assert gateway.snapshot()['error'] and not pending and not calls
