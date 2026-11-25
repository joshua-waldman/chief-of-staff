import json
import pytest
import httpx
from cos import config, model_catalog, model_routing
from test_model_routing import routed, model


def decision_calls(calls):
    from cos.model_suitability import SYSTEM
    return [kw['json'] for url, kw in calls if (kw['json'].get('messages') or [{}])[0].get('content') == SYSTEM]


def test_full_catalog_is_assessed_in_bounded_batches_without_assignment_shortlists(routed):
    settings, models, gateway, calls, decision = routed
    models.extend(model('new-' + str(i).zfill(3)) for i in range(60))
    decision['choice'] = 'new-059'
    models[-61]['context_length'] = 12000
    settings['model_routing']['profiles'] = {'fast': 'work'}
    answer, meta = gateway.complete([{'role': 'user', 'content': 'Write a careful analysis'}])
    assert meta['model'] == 'new-059'
    requests = decision_calls(calls)
    assessed = [m['id'] for r in requests for m in json.loads(r['messages'][-1]['content'])['candidates']]
    expected = {m['id'] for m in models if model_catalog.supports(m, 'chat')}
    assert set(assessed) == expected and len(assessed) == len(expected)
    assert len(requests) > 1
    assert all(len(json.dumps(r['messages']).encode()) <= 12000-8192 for r in requests)
    assert meta['routing']['eligible_models_count'] == len(expected)
    assert meta['routing']['router_usage']['requests'] == len(requests)
    before = len(requests)
    assert gateway.complete([{'role':'user','content':'Write a careful analysis'}, answer])[1]['model'] == 'new-059'
    assert len(decision_calls(calls)) == before


def test_new_catalog_model_can_win_after_a_pinned_choice(routed):
    _, models, gateway, calls, decision = routed
    history = [{'role':'user','content':'Design a parser'}]
    answer, _ = gateway.complete(history)
    models.append(model('new-expert'))
    decision.update(choice='new-expert')
    _, meta = gateway.complete(history+[answer])
    assert meta['model'] == 'new-expert' and len(decision_calls(calls)) == 2


def test_unassigned_vision_model_can_replace_incompatible_configured_models(routed):
    _, models, gateway, calls, decision = routed
    models.append(model('new-vision', vision=True))
    models[4]['capabilities']['tools'] = False
    decision['choice'] = 'new-vision'
    history = [{'role':'user','content':[{'type':'text','text':'Read the error in this screenshot'},
        {'type':'image_url','image_url':{'url':'data:image/png;base64,AA=='}}]}]
    _, meta = gateway.complete(history, tools=[{'type':'function','function':{'name':'test'}}])
    assert meta['model'] == 'new-vision'
    state = json.loads(decision_calls(calls)[0]['messages'][-1]['content'])
    assert state['requests'] == ['Read the error in this screenshot']
    assert 'data:image' not in json.dumps(state)
    assert set(m['id'] for m in state['candidates']) == {'new-vision'}


def test_partial_batch_failure_uses_role_fallback_instead_of_a_partial_shortlist(routed, monkeypatch):
    _, models, gateway, calls, _ = routed
    models.extend(model('extra-' + str(i)) for i in range(30))
    models[7]['context_length'] = 12000
    original = gateway._complete
    count = 0
    def fail_second(*args, **kwargs):
        nonlocal count
        if kwargs.get('role') != 'model_selector':
            return original(*args, **kwargs)
        count += 1
        if count == 2:
            raise RuntimeError('decision service unavailable')
        return original(*args, **kwargs)
    monkeypatch.setattr(gateway, '_complete', fail_second)
    _, meta = gateway.complete([])
    assert count == 2 and meta['model'] == 'work' and meta['routing']['fallback']
    assert calls[-1][1]['json']['model'] == 'work'


def test_independent_suitability_is_required_for_an_unassigned_candidate(routed):
    _, models, gateway, _, decision = routed
    models.append(model('new-specialist'))
    decision['candidates'] = {'new-specialist': {'choice':'suitable', 'confidence':.97}}
    decision['choice'] = 'unlisted-model'
    assert gateway.complete([])[1]['model'] == 'new-specialist'
    decision['candidates']['new-specialist']['confidence'] = .84
    assert gateway.complete([])[1]['model'] == 'work'


def test_catalog_guidance_is_bounded_and_cannot_expose_deployment_fields():
    item = {**model('new'), 'description':'x'*5000, 'strengths':['coding', 123],
            'limitations':['Uncertain image understanding'], 'keyName':'secret-deployment',
            'stats':{'private':1}, 'pricing':{'input_per_million':1,'output_per_million':2}}
    normalized = model_catalog.normalize(item)
    assert len(normalized['description']) == 600
    assert normalized['strengths'] == ['coding']
    assert 'keyName' not in normalized and 'stats' not in normalized


def test_legacy_mappings_do_not_block_saving_or_enter_the_candidate_pool(routed):
    settings, _, _, _, _ = routed
    saved = config.save_settings({'model_routing':{'profiles':{'coding':'audio'},
                                'budget_profiles':{'fast':'audio'}}})
    assert saved['model_routing']['profiles']['coding'] == 'audio'


def test_long_context_price_boundary_reassesses_a_pinned_model(routed):
    _, models, gateway, calls, _ = routed
    models[3]['pricing'] = {'input_per_million':1, 'output_per_million':2,
                            'long_context_threshold':5000,
                            'input_long_context_per_million':3,'output_long_context_per_million':6}
    history = [{'role':'user','content':'Task'}]
    answer, _ = gateway.complete(history)
    gateway.complete(history+[answer, {'role':'tool','tool_call_id':'example','content':'x'*6000}])
    assert len(decision_calls(calls)) == 2


@pytest.mark.parametrize('proposal', [
    {'model':'outside-catalog','suitability':1,'strengths':'Good','limitations':'None'},
    {'model':'audio','suitability':1,'strengths':'Good','limitations':'None'},
    {'model':'code','suitability':True,'strengths':'Good','limitations':'None'},
    {'model':'code','suitability':.99,'strengths':'','limitations':'None'},
    {'model':'code','suitability':.99,'strengths':'Good'},
])
def test_invalid_or_incompatible_advisor_proposals_cannot_execute(routed, proposal):
    _, _, gateway, calls, decision = routed
    decision['raw_proposals'] = [proposal]
    _, meta = gateway.complete([{'role':'user','content':'Write code'}])
    assert meta['model'] == 'work' and meta['routing']['fallback']
    assert calls[-1][1]['json']['model'] == 'work'


def test_advisor_calls_are_counted_without_logging_rationale_or_task_text(routed):
    _, _, gateway, _, _ = routed
    _, meta = gateway.complete([{'role':'user','content':'private task material'}])
    rows = gateway.capacity.store.rows('SELECT model,purpose,role FROM model_usage ORDER BY created')
    assert rows == [{'model':'advisor','purpose':'routing','role':'model_selector'},
                    {'model':'code','purpose':'agent','role':'worker'}]
    assert all(value not in json.dumps(meta) for value in ('private task material','Good task fit','Assessment uncertainty'))


def test_nonchat_advisor_is_rejected_by_settings_validation(routed):
    with pytest.raises(ValueError, match='advisor must support chat'):
        config.save_settings({'model_routing':{'selector_model':'decision'}})


@pytest.mark.parametrize('status', [403,404,410,426])
def test_definite_model_rejection_chooses_another_candidate_and_remembers_unavailability(routed, monkeypatch, status):
    from cos import provider
    _, models, gateway, calls, decision = routed
    models.append(model('replacement'))
    decision['candidates'] = {'code':{'choice':'suitable','confidence':.99},
                              'replacement':{'choice':'suitable','confidence':.95}}
    original = provider.httpx.post
    def reject(url, **kwargs):
        if kwargs['json']['model'] == 'code':
            calls.append((url,kwargs))
            return httpx.Response(status)
        return original(url, **kwargs)
    monkeypatch.setattr(provider.httpx,'post',reject)
    answer, meta = gateway.complete([{'role':'user','content':'Implement code'}])
    assert meta['model'] == 'replacement'
    assert sum(kw['json']['model']=='code' for _,kw in calls) == 1
    assert len(decision_calls(calls)) == 2
    restarted = provider.Gateway(gateway.capacity.store)
    assert 'code' in restarted.unavailable_models(provider.settings())
    assert gateway.capacity.store.one("SELECT COUNT(*) AS n FROM model_usage WHERE status='rejected'")['n'] == 1
    monkeypatch.setattr(provider,'gateway_key',lambda:'another-synthetic-key')
    assert 'code' not in restarted.unavailable_models(provider.settings())


@pytest.mark.parametrize('failure', [401,400,500,'timeout'])
def test_ambiguous_or_non_model_errors_never_trigger_cross_model_replay(routed, monkeypatch, failure):
    from cos import provider
    _, _, gateway, calls, _ = routed
    original = provider.httpx.post
    def reject(url, **kwargs):
        if kwargs['json']['model'] == 'code':
            calls.append((url,kwargs))
            if failure == 'timeout':
                raise httpx.TimeoutException('unknown outcome')
            return httpx.Response(failure,json={})
        return original(url, **kwargs)
    monkeypatch.setattr(provider.httpx,'post',reject)
    with pytest.raises((RuntimeError,provider.NotConfigured)):
        gateway.complete([{'role':'user','content':'Implement code'}],attempts=1)
    assert len(decision_calls(calls)) == 1
    assert sum(kw['json']['model']=='code' for _,kw in calls) == 1
    assert not gateway.unavailable_models(provider.settings())


def test_rejected_catalog_attempts_are_bounded_and_end_at_the_known_role_model(routed, monkeypatch):
    from cos import provider
    _, models, gateway, calls, decision = routed
    models.append(model('replacement'))
    decision['candidates'] = {'code':{'choice':'suitable','confidence':.99},
                              'replacement':{'choice':'suitable','confidence':.95}}
    original = provider.httpx.post
    def reject(url, **kwargs):
        if kwargs['json']['model'] in {'code','replacement'}:
            calls.append((url,kwargs));return httpx.Response(426)
        return original(url,**kwargs)
    monkeypatch.setattr(provider.httpx,'post',reject)
    _, meta = gateway.complete([{'role':'user','content':'Implement code'}])
    assert meta['model'] == 'work' and meta['routing']['fallback']
    assert len(decision_calls(calls)) == 2
    executed = [kw['json']['model'] for _,kw in calls if kw['json']['model']!='advisor']
    assert executed == ['code','replacement','work']


def test_manual_selection_is_not_changed_after_model_rejection(routed, monkeypatch):
    from cos import provider
    settings, _, gateway, calls, _ = routed
    settings['model_routing']['enabled'] = False
    def reject(url, **kwargs):
        calls.append((url,kwargs));return httpx.Response(426)
    monkeypatch.setattr(provider.httpx,'post',reject)
    with pytest.raises(provider.ModelUnavailable):
        gateway.complete([{'role':'user','content':'Task'}])
    assert len(calls) == 1 and calls[0][1]['json']['model'] == 'work'


def test_ambiguous_attempt_before_a_model_rejection_is_not_replayed_on_another_model(routed, monkeypatch):
    from cos import provider
    _, _, gateway, calls, _ = routed
    original = provider.httpx.post
    statuses = iter((503,426))
    def reject(url, **kwargs):
        if kwargs['json']['model'] == 'code':
            calls.append((url,kwargs));return httpx.Response(next(statuses))
        return original(url,**kwargs)
    monkeypatch.setattr(provider.httpx,'post',reject)
    monkeypatch.setattr(provider.time,'sleep',lambda _:None)
    with pytest.raises(provider.NotConfigured):
        gateway.complete([{'role':'user','content':'Implement code'}],attempts=2)
    assert len(decision_calls(calls)) == 1
    assert sum(kw['json']['model']=='code' for _,kw in calls) == 2
