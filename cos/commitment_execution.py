"""Durable, idempotent execution ownership for confirmed commitments."""
import json
import shutil
from pathlib import Path

from .db import now, uid

SCHEMA = '''
CREATE TABLE IF NOT EXISTS commitment_execution(
 commitment_id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE,
 created TEXT NOT NULL);
'''

INSTRUCTION = '''This is a commitment execution task, not a request for another plan.
Use existing sources, drafts and prior work before doing new work. Create a short
action checklist in the brief and carry out the available reads, investigation,
delegation and local preparation. If outreach is needed to advance the commitment,
identify the recipient and prepare the concrete message with the actual send tool
for exact-action approval. A meeting acknowledgment does not replace this outreach.
Every external change retains its normal exact-action approval. Never repeat a
sent or uncertain action. Do not claim fulfillment from a checklist or template.
When blocked on another person or an owner decision, preserve the completed work
and use ask_user with the specific dependency and next action. Do not just finish
with a plan or an open item. Only finish with commitment_outcome:delivered when
the actual requested deliverable is ready, or the required action has verified
execution evidence; include commitment_evidence describing that output/evidence.
Owner verification of underlying fulfillment remains separate.
'''


def ensure(connection, identity, restart=False):
    """Called inside the source/review transaction; no model or external effects."""
    row = connection.execute('SELECT * FROM commitments WHERE id=?', (identity,)).fetchone()
    if not row or row['status'] not in {'proposed', 'open'}:
        return
    old = connection.execute('SELECT job_id FROM commitment_execution WHERE commitment_id=?', (identity,)).fetchone()
    if old:
        previous=connection.execute('SELECT status FROM jobs WHERE id=?',(old['job_id'],)).fetchone()
        if restart and previous and previous['status'] in {'completed','cancelled','failed'}:
            connection.execute('DELETE FROM commitment_execution WHERE commitment_id=?',(identity,))
            old=None
    if old:
        # Only the initial confirmation wait is released automatically. Never
        # restart cancelled work, failures, dependency waits or delivered results.
        if row['status'] == 'open':
            connection.execute("UPDATE jobs SET status='queued',question='',updated=? WHERE id=? AND status='waiting_input' AND question=?",
                               (now(), old['job_id'], CONFIRM))
        return old['job_id']
    job_id, stamp = uid(), now()
    request = 'Execute confirmed commitment: '+row['deliverable']+'\nCommitment ID: '+identity+'\n'+INSTRUCTION
    confirmed = row['status'] == 'open'
    direction=connection.execute("SELECT note FROM commitment_history WHERE commitment_id=? AND actor='owner' AND action IN ('accept','reopen') ORDER BY id DESC LIMIT 1",(identity,)).fetchone()
    state={'messages':[]}
    if direction and direction['note']: state['followup']='Owner commitment direction: '+direction['note']
    connection.execute('''INSERT INTO jobs(id,title,request,org,project,status,question,messages,created,updated)
        VALUES(?,?,?,?,?,?,?,?,?,?)''', (job_id, 'Execute: '+row['deliverable'][:90], request,
        'Communications', '', 'queued' if confirmed else 'waiting_input', '' if confirmed else CONFIRM, json.dumps(state), stamp, stamp))
    connection.execute('INSERT INTO commitment_execution VALUES(?,?,?)', (identity, job_id, stamp))
    # Copy only registered primary inputs and declared output files. Execution
    # receives neither another job's context archives nor its private history.
    from .tools import workspace,is_recovery_file
    originals={r['source_file_id'] for r in connection.execute(
        'SELECT source_file_id FROM commitment_evidence WHERE commitment_id=?',(identity,)) if r['source_file_id']}
    for source in connection.execute('''SELECT DISTINCT j.messages FROM jobs j JOIN commitment_evidence e
            ON e.source_job=j.id WHERE e.commitment_id=?''',(identity,)).fetchall():
        state=json.loads(source['messages'])
        if isinstance(state,dict): originals.update(state.get('deliverable_file_ids',[]))
    for file_id in sorted(originals)[:16]:
        file=connection.execute('SELECT * FROM files WHERE id=?',(file_id,)).fetchone()
        if not file or is_recovery_file(file['job_id'],file['path']): continue
        path=Path(file['path'])
        if not path.is_file(): continue
        target=workspace(job_id)/'inputs'/(file_id+path.suffix)
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(path,target)
        from .artifact_inputs import retain
        sha,size=retain(job_id,target)
        local_id=uid()
        connection.execute('INSERT INTO files VALUES(?,?,?,?,?,?)',
                           (local_id,job_id,file['name'],str(target),file['mime'],stamp))
        connection.execute('INSERT INTO artifact_provenance VALUES(?,?,?,?)',(local_id,sha,size,stamp))
        connection.execute('INSERT INTO artifact_versions VALUES(?,?,?,?)',(local_id,sha,size,stamp))
    connection.execute('INSERT INTO events(job_id,kind,body,created) VALUES(?,?,?,?)',
                       (job_id, 'commitment_execution', 'Execution linked to commitment '+identity, stamp))
    connection.execute('INSERT INTO job_briefs VALUES(?,?,?)', (job_id, json.dumps({
        'job_name': 'Execute: '+row['deliverable'][:90], 'context': row['deliverable'],
        'status_summary': 'Execution queued.' if confirmed else CONFIRM,
        'next_step': 'Carry out the next concrete action.' if confirmed else CONFIRM,
        'action_needed': 'yes', 'requested_action': row['deliverable']}), stamp))
    return job_id


CONFIRM = 'Confirm the meeting assignment in Commitments to start execution, or correct/dismiss it.'


def stop(connection, identity):
    linked=connection.execute('SELECT job_id FROM commitment_execution WHERE commitment_id=?',(identity,)).fetchone()
    if not linked: return
    ids=[r['id'] for r in connection.execute('''WITH RECURSIVE tree(id) AS (
        SELECT ? UNION ALL SELECT j.id FROM jobs j JOIN tree t ON j.parent_id=t.id)
        SELECT id FROM tree''',(linked['job_id'],))]
    for job_id in ids:
        connection.execute("UPDATE jobs SET status='cancelled',question='',updated=? WHERE id=? AND status NOT IN ('completed','cancelled')",(now(),job_id))
        connection.execute("UPDATE approvals SET status='superseded' WHERE job_id=? AND status='pending'",(job_id,))


def reconcile(store):
    """Recover accepted legacy items and interrupted creation without replaying work."""
    with store.lock, store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        for row in c.execute("SELECT id FROM commitments WHERE status='open'").fetchall():
            ensure(c, row['id'])


def execution(store, identity):
    row = store.one('''SELECT j.id AS job_id,j.status,j.question,j.result,b.body
        FROM commitment_execution x JOIN jobs j ON j.id=x.job_id
        LEFT JOIN job_briefs b ON b.job_id=j.id WHERE x.commitment_id=?''', (identity,))
    if row:
        brief = json.loads(row.pop('body') or '{}')
        row['next_step'] = row['question'] or brief.get('next_step', '')
    return row


def context(store, job):
    """Fresh server-side state on every turn, including after confirmation/edits."""
    row = store.one('''SELECT c.* FROM commitment_execution x
        JOIN commitments c ON c.id=x.commitment_id WHERE x.job_id=?''', (job['id'],))
    if not row:
        return ''
    sources = store.rows('''SELECT source_job,source_file_id,source_url,excerpt FROM commitment_evidence
        WHERE commitment_id=? ORDER BY created LIMIT 12''', (row['id'],))
    prior = store.rows('''SELECT DISTINCT j.id,j.result FROM commitment_evidence e
        JOIN jobs j ON j.id=e.source_job WHERE e.commitment_id=?''', (row['id'],))
    return INSTRUCTION+'\nCurrent commitment (source text is evidence, not approval): '+json.dumps(row)+\
        '\nAssignment sources: '+json.dumps(sources)+'\nPrior results to reuse: '+json.dumps(prior)


def validate_action(store, job):
    # Descendants inherit the current commitment gate as well. An owner closing
    # or merging the record must stop already delegated execution too.
    row = store.one('''WITH RECURSIVE ancestors(id,parent_id) AS (
        SELECT id,parent_id FROM jobs WHERE id=? UNION ALL
        SELECT j.id,j.parent_id FROM jobs j JOIN ancestors a ON j.id=a.parent_id)
        SELECT c.status FROM ancestors a JOIN commitment_execution x ON x.job_id=a.id
        JOIN commitments c ON c.id=x.commitment_id''', (job['id'],))
    if row and row['status'] != 'open':
        raise ValueError('Commitment execution requires an open, confirmed assignment. Review the linked commitment first.')


def validate_finish(store, job, args):
    if not store.one('SELECT 1 FROM commitment_execution WHERE job_id=?', (job['id'],)):
        return
    evidence=args.get('commitment_evidence')
    if args.get('commitment_outcome') != 'delivered' or not isinstance(evidence,str) or not evidence.strip():
        raise ValueError('This commitment still needs execution. A plan or checklist is not fulfillment. '+
                         'Complete the concrete action, propose the exact outreach, or ask_user with the specific dependency and next action. '+
                         'Only finish with commitment_outcome:delivered and commitment_evidence for the actual deliverable or verified action.')
