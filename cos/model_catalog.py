"""Account-scoped AMD model discovery. Never follow redirects with gateway keys."""
import copy
import hashlib
import math
import threading
import time
import httpx
from .config import settings, gateway_key, DEFAULTS
from . import config as app_config

_lock = threading.RLock()
_cache = {}
_active = None
TTL = 300


def _identity(key, config):
    return (config['gateway_url'], hashlib.sha256(key.encode()).hexdigest(), str(app_config.ROOT))


def normalize(item):
    """Expose capability metadata, not deployment keys or usage history."""
    if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id'].strip():
        return None
    result = {'id': item['id'], 'mode': item.get('mode', 'chat'),
              'owned_by': item.get('owned_by', ''),
              'capabilities': {k: v for k, v in (item.get('capabilities') or {}).items()
                               if isinstance(v, bool)}}
    # Optional public model guidance only; deployment keys and account usage
    # remain excluded. Most gateway models currently omit these descriptions.
    for name in ('description', 'strengths', 'limitations'):
        value = item.get(name)
        if isinstance(value, str):
            result[name] = value[:600]
        elif name != 'description' and isinstance(value, list):
            result[name] = [v[:150] for v in value[:4] if isinstance(v, str)]
    for name in ('context_length', 'max_output_tokens'):
        value = item.get(name)
        if type(value) is int and value > 0:
            result[name] = value
    for name in ('supported_parameters', 'supported_unified_operations'):
        if isinstance(item.get(name), list):
            result[name] = [x for x in item[name] if isinstance(x, str)]
    prices = item.get('pricing')
    if isinstance(prices, dict):
        safe = {name: prices[name] for name in (
            'input_per_million', 'output_per_million', 'reasoning_per_million',
            'thinking_per_million', 'input_long_context_per_million',
            'output_long_context_per_million', 'long_context_threshold')
            if type(prices.get(name)) in (int, float) and math.isfinite(prices[name]) and prices[name] >= 0}
        if 'input_per_million' in safe and 'output_per_million' in safe:
            result['pricing'] = safe
    return result


def supports(model, capability):
    # Responses-only models (for example Codex) can still serve COS agents.
    if capability == 'chat' and model.get('capabilities', {}).get('responses') is True:
        return True
    explicit = model.get('capabilities', {}).get(capability)
    if explicit is not None:
        return explicit
    if capability == 'chat':
        return model.get('mode', 'chat') == 'chat'
    if capability == 'decision':
        return model.get('mode') == 'decision'
    return False


def snapshot(config=None):
    """Inference may consult cached metadata without making discovery a dependency."""
    config = config or settings()
    with _lock:
        if not _active or _active[0] != config['gateway_url'] or _active[2] != str(app_config.ROOT):
            return []
        return copy.deepcopy(_cache.get(_active, {}).get('models', []))


def catalog(refresh=False):
    global _active
    config = settings()
    key = gateway_key()
    empty = {'models': [], 'stale': False, 'fetched_at': None}
    if config['gateway_url'] != DEFAULTS['gateway_url']:
        return {**empty, 'error': 'Unverified model endpoint blocked.'}
    if not key:
        with _lock:
            _active = None
        return {**empty, 'error': 'Save an AMD gateway key to load available models.'}
    identity = _identity(key, config)
    with _lock:
        _active = identity
        cached = _cache.get(identity, {})
        if not refresh and cached.get('retry_at', 0) > time.time():
            return copy.deepcopy({k: v for k, v in cached.items() if k != 'retry_at'})
        try:
            response = httpx.get(config['gateway_url'] + '/models',
                headers={'Authorization': 'Bearer ' + key, 'Ocp-Apim-Subscription-Key': key},
                timeout=15, follow_redirects=False)
            if response.status_code != 200:
                raise ValueError('Model catalog request failed (HTTP ' + str(response.status_code) + '). Check gateway access.')
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
                raise ValueError('The gateway returned an invalid model catalog.')
            models = {}
            for item in payload['data']:
                model = normalize(item)
                if model:
                    models[model['id']] = model
            if not models:
                raise ValueError('The gateway returned no available models.')
            result = {'models': sorted(models.values(), key=lambda m: m['id'].lower()),
                      'stale': False, 'error': '', 'fetched_at': time.time()}
            _cache[identity] = {**result, 'retry_at': time.time() + TTL}
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            # Do not return exception text or response bodies: either may contain credentials.
            result = {'models': cached.get('models', []), 'stale': bool(cached.get('models')),
                      'fetched_at': cached.get('fetched_at'),
                      'error': 'Unable to refresh the AMD model catalog. Check your key and network/VPN access. Saved assignments are preserved.'}
            _cache[identity] = {**result, 'retry_at': time.time() + 30}
        return copy.deepcopy(result)
