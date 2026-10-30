"""Separate delegated research inputs from the director's final deliverable."""
import json
from pathlib import PureWindowsPath

from .db import now


INSTRUCTION = '''Complete the original requested deliverable using returned research as inputs.
Child completion is not completion of the owner's task. Read full_result_path and
selected files when summaries omit necessary details. Reconcile contradictions and
stale access failures against later successful evidence. Do not repeat completed
actions or delegate the same research again. Close remaining source gaps directly
or through a narrow source_only reader when organization routing requires it.
For repository methodology reviews, inspect the relevant implementation and tests
at one pinned commit using repository_tree and repository_read. README and design
claims alone do not verify the implemented method. Produce one coherent report
addressing the original ask and meeting questions, with citations and explicit
limits. Select only final outputs, not transcripts, raw evidence or child notes.
Call finish with final_output:true and result_kind strategy or generated_content
after that deliverable exists. The report may be inline in summary or a selected
document. A no-reply decision does not fulfill a promised report. Do not ask the
owner to assemble research or confirm routine synthesis.'''


def validate_finish(connection, job, state, args):
    if 'final_output' in args and type(args['final_output']) is not bool:
        raise ValueError('final_output must be a boolean.')
    from .result_reviews import delegated_work
    work = list(delegated_work(connection, job)) if state.get('delivery_pending') else []
    needs_output=bool(work)
    if state.get('delivery_pending'):
        for child in connection.execute("SELECT status,messages FROM jobs WHERE parent_id=? AND status<>'cancelled'",(job['id'],)):
            child_state=json.loads(child['messages'])
            if not isinstance(child_state,dict) or (not child_state.get('source_only') and
                    (child['status']!='completed' or child_state.get('result_kind')!='action_receipt')):
                needs_output=True
    if needs_output and (args.get('final_output') is not True or args.get('result_kind') not in {'strategy', 'generated_content'}):
        raise ValueError('Delegated findings are inputs, not the requested final deliverable. '+INSTRUCTION)
    if args.get('final_output') and (args.get('result_kind') not in {'strategy', 'generated_content'} or not str(args.get('summary', '')).strip()):
        raise ValueError('A final output needs a substantive summary and result_kind strategy or generated_content.')
    if args.get('final_output'):
        for file_id in args.get('file_ids', []):
            file=connection.execute('SELECT * FROM files WHERE id=? AND job_id=?',(file_id,job['id'])).fetchone()
            if not file:
                continue  # selected_files enforces job ownership separately.
            parts=PureWindowsPath(file['path']).parts
            directory=parts[parts.index('workspace')+1] if 'workspace' in parts and len(parts)>parts.index('workspace')+1 else ''
            if directory in {'evidence','sources','inputs','downloaded','meeting-transcripts','context'}:
                raise ValueError('Select the final authored deliverable, not an input or source evidence file.')
            if directory=='delegated':
                original=connection.execute('SELECT j.messages FROM files f JOIN jobs j ON j.id=f.job_id WHERE f.id=?',(PureWindowsPath(file['path']).stem,)).fetchone()
                original_state=json.loads(original['messages']) if original else {}
                if not isinstance(original_state,dict) or original_state.get('source_only') or not original_state.get('final_output'):
                    raise ValueError('Delegated research is supporting evidence. Synthesize the requested report before selecting a final output.')


def recover(store, *, apply=True):
    """Schedule unfinished synthesis; never author the owner's report here."""
    from .result_reviews import delegated_work
    resumed = []
    with store.lock, store.connect() as c:
        c.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
        jobs = c.execute('''SELECT j.* FROM jobs j JOIN result_reviews r ON r.job_id=j.id
            WHERE j.parent_id IS NULL AND j.org='Communications'
            AND j.status IN ('queued','completed','waiting_input','blocked','failed')
            AND r.eligible=1 AND r.reviewed IS NULL''').fetchall()
        for job in jobs:
            try:
                state = json.loads(job['messages'])
            except (TypeError, ValueError):
                continue
            if not isinstance(state, dict) or state.get('final_output') or state.get('final_review') or state.get('delivery_recovery_version'):
                continue
            if state.get('result_kind') in {'strategy', 'generated_content', 'action_receipt'}:
                continue
            if c.execute("SELECT 1 FROM events WHERE job_id=? AND kind='cancelled'", (job['id'],)).fetchone():
                continue
            if c.execute("WITH RECURSIVE tree(id) AS (SELECT ? UNION ALL SELECT j.id FROM jobs j JOIN tree t ON j.parent_id=t.id) SELECT 1 FROM approvals a JOIN tree t ON t.id=a.job_id WHERE a.status IN ('pending','executing','uncertain')", (job['id'],)).fetchone():
                continue
            if not list(delegated_work(c, job)):
                continue
            if not apply:
                resumed.append(job['id'])
                continue
            state.update(delivery_pending=True, delivery_recovery_version=1)
            from .progress import reset
            reset(state)
            state.pop('historical_import', None)
            state.pop('waiting_call', None)
            state.pop('waiting_action', None)
            messages = state.setdefault('messages', [])
            if messages and messages[-1].get('tool_calls'):
                for call in messages[-1]['tool_calls']:
                    messages.append({'role':'tool', 'tool_call_id':call['id'],
                        'content':json.dumps({'completed':False, 'continuing_delivery':True})})
            messages.append({'role':'system', 'content':INSTRUCTION})
            stamp = now()
            c.execute("UPDATE jobs SET status='queued',question='',step=0,messages=?,updated=? WHERE id=?",
                (json.dumps(state), stamp, job['id']))
            c.execute('UPDATE result_reviews SET eligible=0 WHERE job_id=?', (job['id'],))
            previous=c.execute('SELECT body FROM job_briefs WHERE job_id=?',(job['id'],)).fetchone()
            brief=json.loads(previous['body']) if previous else {}
            brief.update(status_summary='COS is completing the requested final deliverable from saved research.',
                next_step='COS will return the final report for review; intermediate research remains in history.')
            c.execute('INSERT OR REPLACE INTO job_briefs VALUES(?,?,?)',(job['id'],json.dumps(brief),stamp))
            c.execute('INSERT INTO events(job_id,kind,body,created) VALUES(?,?,?,?)',
                (job['id'], 'delivery_resumed', 'Research preserved as inputs; director resumed to produce the requested final deliverable.', stamp))
            resumed.append(job['id'])
    return resumed
