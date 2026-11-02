"""Bounded, read-only reconciliation of uncertain actions; never replay mutations."""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

from .db import now

MAX_ATTEMPTS = 3
RECHECK_SECONDS = 30


def target(tool, args):
    """A changed message body must not bypass protection against duplicate sends."""
    if tool == 'send_email':
        normalized = {}
        for key in ('to', 'cc', 'bcc'):
            values = args.get(key) or ''
            values = values if isinstance(values, list) else re.split('[,;]', values)
            normalized[key] = sorted({value.strip().casefold() for value in values if value.strip()})
        return normalized
    if tool == 'apply_changes':
        return sorted(str(item.get('target')) for item in args.get('changes', []))
    keys = {
        'reply_email': ('id',),
        'send_teams': ('chat_id',), 'jira_write': ('operation', 'id', 'project'),
        'mcp_call': ('server', 'tool'), 'calendar_rsvp': ('id',),
        'delete_email': ('id',), 'mark_email_read': ('id',),
        'mark_teams_read': ('chat_id',), 'run_command': ('command', 'cwd'),
        'confluence_write': ('operation', 'id', 'space', 'title'),
    }.get(tool)
    return {k: args.get(k) for k in keys} if keys else args


def guard(store, job_id, tool, args):
    for row in store.rows("SELECT args FROM approvals WHERE job_id=? AND tool=? AND status IN ('uncertain','verification_inconclusive')", (job_id, tool)):
        if target(tool, json.loads(row['args'] or '{}')) == target(tool, args):
            raise ValueError('COS could not establish the previous action outcome. Do not repeat this action or ask the user to verify it. Report the evidence limitation and continue other work.')


def check(store, job, action):
    """Only authoritative positive evidence establishes an outcome. Absence is unknown."""
    from .integrations import communication
    tool = action['tool']
    args = json.loads(action['args'] or '{}')
    verification = store.one('SELECT started,evidence FROM action_verifications WHERE action_id=?', (action['id'],))
    started = verification['started'] if verification else ''
    if verification and verification['evidence']:
        saved = json.loads(verification['evidence'])
        if saved.get('outcome') == 'executed' and saved.get('evidence'):
            return saved
    if tool == 'mcp_call' and any(not args.get(k) for k in ('server', 'tool')):
        return {'outcome': 'not_executed', 'evidence': 'Required MCP connection/tool argument was missing; no remote call could start.'}
    if tool == 'apply_changes':
        from .tools import resolve_path
        changes = args.get('_review_diffs', [])
        hashes = []
        for item in changes:
            source = resolve_path(job['id'], item['source'])
            destination = resolve_path(job['id'], item['target'])
            if not source.is_file() or not destination.is_file():
                return {'outcome': 'unknown', 'evidence': 'The complete patch could not be confirmed on disk.'}
            digest = item.get('source_sha256')
            if not digest:
                return {'outcome': 'unknown', 'evidence': 'The original staged content hash was not retained.'}
            if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                return {'outcome': 'unknown', 'evidence': 'Patch targets differ; partial application remains possible.'}
            hashes.append({'target': str(destination), 'sha256': digest})
        if hashes:
            return {'outcome': 'executed', 'evidence': hashes}
    if tool == 'create_feature_branch':
        from .engineering import git, project_root, branch_name
        root = project_root(job)
        name = branch_name(job, args)
        if args.get('branch') == name and git(root, 'branch', '--show-current') == name:
            return {'outcome':'executed', 'evidence':{'branch':name, 'commit':git(root, 'rev-parse', '--verify', 'refs/heads/'+name)}}
    if tool in {'send_email', 'reply_email', 'send_teams', 'calendar_rsvp', 'delete_email', 'mark_email_read'}:
        return communication('verify_action', {'tool': tool, 'arguments': args, 'started': started})
    return {'outcome': 'unknown', 'evidence': 'This connection has no authoritative read-only outcome check for this operation.'}


def dispatch(engine):
    """Persist attempts before submitting; legacy uncertain actions are picked up too."""
    store = engine.store
    with engine.lock:
        available = 2 - len(engine.verifying)
        if available <= 0:
            return
        with store.lock, store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("INSERT OR IGNORE INTO action_verifications(action_id) SELECT a.id FROM approvals a JOIN jobs j ON j.id=a.job_id WHERE a.status='uncertain' AND j.status!='cancelled'")
            rows = c.execute("SELECT a.id FROM approvals a JOIN action_verifications v ON v.action_id=a.id JOIN jobs j ON j.id=a.job_id WHERE a.status='uncertain' AND j.status!='cancelled' AND v.next_check<=? ORDER BY v.next_check,a.created LIMIT 20", (now(),)).fetchall()
            selected = [r['id'] for r in rows if r['id'] not in engine.verifying][:available]
            next_check = (datetime.now(timezone.utc) + timedelta(seconds=RECHECK_SECONDS)).isoformat()
            for aid in selected:
                c.execute('UPDATE action_verifications SET attempts=attempts+1,next_check=? WHERE action_id=?', (next_check, aid))
        for aid in selected:
            engine.verifying.add(aid)
            try:
                engine.verification_pool.submit(run, engine, aid)
            except RuntimeError:
                engine.verifying.discard(aid)


def run(engine, aid):
    try:
        action = engine.store.one('SELECT * FROM approvals WHERE id=?', (aid,))
        job = engine.store.job(action['job_id'])
        if action['status'] != 'uncertain' or job['status'] == 'cancelled':
            return
        try:
            result = check(engine.store, job, action)
            if result.get('outcome') not in {'executed', 'not_executed', 'unknown'} or not result.get('evidence'):
                raise ValueError('Outcome checker returned no authoritative evidence.')
        except Exception as error:
            result = {'outcome': 'unknown', 'evidence': 'Automatic check could not complete: ' + str(error)[:1500]}
        complete(engine, aid, result)
    except Exception:
        # Leave the durable action uncertain; the next scheduler pass retries its check.
        import logging
        logging.getLogger(__name__).exception('Action verification failed for %s', aid)
    finally:
        with engine.lock:
            engine.verifying.discard(aid)


def complete(engine, aid, result):
    from .engine import session
    from . import progress
    store = engine.store
    with store.lock, store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        action = c.execute('SELECT * FROM approvals WHERE id=?', (aid,)).fetchone()
        verification = c.execute('SELECT * FROM action_verifications WHERE action_id=?', (aid,)).fetchone()
        if not action or action['status'] != 'uncertain' or not verification:
            return
        c.execute('UPDATE action_verifications SET evidence=?,checked=? WHERE action_id=?', (json.dumps(result), now(), aid))
        outcome = result['outcome']
        if outcome == 'unknown' and verification['attempts'] < MAX_ATTEMPTS:
            return
        status = 'verification_inconclusive' if outcome == 'unknown' else 'verified_' + outcome
        c.execute('UPDATE approvals SET status=? WHERE id=?', (status, aid))
        job = dict(c.execute('SELECT * FROM jobs WHERE id=?', (action['job_id'],)).fetchone())
        if job['status'] == 'cancelled':
            return
        state = session(job)
        receipt = {'verification': result, 'action_receipt_id': aid,
            'instruction': ('Do not repeat this action. Its outcome remains unknown after automatic checks. Report this limitation without claiming success or asking the user to verify it; continue other work.' if outcome == 'unknown' else
                            'COS verified the action outcome. Do not repeat it.' if outcome == 'executed' else
                            'COS verified that this action never executed. Correct its arguments and continue; gated actions still require fresh approval.')}
        if state.get('waiting_action') in (None, aid) and state.get('waiting_call'):
            engine.add_result(state['messages'], state.pop('waiting_call'), receipt)
            state.pop('waiting_action', None)
        else:
            state.setdefault('messages', []).append({'role': 'system', 'content': json.dumps(receipt)})
        progress.reset(state)
        # Do not overwrite a different in-flight action or resume a cancelled task.
        outstanding = c.execute("SELECT 1 FROM approvals WHERE job_id=? AND status IN ('pending','executing','uncertain')", (job['id'],)).fetchone()
        c.execute('UPDATE jobs SET messages=?,status=?,question=?,updated=? WHERE id=?',
                  (json.dumps(state), job['status'] if outstanding else 'queued', job['question'] if outstanding else '', now(), job['id']))
        c.execute('INSERT INTO events(job_id,kind,body,created) VALUES(?,?,?,?)',
                  (job['id'], 'action_verification', json.dumps(receipt), now()))
