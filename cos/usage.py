"""Durable accounting of physical gateway attempts, without prompts or credentials."""
import hashlib
import logging
from datetime import datetime, timezone
from .db import now, uid
from .trace import CURRENT

SCHEMA = '''
CREATE TABLE IF NOT EXISTS model_usage(
 id TEXT PRIMARY KEY, created TEXT NOT NULL, completed TEXT,
 job_id TEXT, purpose TEXT NOT NULL, role TEXT NOT NULL, model TEXT NOT NULL,
 endpoint TEXT NOT NULL, key_scope TEXT NOT NULL, status TEXT NOT NULL,
 input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER,
 cached_tokens INTEGER, reasoning_tokens INTEGER
);
CREATE INDEX IF NOT EXISTS model_usage_created ON model_usage(created);
CREATE INDEX IF NOT EXISTS model_usage_job ON model_usage(job_id,created);
'''


def count(value):
    return value if type(value) is int and 0 <= value <= 2**53 - 1 else None


def normalize(value):
    value = value if isinstance(value, dict) else {}
    incoming = count(value.get('input_tokens', value.get('prompt_tokens')))
    outgoing = count(value.get('output_tokens', value.get('completion_tokens')))
    total = count(value.get('total_tokens'))
    if total is None and incoming is not None and outgoing is not None:
        total = count(incoming + outgoing)
    details_in = value.get('input_tokens_details', value.get('prompt_tokens_details'))
    details_out = value.get('output_tokens_details', value.get('completion_tokens_details'))
    return (incoming, outgoing, total,
            count(details_in.get('cached_tokens')) if isinstance(details_in, dict) else None,
            count(details_out.get('reasoning_tokens')) if isinstance(details_out, dict) else None)


def record(store, identity, *, model='', endpoint='', key='', purpose='agent', role='', status='pending', data=None):
    """Accounting cannot change an inference outcome or cause a network retry."""
    try:
        if status == 'pending':
            store.write('INSERT INTO model_usage(id,created,job_id,purpose,role,model,endpoint,key_scope,status) VALUES(?,?,?,?,?,?,?,?,?)',
                        (identity, now(), CURRENT.get().get('job_id'), purpose, role, model,
                         endpoint, hashlib.sha256(key.encode()).hexdigest(), status))
        else:
            tokens = normalize(data.get('usage')) if isinstance(data, dict) else (None,) * 5
            if status == 'reported' and any(v is None for v in tokens[:3]):
                status = 'missing_usage'
            store.write('UPDATE model_usage SET completed=?,status=?,input_tokens=?,output_tokens=?,total_tokens=?,cached_tokens=?,reasoning_tokens=? WHERE id=?',
                        (now(), status, *tokens, identity))
    except Exception:
        logging.getLogger(__name__).warning('Usage accounting unavailable; model outcome unchanged.')


TOTALS = '''COUNT(*) AS requests,
 COALESCE(SUM(input_tokens),0) AS input_tokens,
 COALESCE(SUM(output_tokens),0) AS output_tokens,
 COALESCE(SUM(total_tokens),0) AS total_tokens,
 COALESCE(SUM(cached_tokens),0) AS cached_tokens,
 COALESCE(SUM(reasoning_tokens),0) AS reasoning_tokens,
 COALESCE(SUM(status='reported'),0) AS reported_requests,
 COALESCE(SUM(status IN ('unknown','pending','missing_usage')),0) AS incomplete_requests,
 COALESCE(SUM(status='rejected'),0) AS rejected_requests'''


def summary(store, since=None, job_id=None):
    if since:
        try:
            parsed = datetime.fromisoformat(since.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                raise ValueError()
            since = parsed.astimezone(timezone.utc).isoformat()
        except (ValueError, TypeError, AttributeError):
            raise ValueError('Usage start must be an ISO date and time with a timezone.') from None
    else:
        since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    args = [since]
    scope = ''
    if job_id:
        scope = ' AND job_id IN (WITH RECURSIVE tree(id) AS (SELECT id FROM jobs WHERE id=? UNION SELECT j.id FROM jobs j JOIN tree t ON j.parent_id=t.id) SELECT id FROM tree)'
        args.append(job_id)
    where = ' WHERE created>=?' + scope
    with store.connect() as c:
        # A consistent read snapshot keeps model subtotals aligned while agents write.
        c.execute('BEGIN')
        def rows(sql, values=()):
            return [dict(row) for row in c.execute(sql, values)]
        return {'since': since,
                'started_at': rows('SELECT MIN(created) AS stamp FROM model_usage')[0]['stamp'],
                'period': rows('SELECT ' + TOTALS + ' FROM model_usage' + where, args)[0],
                'models': rows('SELECT model,' + TOTALS + ' FROM model_usage' + where + ' GROUP BY model ORDER BY total_tokens DESC,model', args),
                'purposes': rows('SELECT purpose,' + TOTALS + ' FROM model_usage' + where + ' GROUP BY purpose ORDER BY total_tokens DESC,purpose', args)}
