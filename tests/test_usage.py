import json
from concurrent.futures import ThreadPoolExecutor
import httpx
import pytest
from fastapi.testclient import TestClient
from cos import provider, usage, server
from cos.db import Store
from cos.trace import context


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    monkeypatch.setattr(provider, 'gateway_key', lambda: 'test-secret')
    monkeypatch.setattr(provider.time, 'sleep', lambda _: None)
    return provider.Gateway(Store(tmp_path/'usage.sqlite3'))


@pytest.mark.parametrize('payload,expected', [
    ({'prompt_tokens': 10, 'completion_tokens': 5, 'prompt_tokens_details': {'cached_tokens': 8},
      'completion_tokens_details': {'reasoning_tokens': 2}}, (10, 5, 15, 8, 2)),
    ({'input_tokens': 12, 'output_tokens': 6, 'total_tokens': 18,
      'input_tokens_details': {'cached_tokens': 4}, 'output_tokens_details': {'reasoning_tokens': 3}}, (12, 6, 18, 4, 3)),
    ({'input_tokens': True, 'output_tokens': -1, 'total_tokens': '8'}, (None,)*5),
    ({'input_tokens': 2**64, 'output_tokens': float('inf')}, (None,)*5),
    (None, (None,)*5),
])
def test_normalize(payload, expected):
    assert usage.normalize(payload) == expected


def test_every_physical_attempt_is_recorded_without_secrets_or_bodies(gateway, monkeypatch):
    replies = [httpx.TimeoutException('uncertain'), httpx.Response(503),
               httpx.Response(200, json={'choices': [{'message': {'content': 'private reply'}}],
                                        'usage': {'prompt_tokens': 10, 'completion_tokens': 5}})]
    def post(*args, **kwargs):
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(provider.httpx, 'post', post)
    job = gateway.capacity.store.create_job('Private work', 'Technology')
    with context(job_id=job['id']):
        gateway.complete([{'role': 'user', 'content': 'private prompt'}])
    rows = gateway.capacity.store.rows('SELECT * FROM model_usage ORDER BY created')
    assert [r['status'] for r in rows] == ['unknown', 'unknown', 'reported']
    assert all(r['job_id'] == job['id'] for r in rows)
    assert all(r['role'] == 'worker' for r in rows)
    assert not any(secret in json.dumps(rows) for secret in ('test-secret', 'private prompt', 'private reply'))
    result = usage.summary(gateway.capacity.store)['period']
    assert result['requests'] == 3 and result['total_tokens'] == 15
    assert result['incomplete_requests'] == 2


def test_routing_and_background_calls_are_counted_once(gateway, monkeypatch):
    monkeypatch.setattr(provider.httpx, 'post', lambda *a, **kw: httpx.Response(200, json={
        'answers': {}, 'choices': [{'message': {'content': 'OK'}}],
        'usage': {'input_tokens': 7, 'output_tokens': 2, 'total_tokens': 9}}))
    from cos import model_routing
    def select(g, config, *args):
        g.decide('decision-model', 'state', {})
        return config['models']['worker'], {'mode': 'automatic'}
    monkeypatch.setattr(model_routing, 'select', select)
    gateway.purpose = 'email_intake'
    gateway.complete([])
    result = usage.summary(gateway.capacity.store)
    assert result['period']['total_tokens'] == 18
    assert {r['purpose'] for r in result['purposes']} == {'routing', 'email_intake'}
    assert len(gateway.capacity.store.rows('SELECT * FROM model_usage')) == 2


def test_missing_rejected_and_interrupted_usage_remain_distinct(gateway, monkeypatch):
    replies = [httpx.Response(200, json={'choices': [{'message': {'content': 'OK'}}]}),
               httpx.Response(401)]
    monkeypatch.setattr(provider.httpx, 'post', lambda *a, **kw: replies.pop(0))
    gateway.complete([])
    with pytest.raises(provider.NotConfigured):
        gateway.complete([])
    usage.record(gateway.capacity.store, 'interrupted')
    restarted = Store(gateway.capacity.store.path)
    result = usage.summary(restarted)['period']
    assert result['requests'] == 3 and result['total_tokens'] == 0
    assert result['reported_requests'] == 0 and result['incomplete_requests'] == 2
    assert result['rejected_requests'] == 1


def test_concurrent_accounting_and_repeated_receipt_updates(tmp_path):
    path = tmp_path/'parallel.sqlite3'
    store = Store(path)
    def write(index):
        identity = str(index)
        usage.record(store, identity)
        for _ in range(2):
            usage.record(store, identity, status='reported', data={'usage': {'input_tokens': 3, 'output_tokens': 2}})
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(24)))
    result = usage.summary(Store(path))['period']
    assert result['requests'] == 24 and result['total_tokens'] == 120


def test_accounting_failure_does_not_replay_success(gateway, monkeypatch):
    calls = []
    monkeypatch.setattr(provider.httpx, 'post', lambda *a, **kw: calls.append(1) or httpx.Response(200, json={
        'choices': [{'message': {'content': 'OK'}}], 'usage': {'total_tokens': 4}}))
    write = gateway.capacity.store.write
    def fail_usage(sql, args=()):
        if 'model_usage' in sql:
            raise OSError('disk failure')
        return write(sql, args)
    monkeypatch.setattr(gateway.capacity.store, 'write', fail_usage)
    answer, _ = gateway.complete([])
    assert answer['content'] == 'OK' and len(calls) == 1


def test_job_tree_and_period_filters(gateway):
    store = gateway.capacity.store
    parent = store.create_job('Parent', 'Technology')
    child = store.create_job('Child', 'Technology', parent_id=parent['id'])
    unrelated = store.create_job('Other', 'Technology')
    for job in (parent, child, unrelated):
        with context(job_id=job['id']):
            usage.record(store, job['id'])
            usage.record(store, job['id'], status='reported', data={'usage': {'input_tokens': 4, 'output_tokens': 1}})
    assert usage.summary(store, job_id=parent['id'])['period']['total_tokens'] == 10
    assert usage.summary(store, '2100-01-01T00:00:00Z')['period']['requests'] == 0
    for invalid in ('no', '2026-11-25', '2026-11-25T00:00:00'):
        with pytest.raises(ValueError):
            usage.summary(store, invalid)


def test_usage_api_session_and_readonly_validation(gateway, monkeypatch):
    monkeypatch.setattr(server, 'store', gateway.capacity.store)
    client = TestClient(server.app)
    assert client.get('/api/usage').status_code == 401
    assert client.get('/api/gateway/usage').status_code == 401
    client.cookies.set('cos_session', server.TOKEN)
    assert client.get('/api/usage?since=invalid').status_code == 400
    assert client.get('/api/usage').json()['period']['requests'] == 0
    assert client.post('/api/gateway/usage/refresh', json={}).status_code == 403


def test_costs_use_per_request_rates_with_consistent_model_activity_and_job_totals(gateway, monkeypatch):
    from cos import model_catalog
    monkeypatch.setattr(model_catalog, 'catalog', lambda: {'models': [
        {'id': 'priced', 'pricing': {'input_per_million': 2, 'output_per_million': 4,
         'long_context_threshold': 100, 'input_long_context_per_million': 3,
         'output_long_context_per_million': 6}},
        {'id': 'free', 'pricing': {'input_per_million': 0, 'output_per_million': 0}}]})
    store = gateway.capacity.store
    parent = store.create_job('Parent', 'Technology')
    child = store.create_job('Child', 'Technology', parent_id=parent['id'])
    for identity, model, incoming, job, purpose in (
            ('short-a', 'priced', 60, parent, 'agent'),
            ('short-b', 'priced', 60, child, 'routing'),
            ('long', 'priced', 120, child, 'agent'),
            ('unknown', 'no-price', 10, child, 'agent'),
            ('free', 'free', 10, child, 'agent')):
        with context(job_id=job['id']):
            usage.record(store, identity, model=model, purpose=purpose)
        usage.record(store, identity, status='reported', data={'usage': {
            'input_tokens': incoming, 'output_tokens': 10}})
    usage.record(store, 'pending', model='priced')
    result = usage.summary(store)
    # Two short requests remain on the base rate despite their combined length.
    expected = (60 * 2 + 10 * 4) * 2 / 1e6 + (120 * 3 + 10 * 6) / 1e6
    assert result['period']['estimated_cost_usd'] == pytest.approx(expected)
    assert result['period']['priced_requests'] == 4
    assert result['period']['unpriced_requests'] == 2
    assert sum(r['estimated_cost_usd'] or 0 for r in result['models']) == pytest.approx(expected)
    assert sum(r['estimated_cost_usd'] or 0 for r in result['purposes']) == pytest.approx(expected)
    by_model = {r['model']: r for r in result['models']}
    assert by_model['no-price']['estimated_cost_usd'] is None
    assert by_model['free']['estimated_cost_usd'] == 0
    scoped = usage.summary(store, job_id=parent['id'])['period']
    assert scoped['estimated_cost_usd'] == pytest.approx(expected)
    assert scoped['unpriced_requests'] == 1
    assert usage.summary(store, '2100-01-01T00:00:00Z')['period']['estimated_cost_usd'] == 0


def test_missing_pricing_never_claims_free_usage_and_stale_prices_are_identified(gateway, monkeypatch):
    from cos import model_catalog
    store = gateway.capacity.store
    usage.record(store, 'request', model='reasoning')
    usage.record(store, 'request', status='reported', data={'usage': {
        'input_tokens': 10, 'output_tokens': 20}})
    monkeypatch.setattr(model_catalog, 'catalog', lambda: {'models': [], 'error': 'offline'})
    missing = usage.summary(store)['period']
    assert missing['estimated_cost_usd'] is None
    assert missing['unpriced_requests'] == 1
    monkeypatch.setattr(model_catalog, 'catalog', lambda: {'stale': True, 'models': [
        {'id': 'reasoning', 'pricing': {'input_per_million': 1, 'output_per_million': 2,
                                      'reasoning_per_million': 4}}]})
    result = usage.summary(store)
    assert result['cost_pricing_stale'] is True
    # Conservative output pricing includes reasoning; never add tokens twice.
    assert result['period']['estimated_cost_usd'] == pytest.approx(90 / 1e6)
