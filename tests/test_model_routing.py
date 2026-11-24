import copy
import json
import pytest
from cos import config, model_catalog, model_routing, provider
from cos.db import Store
from cos.capacity import CapacityDelayed


class Response:
    status_code = 200
    headers = {}
    def __init__(self, data):
        self.data = data
    def json(self):
        return self.data


def model(id, mode='chat', **caps):
    return {'id': id, 'mode': mode, 'context_length': 128000,
            'capabilities': {'chat': mode == 'chat', 'decision': mode == 'decision', **caps}}


@pytest.fixture
def routed(tmp_path, monkeypatch):
    settings = copy.deepcopy(config.DEFAULTS)
    settings['models'] = {'chief': 'work', 'director': 'work', 'worker': 'work', 'triage': 'fast'}
    settings['model_routing'].update(enabled=True, router_model='decision',
        profiles={'fast': 'fast', 'balanced': 'work', 'reasoning': 'strong', 'coding': 'code', 'vision': 'vision'})
    models = [model('work'), model('fast'), model('strong'), model('code'),
              model('vision', vision=True), model('decision', 'decision'), model('audio', 'realtime')]
    monkeypatch.setattr(provider, 'settings', lambda: settings)
    monkeypatch.setattr(provider, 'gateway_key', lambda: 'synthetic-key')
    monkeypatch.setattr(model_catalog, 'catalog', lambda **kw: {'models': models, 'error': ''})
    monkeypatch.setattr(model_catalog, 'snapshot', lambda *args: models)
    gateway = provider.Gateway(Store(tmp_path/'routing.db'))
    calls = []
    decisions = {'choice': 'coding', 'probabilities': {'coding': .98}}
    def post(url, **kw):
        calls.append((url, kw))
        if url.endswith('/systemone'):
            return Response({'answers': {'profile': decisions}, 'usage': {'input_tokens': 42}})
        return Response({'choices': [{'message': {'role': 'assistant', 'content': 'OK'}}]})
    monkeypatch.setattr(provider.httpx, 'post', post)
    return settings, models, gateway, calls, decisions


def test_manual_mode_and_excluded_roles_never_call_router(routed):
    settings, _, gateway, calls, _ = routed
    gateway.complete([{'role': 'user', 'content': 'Implement code'}], role='chief')
    settings['model_routing']['enabled'] = False
    _, meta = gateway.complete([{'role': 'user', 'content': 'Implement code'}])
    assert [kw['json']['model'] for _, kw in calls] == ['work', 'work']
    assert meta['routing']['mode'] == 'manual'


def test_decision_routes_and_pins_across_gateway_instances_without_leaking_state(routed):
    settings, _, gateway, calls, _ = routed
    history = [{'role': 'user', 'content': 'Implement a parser'}]
    answer, meta = gateway.complete(history)
    assert meta['model'] == 'code'
    assert meta['routing']['router_usage'] == {'input_tokens': 42}
    assert calls[0][0].endswith('/systemone')
    assert calls[0][1]['timeout'] == 10
    history += [answer]
    answer2, meta2 = provider.Gateway(gateway.capacity.store).complete(history)
    assert len(calls) == 3
    assert meta2['model'] == 'code'
    assert 'router_usage' not in meta2['routing']
    assert 'Retained' in meta2['routing']['reason']
    wire = calls[-1][1]['json']['messages']
    assert not any(k.startswith('_') for m in wire for k in m)
    settings['model_routing']['profiles']['coding'] = 'strong'
    _, meta3 = gateway.complete(history + [answer2])
    assert meta3['model'] == 'strong'
    assert len(calls) == 5


def test_new_user_request_allows_a_new_profile(routed):
    _, _, gateway, calls, decision = routed
    history = [{'role': 'user', 'content': 'Implement a parser'}]
    answer, _ = gateway.complete(history)
    decision.update(choice='fast', probabilities={'fast': .99})
    _, meta = gateway.complete(history + [answer, {'role': 'user', 'content': 'Now summarize the result'}])
    assert len(calls) == 4 and meta['model'] == 'fast'


@pytest.mark.parametrize('decision', [
    {'choice': 'coding', 'confidence': .4}, {'choice': 'external-model', 'confidence': 1},
    {'choice': 'coding', 'confidence': '1'}, {'choice': 'coding', 'confidence': float('nan')},
    {'choice': 'coding', 'confidence': 2}, {},
])
def test_invalid_or_uncertain_decisions_use_role_assignment(routed, decision):
    _, _, gateway, calls, answer = routed
    answer.clear()
    answer.update(decision)
    _, meta = gateway.complete([{'role': 'user', 'content': 'Task'}])
    assert meta['model'] == 'work'
    assert meta['routing']['fallback']
    assert calls[-1][1]['json']['model'] == 'work'


@pytest.mark.parametrize('failure', ['catalog', 'router', 'access', 'invalid-json', 'cooldown'])
def test_router_failures_do_not_block_assigned_model(routed, monkeypatch, failure):
    _, _, gateway, calls, _ = routed
    if failure == 'catalog':
        monkeypatch.setattr(model_catalog, 'catalog', lambda: {'models': [], 'error': 'offline'})
    elif failure == 'cooldown':
        gateway.capacity.store.set_state('capacity:decision', {'paused': True})
    else:
        def fail(*args, **kw):
            if failure == 'access':
                raise provider.NotConfigured('denied')
            if failure == 'invalid-json':
                raise ValueError('bad response')
            raise RuntimeError('offline')
        monkeypatch.setattr(gateway, 'decide', fail)
    _, meta = gateway.complete([{'role': 'user', 'content': 'Task'}])
    assert meta['model'] == 'work' and meta['routing']['fallback']
    assert len(calls) == 1


def test_routing_filters_images_context_tools_capacity_and_nonchat(routed):
    settings, models, gateway, calls, decision = routed
    decision.update(choice='vision', probabilities={'vision': .99})
    history = [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}}]}]
    _, meta = gateway.complete(history)
    assert meta['model'] == 'vision'
    assert len(calls) == 1 and calls[0][0].endswith('/chat/completions')
    assert meta['routing']['reason'] == 'Only eligible task model'
    calls.clear()
    settings['model_routing']['profiles']['balanced'] = 'audio'
    models[1]['context_length'] = 4096
    models[2]['capabilities']['tools'] = False
    gateway.capacity.store.set_state('capacity:code', {'paused': True})
    decision.update(choice='vision', probabilities={'vision': .99})
    _, meta = gateway.complete([{'role': 'user', 'content': 'x'*6000}], tools=[{'type': 'function', 'function': {'name': 'test'}}])
    assert len(calls) == 1
    assert meta['model'] == 'vision'


def test_actual_target_capacity_is_enforced_after_routing(routed, monkeypatch):
    _, _, gateway, _, _ = routed
    original = gateway.decide
    def decide(*args, **kw):
        result = original(*args, **kw)
        gateway.capacity.store.set_state('capacity:code', {'paused': True})
        return result
    monkeypatch.setattr(gateway, 'decide', decide)
    with pytest.raises(CapacityDelayed) as error:
        gateway.complete([{'role': 'user', 'content': 'Task'}])
    assert error.value.model == 'code'


def test_context_budget_respects_smallest_routed_window_and_manual_assignment(routed):
    settings, models, *_ = routed
    models[3]['context_length'] = 32768
    assert model_routing.context_window(settings, 'worker') == 32768
    settings['model_routing']['enabled'] = False
    assert model_routing.context_window(settings, 'worker') == 128000


def test_routing_settings_validation_and_partial_updates(routed, monkeypatch):
    monkeypatch.setattr(model_catalog, 'snapshot', lambda *args: [model('decision', 'decision')])
    saved = config.save_settings({'model_routing': {'profiles': {'coding': 'code'}}})
    assert saved['model_routing']['enabled'] is False
    saved = config.save_settings({'model_routing': {'enabled': True}})
    assert saved['model_routing']['profiles']['coding'] == 'code'
    for invalid in ({'enabled': 'yes'}, {'min_confidence': float('nan')}, {'roles': ['unknown']},
                    {'profiles': {'unknown': 'work'}}, {'profiles': {'fast': ''}}, {'roles': []}):
        with pytest.raises(ValueError):
            config.save_settings({'model_routing': invalid})
    with pytest.raises(ValueError, match='chat'):
        config.save_settings({'models': {'worker': 'decision'}})
    assert config.settings()['models']['worker'] == config.DEFAULTS['models']['worker']


def test_deepseek_reasoning_stays_on_its_model(routed):
    settings, models, gateway, calls, _ = routed
    settings['model_routing']['enabled'] = False
    settings['models']['worker'] = 'DeepSeek-V4-Pro'
    models.append(model('DeepSeek-V4-Pro'))
    prior = {'role': 'assistant', 'content': 'Continue', 'reasoning_content': 'private-state', '_cos_model': 'DeepSeek-V4-Pro'}
    gateway.complete([prior])
    assert calls[-1][1]['json']['messages'][0]['reasoning_content'] == 'private-state'
    settings['models']['worker'] = 'work'
    gateway.complete([prior])
    assert 'reasoning_content' not in calls[-1][1]['json']['messages'][0]
    assert prior['reasoning_content'] == 'private-state'


def test_gpt_oss_uses_chat_protocol(routed):
    settings, models, gateway, calls, _ = routed
    settings['model_routing']['enabled'] = False
    settings['models']['worker'] = 'GPT-oss-120B'
    models.append(model('GPT-oss-120B'))
    gateway.complete([{'role': 'user', 'content': 'Task'}], tools=[{'type': 'function', 'function': {'name': 'test'}}])
    assert calls[-1][0].endswith('/chat/completions')


@pytest.mark.parametrize('tools', [None, [{'type': 'function', 'function': {'name': 'test'}}]])
def test_responses_only_models_are_assignable_with_or_without_tools(routed, monkeypatch, tools):
    settings, models, gateway, calls, _ = routed
    settings['model_routing']['enabled'] = False
    settings['models']['worker'] = 'gpt-5.3-codex'
    models.append(model('gpt-5.3-codex', chat=False, responses=True))
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response({'output': [], 'status': 'completed'})
    monkeypatch.setattr(provider.httpx, 'post', post)
    assert model_catalog.supports(models[-1], 'chat')
    gateway.complete([{'role': 'user', 'content': 'Task'}], tools=tools)
    assert calls[-1][0].endswith('/responses')
    assert bool(calls[-1][1]['json'].get('tools')) == bool(tools)


def test_nonreasoning_gpt_does_not_receive_reasoning_parameters(routed, monkeypatch):
    settings, models, gateway, calls, _ = routed
    settings['model_routing']['enabled'] = False
    settings['models']['worker'] = 'gpt-4.1'
    models.append(model('gpt-4.1', responses=True))
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response({'output': [], 'status': 'completed'})
    monkeypatch.setattr(provider.httpx, 'post', post)
    gateway.complete([{'role': 'user', 'content': 'Task'}], tools=[{'type': 'function', 'function': {'name': 'test'}}])
    assert 'reasoning' not in calls[-1][1]['json']


def test_custom_assignment_discovers_transport_after_restart(routed, monkeypatch):
    settings, models, gateway, calls, _ = routed
    settings['model_routing']['enabled'] = False
    settings['models']['worker'] = 'responses-only'
    models.append(model('responses-only', chat=False, responses=True))
    monkeypatch.setattr(model_catalog, 'snapshot', lambda *args: [])
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response({'output': [], 'status': 'completed'})
    monkeypatch.setattr(provider.httpx, 'post', post)
    gateway.complete([{'role': 'user', 'content': 'Task'}])
    assert calls[-1][0].endswith('/responses')


def test_manual_model_capabilities_and_context_are_enforced_before_inference(routed):
    settings, models, gateway, calls, _ = routed
    settings['model_routing']['enabled'] = False
    settings['models']['worker'] = 'audio'
    with pytest.raises(provider.NotConfigured, match='chat'):
        gateway.complete([{'role': 'user', 'content': 'Task'}])
    settings['models']['worker'] = 'fast'
    models[1]['context_length'] = 16384
    with pytest.raises(provider.ContextWindowExceeded):
        gateway.complete([{'role': 'user', 'content': 'x'*20000}])
    models[1]['capabilities']['tools'] = False
    with pytest.raises(provider.NotConfigured, match='tools'):
        gateway.complete([{'role': 'user', 'content': 'Task'}], tools=[{'type': 'function'}])
    assert not calls


def test_scheduler_does_not_preblock_auto_routing_on_role_model_capacity(routed, monkeypatch):
    from cos.engine import Engine
    settings, _, gateway, *_ = routed
    monkeypatch.setattr(config, 'settings', lambda: settings)
    from cos import engine as engine_module
    monkeypatch.setattr(engine_module, 'settings', lambda: settings)
    job = gateway.capacity.store.create_job('Research task', 'Research')
    engine = Engine(gateway.capacity.store, gateway)
    gateway.capacity.store.set_state('capacity:work', {'paused': True})
    assert engine.capacity_waiting(job) is False
    settings['model_routing']['enabled'] = False
    assert engine.capacity_waiting(job) is True
