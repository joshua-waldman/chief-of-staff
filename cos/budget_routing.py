"""Budget pressure affects cost preference, never task or capability requirements."""
import math
from datetime import datetime, timezone

ACTIVE = {'conserve', 'critical', 'exhausted'}


def budget_snapshot():
    from .tokenops import connection
    return connection().snapshot()


def state(config):
    if not config.get('model_routing', {}).get('budget_aware', True):
        return {'tier': 'disabled'}
    try:
        value = budget_snapshot()
        report = value.get('report') or {}
        now = datetime.now(timezone.utc)
        if (value.get('state') != 'ready' or value.get('stale') or value.get('connecting')
                or report.get('source_stale') or report.get('coverage_pending')
                or report.get('currency') != 'USD' or report.get('month') != now.strftime('%Y-%m')):
            return {'tier': 'unavailable'}
        for field, limit in (('fetched_at', 900), ('source_updated_at', 86400)):
            stamp = datetime.fromisoformat(report[field].replace('Z', '+00:00'))
            age = (now - stamp).total_seconds()
            if age < -300 or age > limit:
                return {'tier': 'unavailable'}
        budget, remaining = report['budget_usd'], report['remaining_usd']
        if (type(budget) not in (int, float) or type(remaining) not in (int, float)
                or not math.isfinite(budget) or not math.isfinite(remaining) or budget <= 0):
            return {'tier': 'unavailable'}
        fraction = remaining / budget
        if not math.isfinite(fraction):
            return {'tier': 'unavailable'}
        tier = 'exhausted' if fraction <= 0 else 'critical' if fraction <= .10 else 'conserve' if fraction <= .25 else 'normal'
        return {'tier': tier, 'remaining_percent': round(100 * fraction, 1)}
    except Exception:
        return {'tier': 'unavailable'}


def estimate(model, incoming, outgoing):
    """Compare gateway prices conservatively; unknown prices are never zero."""
    prices = model.get('pricing') or {}
    def rate(name):
        value = prices.get(name)
        return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
    input_rate, output_rate = rate('input_per_million'), rate('output_per_million')
    if input_rate is None or output_rate is None:
        return None
    threshold = rate('long_context_threshold')
    if threshold and incoming > threshold:
        input_rate, output_rate = rate('input_long_context_per_million'), rate('output_long_context_per_million')
        if input_rate is None or output_rate is None:
            return None
    output_rate = max(output_rate, rate('reasoning_per_million') or 0, rate('thinking_per_million') or 0)
    cost = (incoming * input_rate + outgoing * output_rate) / 1000000
    return cost if math.isfinite(cost) else None



def pick(candidates, budget):
    """Only independently qualified models compete on price, never untested ones."""
    def order(row):
        return (-row['confidence'], row['cost'] if row['cost'] is not None else float('inf'), row['model'])
    baseline = min(candidates, key=order)
    price = baseline['cost']
    if budget['tier'] not in ACTIVE or price is None or price <= 0:
        return baseline, baseline, 0
    saving = .20 if budget['tier'] == 'conserve' else .05
    cheaper = [row for row in candidates if row['cost'] is not None and row['cost'] <= price * (1 - saving)]
    if not cheaper:
        return baseline, baseline, 0
    selected = min(cheaper, key=lambda row: (row['cost'], -row['confidence'], row['model']))
    return selected, baseline, round(100 * (1 - selected['cost'] / price), 1)
