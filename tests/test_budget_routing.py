import copy
import json
from datetime import datetime, timedelta, timezone
import pytest
from cos import budget_routing, config, model_catalog, tokenops
from test_model_routing import routed, model


def report(remaining=20):
    now = datetime.now(timezone.utc)
    return {'state': 'ready', 'stale': False, 'report': {
        'currency': 'USD', 'month': now.strftime('%Y-%m'), 'budget_usd': 100,
        'remaining_usd': remaining, 'fetched_at': now.isoformat(),
        'source_updated_at': now.isoformat()}}


@pytest.fixture
def budget(routed, monkeypatch):
    settings, models, gateway, calls, decision = routed
    for item in models:
        item['pricing'] = {'input_per_million': 10, 'output_per_million': 30}
    models[1]['pricing'] = {'input_per_million': .1, 'output_per_million': .3}
    current = report()
    monkeypatch.setattr(budget_routing, 'budget_snapshot', lambda: current)
    decision['candidates'] = {'code': {'choice':'suitable','confidence':.99},
                              'fast': {'choice':'suitable','confidence':.98}}
    return settings, models, gateway, calls, decision, current



@pytest.mark.parametrize('remaining,tier', [(100,'normal'),(25,'conserve'),(10,'critical'),(0,'exhausted'),(-10,'exhausted')])
def test_budget_pressure_thresholds(monkeypatch, remaining, tier):
    monkeypatch.setattr(budget_routing, 'budget_snapshot', lambda: report(remaining))
    assert budget_routing.state(config.DEFAULTS)['tier'] == tier


@pytest.mark.parametrize('change', [
    lambda v: v.update(state='sign_in_required'), lambda v: v.update(stale=True),
    lambda v: v['report'].update(month='2000-01'),
    lambda v: v['report'].update(source_stale=True),
    lambda v: v['report'].update(coverage_pending=True),
    lambda v: v['report'].update(budget_usd=0),
    lambda v: v['report'].update(remaining_usd=float('nan')),
    lambda v: v['report'].update(currency='EUR'),
    lambda v: v['report'].update(fetched_at=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()),
    lambda v: v['report'].update(source_updated_at=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()),
    lambda v: v['report'].pop('source_updated_at'),
])
def test_unreliable_balances_do_not_drive_routing(monkeypatch, change):
    value = report(); change(value)
    monkeypatch.setattr(budget_routing, 'budget_snapshot', lambda: value)
    assert budget_routing.state(config.DEFAULTS)['tier'] == 'unavailable'


def test_budget_chooses_cheapest_qualified_catalog_model(budget):
    _, models, gateway, calls, decision, _ = budget
    models.append({**model('unassigned'), 'pricing': {'input_per_million':.01,'output_per_million':.03}})
    decision['candidates']['unassigned'] = {'choice':'suitable','confidence':.97}
    _, meta = gateway.complete([{'role':'user','content':'Implement a parser'}])
    assert meta['model'] == 'unassigned'
    assert meta['routing']['baseline_model'] == 'code'
    assert meta['routing']['quality_confidence'] == .97
    assert meta['routing']['budget']['estimated_saving_percent'] == 99.9
    payload = calls[0][1]['json']
    assert 'remaining_usd' not in json.dumps(payload)
    assert 'estimated_request_cost_usd' in payload['messages'][-1]['content']


@pytest.mark.parametrize('confidence', [.84, True, float('nan'), 2, '1'])
def test_budget_never_qualifies_an_uncertain_model_just_because_it_is_cheap(budget, confidence):
    _, _, gateway, _, decision, _ = budget
    decision['candidates']['fast']['confidence'] = confidence
    assert gateway.complete([])[1]['model'] == 'code'


def test_normal_manual_and_disabled_budget(budget, monkeypatch):
    settings, _, gateway, _, _, current = budget
    current['report']['remaining_usd'] = 50
    assert gateway.complete([])[1]['model'] == 'code'
    settings['model_routing']['budget_aware'] = False
    monkeypatch.setattr(budget_routing, 'budget_snapshot', lambda: pytest.fail('Disabled budget must not read balance'))
    assert gateway.complete([])[1]['model'] == 'code'
    settings['model_routing']['enabled'] = False
    assert gateway.complete([])[1]['model'] == 'work'


def test_budget_changes_and_prices_reassess_pinned_models(budget):
    _, models, gateway, calls, _, current = budget
    current['report']['remaining_usd'] = 50
    history = [{'role':'user','content':'Task'}]
    answer, meta = gateway.complete(history)
    assert meta['model'] == 'code'
    current['report']['remaining_usd'] = 20
    answer2, meta = gateway.complete(history+[answer])
    assert meta['model'] == 'fast' and len(calls) == 4
    _, meta = gateway.complete(history+[answer2])
    assert meta['model'] == 'fast' and len(calls) == 5
    assert meta['routing']['budget']['estimated_saving_percent'] == 99
    models[1]['pricing']['output_per_million'] = 300
    assert gateway.complete(history+[answer2])[1]['model'] == 'code'


def test_critical_budget_accepts_smaller_savings_with_same_quality_gate(budget):
    _, models, gateway, _, _, current = budget
    models[1]['pricing'] = {'input_per_million':9,'output_per_million':27}
    assert gateway.complete([])[1]['model'] == 'code'
    current['report']['remaining_usd'] = 10
    _, meta = gateway.complete([])
    assert meta['model'] == 'fast' and meta['routing']['budget']['estimated_saving_percent'] == 10


def test_owner_can_require_higher_suitability_confidence(budget):
    settings, _, gateway, _, decision, _ = budget
    settings['model_routing']['min_confidence'] = .99
    assert gateway.complete([])[1]['model'] == 'code'
    decision['candidates']['fast']['confidence'] = 1
    decision['candidates']['code']['confidence'] = 1
    assert gateway.complete([])[1]['model'] == 'fast'


@pytest.mark.parametrize('exclusion', ['tools','context','capacity','unknown_price','image'])
def test_cheap_candidates_keep_capability_capacity_and_price_filters(budget, exclusion):
    _, models, gateway, _, decision, _ = budget
    messages = [{'role':'user','content':'Task'}]
    tools = [{'type':'function','function':{'name':'test'}}]
    if exclusion == 'tools': models[1]['capabilities']['tools'] = False
    elif exclusion == 'context': models[1]['context_length'] = 100
    elif exclusion == 'capacity': gateway.capacity.store.set_state('capacity:fast',{'paused':True})
    elif exclusion == 'unknown_price': models[1].pop('pricing')
    else:
        messages = [{'role':'user','content':[{'type':'image_url','image_url':{'url':'data:image/png;base64,AA=='}}]}]
        decision['candidates']['vision'] = {'choice':'suitable','confidence':.99}
    assert gateway.complete(messages,tools=tools)[1]['model'] != 'fast'


def test_prices_use_long_context_and_reasoning_rates_without_treating_missing_as_free():
    priced = {'pricing': {'input_per_million':1,'output_per_million':2,'reasoning_per_million':4,
                         'long_context_threshold':1000,'input_long_context_per_million':3,'output_long_context_per_million':6}}
    assert budget_routing.estimate(priced,2000,1000) == .012
    assert budget_routing.estimate(priced,1000,1000) == .005
    assert budget_routing.estimate({},1000,1000) is None
    priced['pricing'].pop('input_long_context_per_million')
    assert budget_routing.estimate(priced,2000,1000) is None


def test_economy_models_do_not_force_smaller_context_before_they_are_selected(budget):
    from cos import model_routing
    settings, models, *_ = budget
    models.append({**model('small'), 'context_length': 16384})
    settings['model_routing']['budget_profiles'] = {'coding':'small'}
    assert model_routing.context_window(settings, 'worker') == 128000


def test_model_prices_are_allowlisted_without_deployment_or_secret_fields():
    value = model('priced'); value['pricing'] = {'input_per_million':1,'output_per_million':2,'key':'do-not-export'}
    result = model_catalog.normalize(value)
    assert result['pricing'] == {'input_per_million':1,'output_per_million':2}
    value['pricing']['input_per_million'] = float('inf')
    assert 'pricing' not in model_catalog.normalize(value)


def test_ui_and_routing_share_a_connection_without_sharing_other_roots(monkeypatch, tmp_path):
    monkeypatch.setattr(tokenops, '_connections', {})
    assert tokenops.connection() is tokenops.connection()
    prior = tokenops.connection()
    monkeypatch.setattr(config, 'ROOT', tmp_path/'other')
    assert tokenops.connection() is not prior


def test_job_selection_summary_exposes_reason_and_savings_without_raw_metadata(budget):
    from cos import model_routing
    _, _, gateway, *_ = budget
    store = gateway.capacity.store
    job = store.create_job('Task','Technology')
    store.event(job['id'], 'model', json.dumps({'model':'fast','api_key':'private-secret',
        'routing':{'reason':'Budget-aware choice','budget':{'tier':'critical','estimated_saving_percent':95}}}))
    detail = model_routing.selection_detail(store, job['id'])
    assert detail['estimated_saving_percent'] == 95 and detail['model'] == 'fast'
    assert 'private-secret' not in str(detail)
    store.event(job['id'], 'model', 'invalid legacy record')
    assert model_routing.selection_detail(store, job['id']) is None
