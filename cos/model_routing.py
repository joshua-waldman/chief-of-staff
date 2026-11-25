"""Catalog-wide task suitability routing with independent quality and cost gates."""
import hashlib
import json
import math
from . import model_catalog
from . import budget_routing
from .capacity import CapacityDelayed

PROFILES = {
    'fast': 'Routine extraction, classification, short summaries and simple transformations.',
    'balanced': 'General research, writing, communication and ordinary tool-assisted work.',
    'reasoning': 'Difficult analysis, planning, competing constraints and complex decisions.',
    'coding': 'Implementing, debugging, testing or reviewing source code.',
    'vision': 'Understanding an image, screenshot, chart or scanned page.'}


def enabled(config, role):
    routing = config.get('model_routing', {})
    return routing.get('enabled', False) and role in routing.get('roles', [])


def window(config, model, metadata=None):
    limit = config.get('model_context_windows', {}).get(model, config.get('context_window_tokens', 128000))
    discovered = (metadata or {}).get('context_length')
    return min(limit, discovered) if discovered else limit


def context_window(config, role):
    # Keep a safe recovery budget for the assigned fallback. A small unrelated
    # catalog model must never force compaction just to enter the candidate pool.
    metadata = next((m for m in model_catalog.snapshot(config)
                     if m['id'] == config['models'][role]), None)
    return window(config, config['models'][role], metadata)


def has_images(messages):
    return any(isinstance(m.get('content'), list) and
               any(isinstance(part, dict) and part.get('type') == 'image_url' for part in m['content'])
               for m in messages)


def signature(config, role, messages):
    routing = config.get('model_routing', {})
    value = {'routing': {k: routing.get(k) for k in (
                'enabled', 'roles', 'selector_model', 'min_confidence', 'budget_aware')},
             'role': role, 'models': config['models'],
             'requests': [m.get('content') for m in messages if m.get('role') == 'user']}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def selection_detail(store, job_id):
    row = store.one("SELECT body FROM events WHERE job_id=? AND kind='model' ORDER BY id DESC LIMIT 1", (job_id,))
    try:
        value = json.loads(row['body']) if row else {}
        routing = value.get('routing') or {}
        budget = routing.get('budget') or {}
        saving = budget.get('estimated_saving_percent')
        confidence = routing.get('quality_confidence')
        model, reason = value.get('model'), routing.get('reason', '')
        if not isinstance(model, str) or not isinstance(reason, str):
            return None
        return {'model': model[:200], 'reason': reason[:250],
                **{key: routing[key] for key in ('eligible_models_count', 'qualified_models_count')
                   if type(routing.get(key)) is int and 0 <= routing[key] <= 1000000},
                'quality_confidence': confidence if type(confidence) in (int, float)
                    and math.isfinite(confidence) and 0 <= confidence <= 1 else None,
                'budget_tier': budget.get('tier') if budget.get('tier') in {'normal','conserve','critical','exhausted','disabled','unavailable'} else None,
                'estimated_saving_percent': saving if type(saving) in (int, float) and math.isfinite(saving) and 0 <= saving <= 100 else None}
    except (ValueError, TypeError, AttributeError):
        return None


def select(gateway, config, messages, role, tools, max_tokens):
    fallback = config['models'][role]
    manual = {'mode': 'manual', 'reason': 'Assigned role model', 'role': role}
    if not enabled(config, role):
        return fallback, manual
    budget = budget_routing.state(config)
    route = {'mode': 'automatic', 'role': role, 'signature': signature(config, role, messages), 'budget': budget}
    fallback_route = {**route, 'reason': 'Assigned role fallback', 'fallback': True}
    discovered = model_catalog.catalog()
    if discovered.get('error'):
        return fallback, {**fallback_route, 'reason': 'Model catalog unavailable'}
    catalog = {m['id']: m for m in discovered['models']}
    from .context import estimate
    required = estimate(messages, tools) + max_tokens + 2048
    images = has_images(messages)
    unavailable = gateway.unavailable_models(config)

    def eligible(model):
        if model in unavailable:
            return False
        meta = catalog.get(model)
        if not meta or not model_catalog.supports(meta, 'chat'):
            return False
        if images and not model_catalog.supports(meta, 'vision'):
            return False
        params = meta.get('supported_parameters')
        if tools and params is not None and 'tools' not in params:
            return False
        if meta.get('capabilities', {}).get('tools') is False:
            return False
        if window(config, model, meta) < required:
            return False
        if meta.get('max_output_tokens', max_tokens) < max_tokens:
            return False
        try:
            gateway.capacity.check(model)
        except CapacityDelayed:
            return False
        return True

    candidates = {m: meta for m, meta in catalog.items() if eligible(m)}
    incoming = required - max_tokens - 2048
    fingerprint = hashlib.sha256(json.dumps({
        'version': 2, 'catalog': candidates, 'budget_tier': budget['tier'],
        'uses_tools': bool(tools), 'has_images': images, 'output_allowance': max_tokens,
        'long_context': {m: incoming > (v.get('pricing', {}).get('long_context_threshold') or float('inf'))
                         for m, v in candidates.items()}}, sort_keys=True).encode()).hexdigest()
    route.update(catalog_fingerprint=fingerprint, eligible_models_count=len(candidates))
    fallback_route['eligible_models_count'] = len(candidates)
    for message in reversed(messages):
        prior = message.get('_cos_route', {}) if message.get('role') == 'assistant' else {}
        if (prior.get('signature') == route['signature'] and not prior.get('fallback')
                and prior.get('catalog_fingerprint') == fingerprint):
            model = prior.get('model')
            if model in candidates and eligible(model):
                return model, {**prior, 'budget': {**prior.get('budget', {}), **budget}, 'reason': 'Retained conversation model',
                    'estimated_request_cost_usd': None if images else budget_routing.estimate(candidates[model], incoming, max_tokens)}
            break
    if not candidates:
        return fallback, {**fallback_route, 'reason': 'No eligible gateway model'}
    router = config['model_routing'].get('selector_model') or config['models']['chief']
    if router in unavailable or router not in catalog or not model_catalog.supports(catalog[router], 'chat'):
        return fallback, {**fallback_route, 'reason': 'Model selection advisor unavailable'}
    from .provider import NotConfigured
    from .model_suitability import assess
    try:
        qualified, usage = assess(gateway, config, router, catalog[router], messages, role,
                                  candidates, incoming, max_tokens, bool(tools), images)
        # Recheck availability after assessment, before choosing or pinning a model.
        qualified = [row for row in qualified if eligible(row['model'])]
        if not qualified:
            return fallback, {**fallback_route, 'reason': 'No model met task suitability confidence',
                              'router_model': router, 'router_usage': usage}
        selected, baseline, saving = budget_routing.pick(qualified, budget)
        budget.update(action='lower_cost_model' if saving else 'best_task_fit',
                      estimated_saving_percent=saving)
        return selected['model'], {**route, 'qualified_models_count': len(qualified),
            'quality_confidence': selected['confidence'], 'baseline_model': baseline['model'],
            'estimated_request_cost_usd': selected['cost'],
            'reason': ('Budget-aware choice: cheaper model meets full task requirements' if saving else
                       'Best task suitability across eligible gateway models'),
            'router_model': router, 'router_usage': usage}
    except (RuntimeError, NotConfigured, ValueError, TypeError, KeyError, AttributeError):
        return fallback, {**fallback_route, 'reason': 'Routing decision unavailable', 'router_model': router}
