import json
from datetime import datetime, timezone
from types import SimpleNamespace
import httpx
import pytest
from fastapi.testclient import TestClient
from cos import tokenops, server


def payload():
    return {'show_financial_detail': True, 'month': datetime.now(timezone.utc).strftime('%Y-%m'),
            'budget_summary': {'monthly_budget_usd': 100, 'monthly_spend_usd': 80,
                               'remaining_budget_usd': 20, 'data_freshness_at': '2026-11-25T16:00:00Z'},
            'application': {'apiKey': 'must-not-leak'}, 'user': 'private-account'}


@pytest.fixture
def connection(monkeypatch):
    vault = {}
    class Vault:
        def get_password(self, service, account): return vault.get((service, account))
        def set_password(self, service, account, value): vault[service, account] = value
        def delete_password(self, service, account): vault.pop((service, account), None)
    t = tokenops.TokenOps()
    monkeypatch.setattr(t, 'credential_store', lambda: Vault())
    pending, calls = [], []
    class Thread:
        def __init__(self, target, args, **kwargs): self.target, self.args = target, args
        def start(self): pending.append(lambda: self.target(*self.args))
    monkeypatch.setattr(tokenops.threading, 'Thread', Thread)
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json=payload(), request=httpx.Request('GET', url))
    monkeypatch.setattr(tokenops.httpx, 'get', get)
    return t, pending, calls, vault


def test_personal_balances_are_usd_and_only_allowlisted_fields_survive():
    result = tokenops.normalize(payload())
    assert result['remaining_usd'] == 20 and result['currency'] == 'USD'
    assert 'must-not-leak' not in json.dumps(result) and 'private-account' not in json.dumps(result)
    value = payload(); value['budget_summary']['remaining_budget_usd'] = -10
    assert tokenops.normalize(value)['remaining_usd'] == -10


def test_large_sessions_fit_win_vault_entries_and_rotation_removes_old_chunks(connection, monkeypatch):
    t, _, _, vault = connection
    t.save({'session': 'x'*10000})
    assert max(map(len, vault.values())) <= 800
    assert t.credentials() == {'session': 'x'*10000}
    old_parts = set(vault) - {(tokenops.SERVICE, t.account())}
    t.save({'session': 'y'*9000})
    assert not old_parts.intersection(vault)
    assert t.credentials() == {'session': 'y'*9000}
    t.disconnect()
    assert not vault


def test_failed_chunk_save_preserves_the_previous_session(connection, monkeypatch):
    t, _, _, vault = connection
    t.save({'session': 'original'})
    before = dict(vault)
    backend = t.credential_store()
    write = backend.set_password
    calls = []
    def fail(service, account, value):
        calls.append(account)
        if len(calls) == 2:
            raise OSError('synthetic secure-store failure')
        write(service, account, value)
    backend.set_password = fail
    monkeypatch.setattr(t, 'credential_store', lambda: backend)
    with pytest.raises(OSError):
        t.save({'session': 'z'*10000})
    assert vault == before
    assert t.credentials() == {'session': 'original'}


@pytest.mark.parametrize('change', [
    lambda v: v.update(show_financial_detail=False),
    lambda v: v.pop('show_financial_detail'),
    lambda v: v.update(month='not-a-month'),
    lambda v: v.update(data_error='private diagnostic'),
    lambda v: v['budget_summary'].update(monthly_budget_usd=-1),
    lambda v: v['budget_summary'].update(monthly_spend_usd=True),
    lambda v: v['budget_summary'].update(remaining_budget_usd=float('nan')),
    lambda v: v['budget_summary'].update(data_freshness_at='not-a-timestamp'),
])
def test_hidden_or_invalid_financial_details_are_not_exposed(change):
    value = payload(); change(value)
    with pytest.raises((ValueError, TypeError)):
        tokenops.normalize(value)


def test_refresh_uses_current_user_only_and_caches_without_exporting_cookies(connection):
    t, pending, calls, _ = connection
    t.save({'session': 'private-cookie'})
    assert t.snapshot()['refreshing']
    t.snapshot(); assert len(pending) == 1
    pending.pop()()
    result = t.snapshot()
    assert result['report']['remaining_usd'] == 20 and not result['stale']
    assert not any(s in json.dumps(result) for s in ('private-cookie', 'must-not-leak', 'private-account'))
    url, kwargs = calls[0]
    assert url == tokenops.SUMMARY
    assert kwargs['params'] == {'presentation': 'my-usage/v1'}
    assert kwargs['cookies'] == {'session': 'private-cookie'}
    assert kwargs['follow_redirects'] is False
    assert 'Authorization' not in kwargs['headers']
    assert not pending
    result['report']['remaining_usd'] = -999
    assert t.snapshot()['report']['remaining_usd'] == 20


def test_no_session_requires_interactive_signin_without_network_retry(connection):
    t, pending, calls, _ = connection
    t.snapshot(); pending.pop()()
    assert t.snapshot()['state'] == 'sign_in_required'
    assert not pending and not calls


def test_expired_session_keeps_labeled_stale_balance_without_redirecting(connection, monkeypatch):
    t, pending, calls, _ = connection
    t.save({'session': 'private-cookie'})
    t.snapshot(); pending.pop()()
    monkeypatch.setattr(tokenops.httpx, 'get', lambda *a, **kw: httpx.Response(302, headers={'location': 'https://outside.example'}))
    t.snapshot(refresh=True); pending.pop()()
    result = t.snapshot()
    assert result['state'] == 'sign_in_required' and result['stale']
    assert result['report']['remaining_usd'] == 20
    assert not pending


def test_failed_refresh_and_late_response_cannot_resurrect_a_disconnected_account(connection, monkeypatch):
    t, pending, calls, vault = connection
    t.save({'session': 'private-cookie'})
    t.snapshot(); pending.pop()()
    def fail(*a, **kw): raise httpx.ReadTimeout('private-cookie')
    monkeypatch.setattr(tokenops.httpx, 'get', fail)
    t.snapshot(refresh=True); pending.pop()()
    assert t.snapshot()['stale'] and 'private-cookie' not in json.dumps(t.snapshot())
    t.snapshot(refresh=True)
    obsolete = pending.pop()
    t.disconnect(); obsolete()
    assert not vault and t.snapshot()['report'] is None
    assert t.snapshot()['state'] == 'not_connected'


def test_previous_month_balance_is_not_presented_as_current(connection):
    t, pending, _, _ = connection
    old = tokenops.normalize(payload()); old['month'] = '2000-01'
    t.result = {'state': 'ready', 'report': old, 'stale': False, 'error': ''}
    t.next_refresh = float('inf')
    assert t.snapshot()['report'] is None and len(pending) == 1


def test_interactive_connection_saves_only_portal_cookies_and_closes_browser(connection, monkeypatch):
    t, pending, _, vault = connection
    seen = {}
    class Response:
        status = 200
        def json(self): return payload()
    page = SimpleNamespace(url=tokenops.PORTAL, is_closed=lambda: False,
                goto=lambda *a, **kw: seen.update(goto=(a, kw)),
                request=SimpleNamespace(get=lambda *a, **kw: seen.update(get=(a, kw)) or Response()))
    browser = SimpleNamespace(pages=[page],
                cookies=lambda urls: [{'name': 'session', 'value': 'private-cookie', 'domain': 'llm.amd.com'},
                                      {'name': 'sso', 'value': 'do-not-copy', 'domain': 'amdsso.okta.com'}],
                close=lambda: seen.update(closed=True))
    class Playwright:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        chromium = SimpleNamespace(launch_persistent_context=lambda *a, **kw: seen.update(launch=kw) or browser)
    import playwright.sync_api
    monkeypatch.setattr(playwright.sync_api, 'sync_playwright', Playwright)
    assert t.connect()['connecting']
    assert len(pending) == 1
    pending.pop()()
    assert seen['launch']['headless'] is False and seen['closed']
    assert seen['get'][0] == (tokenops.SUMMARY,) and seen['get'][1]['max_redirects'] == 0
    assert t.credentials() == {'session': 'private-cookie'}
    assert 'do-not-copy' not in str(vault)
    assert t.snapshot()['report']['remaining_usd'] == 20


def test_tokenops_endpoints_require_session_and_mutation_token(connection, monkeypatch):
    t, pending, _, _ = connection
    monkeypatch.setattr(server, 'tokenops', t)
    client = TestClient(server.app)
    assert client.get('/api/tokenops').status_code == 401
    client.cookies.set('cos_session', server.TOKEN)
    assert client.post('/api/tokenops/connect', json={}).status_code == 403
    assert not pending
    response = client.post('/api/tokenops/connect', json={}, headers={'X-COS-Token': server.TOKEN})
    assert response.json()['connecting'] and len(pending) == 1
