"""Read AMD's documented key-scoped UsageStats endpoint; never infer a balance."""
import copy
import hashlib
import math
import threading
import time
from datetime import datetime, timezone
import httpx
from .config import settings, gateway_key, DEFAULTS
from . import config as app_config
from .usage import count

URL = 'https://llm-api.amd.com/api/UsageStats'
PORTAL = 'https://llm.amd.com/dashboard/tokenops/my-usage'
_lock = threading.Lock()
_cache = {}
_running = set()


def empty():
    return {'state': 'unavailable', 'scope': 'api_key', 'fetched_at': None,
            'stale': False, 'error': '', 'balance': None,
            'balance_reason': 'The verified gateway usage API does not report an allocation or remaining balance.',
            'portal_url': PORTAL}


def fetch(key, start, end):
    response = httpx.get(URL, params={'start': start, 'end': end},
                         headers={'Authorization': 'Bearer ' + key, 'Ocp-Apim-Subscription-Key': key},
                         timeout=30, follow_redirects=False)
    if response.status_code != 200:
        raise ValueError('Gateway usage request failed.')
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError('Invalid gateway usage response.')
    requests, tokens = count(data.get('totalRequests')), count(data.get('totalTokens'))
    cost = data.get('approxChargeInUSD')
    if requests is None or tokens is None:
        raise ValueError('Invalid gateway usage totals.')
    cost = cost if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0 else None
    # application.apiKey is returned in cleartext by this endpoint. No raw response,
    # application metadata, model rows, or error bodies may enter cache or logs.
    return {**empty(), 'state': 'ready', 'fetched_at': datetime.now(timezone.utc).isoformat(),
            'period_start': start, 'period_end': end, 'requests': requests,
            'total_tokens': tokens, 'approx_charge_usd': cost}


def _refresh(identity, key, start, end):
    try:
        result = fetch(key, start, end)
        ttl = 300
    except Exception:
        with _lock:
            previous = _cache.get(identity, {}).get('result', empty())
        result = {**previous, 'stale': previous.get('fetched_at') is not None,
                  'error': 'Unable to refresh gateway usage. Check key access and AMD network/VPN.'}
        ttl = 60
    with _lock:
        _cache[identity] = {'result': result, 'retry_at': time.time() + ttl}
        _running.discard(identity)


def snapshot(refresh=False):
    config, key = settings(), gateway_key()
    if config['gateway_url'] != DEFAULTS['gateway_url'] or not key:
        return {**empty(), 'error': 'Connect a verified AMD gateway key to read gateway usage.'}
    stamp = datetime.now(timezone.utc)
    start = stamp.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    identity = (str(app_config.ROOT), hashlib.sha256(key.encode()).hexdigest(), start)
    with _lock:
        cached = _cache.get(identity, {})
        if identity not in _running and (refresh or time.time() >= cached.get('retry_at', 0)):
            _running.add(identity)
            try:
                threading.Thread(target=_refresh, args=(identity, key, start, stamp.isoformat()),
                                 daemon=True, name='cos-gateway-usage').start()
            except Exception:
                _running.discard(identity)
                return {**empty(), 'error': 'Gateway usage refresh could not start.'}
        result = copy.deepcopy(cached.get('result', {**empty(), 'state': 'loading'}))
        result['refreshing'] = identity in _running
        return result
