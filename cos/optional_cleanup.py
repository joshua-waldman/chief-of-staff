"""Keep definitively rejected optional read actions out of owner attention."""
import json

from .db import now


def complete_rejected_read(connection, job, state, tool, error):
    if tool not in {'mark_email_read', 'mark_teams_read'} or job['org'] != 'Communications':
        return False
    row = connection.execute('SELECT body FROM job_briefs WHERE job_id=?', (job['id'],)).fetchone()
    brief = json.loads(row['body']) if row else {}
    from .message_triage import category
    if (brief.get('response_needed') != 'no' or brief.get('action_needed') != 'no'
            or category(brief) not in {'reading', 'ignore'}):
        return False
    if connection.execute('SELECT 1 FROM communication_inbox WHERE job_id=?', (job['id'],)).fetchone():
        return False
    if connection.execute("SELECT 1 FROM jobs WHERE parent_id=? AND status NOT IN ('completed','cancelled')", (job['id'],)).fetchone():
        return False
    if connection.execute('SELECT 1 FROM communication_sources WHERE job_id=? AND complete=0', (job['id'],)).fetchone():
        return False
    if connection.execute("SELECT 1 FROM approvals WHERE job_id=? AND status IN ('pending','executing','uncertain')", (job['id'],)).fetchone():
        return False
    state.pop('summary_read_approval', None)
    state.pop('waiting_action', None)
    state.pop('waiting_call', None)
    state['optional_read_warning'] = 'Summary complete; optional mark-as-read failed. '+str(error)[:1500]
    if not job['result']:
        state['result_kind'] = 'triage'
    stamp = now()
    connection.execute("UPDATE jobs SET messages=?,status='completed',question='',result=?,updated=? WHERE id=?",
                       (json.dumps(state), job['result'] or brief.get('email_excerpt') or 'Informational conversation; no response or work required.', stamp, job['id']))
    connection.execute('INSERT INTO events(job_id,kind,body,created) VALUES(?,?,?,?)',
                       (job['id'], 'not_executed', state['optional_read_warning'], stamp))
    return True


def reconcile(connection):
    """Repair only saved, definitive read rejections; never execute an action."""
    for job in connection.execute("SELECT * FROM jobs WHERE status='waiting_input' AND question LIKE 'Action was not executed.%'").fetchall():
        action = connection.execute('SELECT * FROM approvals WHERE job_id=? ORDER BY created DESC,rowid DESC LIMIT 1', (job['id'],)).fetchone()
        if not action or action['status'] != 'failed_not_executed' or action['tool'] not in {'mark_email_read', 'mark_teams_read'}:
            continue
        # Do not reinterpret later owner direction or a different failure.
        if not action['result'] or not job['question'].endswith(action['result'][:1500]):
            continue
        try:
            state = json.loads(job['messages'])
            if isinstance(state, list):
                state = {'messages': state}
            if isinstance(state, dict):
                complete_rejected_read(connection, job, state, action['tool'], action['result'])
        except (TypeError, ValueError):
            continue
