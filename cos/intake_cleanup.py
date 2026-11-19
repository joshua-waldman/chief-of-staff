"""Server-owned cleanup outbox, authorized only by a committed intake ignore.

This is deliberately not a model tool or a general approval-policy exception.
Persist a claim before contacting Microsoft; unknown outcomes are never replayed.
"""
import hashlib
import json

from .db import now, uid
from .config import settings
from .intake import current_decision
from .integrations import communication, CommunicationRejected

SCHEMA = """
CREATE TABLE IF NOT EXISTS intake_cleanup(
    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL,
    source_id TEXT NOT NULL, batch TEXT NOT NULL, decision TEXT NOT NULL,
    status TEXT NOT NULL, result TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL, updated TEXT NOT NULL,
    UNIQUE(source,source_id,batch));
CREATE INDEX IF NOT EXISTS intake_cleanup_pending ON intake_cleanup(status,updated);
CREATE INDEX IF NOT EXISTS intake_cleanup_job ON intake_cleanup(job_id,created);
"""


def schedule(c, job_id, source, source_id, message_ids, decision):
    """Called in the same transaction that saves the intake closure."""
    if source not in {'email', 'teams'} or not message_ids:
        return
    ids = sorted(set(map(str, message_ids)))
    if source == 'email':
        entries = [(id, id) for id in ids]
    else:
        batch = hashlib.sha256(json.dumps(ids).encode()).hexdigest()
        entries = [(source_id, batch)]
    stamp = now()
    for destination, batch in entries:
        c.execute('''INSERT OR IGNORE INTO intake_cleanup
            (id,job_id,source,source_id,batch,decision,status,created,updated)
            VALUES(?,?,?,?,?,?,'pending',?,?)''',
            (uid(), job_id, source, destination, batch, json.dumps(decision), stamp, stamp))


def untouched(c, job_id):
    job = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    return (job and job['status'] == 'completed' and job['step'] == 0 and job['messages'] == '[]'
            and c.execute("SELECT 1 FROM events WHERE job_id=? AND kind='intake_ignored'", (job_id,)).fetchone()
            and not any(c.execute('SELECT 1 FROM '+table+' WHERE job_id=?', (job_id,)).fetchone()
                        for table in ('review_messages', 'approvals', 'communication_inbox'))
            and not c.execute('SELECT 1 FROM jobs WHERE parent_id=?', (job_id,)).fetchone())


def chat_ready(c, chat_id):
    # The transport API marks a whole chat. Every known topic must be handled,
    # and a partial/ongoing poll cannot establish that all activity was ingested.
    row = c.execute("SELECT value FROM state WHERE key='monitor_status'").fetchone()
    status = json.loads(row['value']) if row else {}
    if (status.get('sources', {}).get('teams', {}).get('state') != 'watching'
        or status.get('active_source') == 'teams' and status.get('state') == 'scanning'):
        return False
    if c.execute("SELECT 1 FROM teams_topic_migrations WHERE chat_id=? AND status<>'completed'", (chat_id,)).fetchone():
        return False
    return not c.execute('''SELECT 1 FROM teams_topics t
        JOIN communication_threads ct ON ct.source='teams' AND ct.thread_id=t.topic_id
        JOIN jobs j ON j.id=ct.job_id WHERE t.chat_id=? AND
        (j.status<>'completed' OR EXISTS(SELECT 1 FROM communication_inbox i WHERE i.job_id=j.id)
         OR EXISTS(SELECT 1 FROM approvals a WHERE a.job_id=j.id AND a.status IN ('pending','executing','uncertain','verification_inconclusive')))
        UNION ALL SELECT 1 FROM communication_threads ct JOIN jobs j ON j.id=ct.job_id
        WHERE ct.source='teams' AND ct.thread_id=? AND
        (j.status<>'completed' OR EXISTS(SELECT 1 FROM communication_inbox i WHERE i.job_id=j.id)
         OR EXISTS(SELECT 1 FROM approvals a WHERE a.job_id=j.id AND a.status IN ('pending','executing','uncertain','verification_inconclusive')))
        LIMIT 1''',
        (chat_id, chat_id)).fetchone()


def finish(c, row, status, result):
    stamp = now()
    c.execute('UPDATE intake_cleanup SET status=?,result=?,updated=? WHERE id=?',
              (status, result, stamp, row['id']))
    c.execute('INSERT INTO events(job_id,kind,body,created) VALUES(?,?,?,?)',
              (row['job_id'], 'intake_cleanup', json.dumps({'source': row['source'],
               'source_id': row['source_id'], 'status': status, 'result': result}), stamp))


def recover(store):
    """A previous process may have stopped after Microsoft accepted its write."""
    with store.lock, store.connect() as c:
        for row in c.execute("SELECT * FROM intake_cleanup WHERE status='executing'").fetchall():
            finish(c, row, 'uncertain', 'Interrupted cleanup; outcome unknown. Not repeated automatically.')


def drain(store, limit=12):
    """Run outside ingestion transactions, from the monitor's dedicated worker."""
    rows = store.rows("SELECT * FROM intake_cleanup WHERE status IN ('pending','deferred') ORDER BY updated,id LIMIT ?", (limit,))
    for row in rows:
        if settings().get('queue_paused', False):
            return
        with store.lock, store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            current = c.execute('SELECT status FROM intake_cleanup WHERE id=?', (row['id'],)).fetchone()
            if not current or current['status'] not in {'pending', 'deferred'}:
                continue
            if not current_decision(c, json.loads(row['decision'])) or not untouched(c, row['job_id']):
                finish(c, row, 'cancelled', 'Guidance or conversation changed before cleanup.')
                continue
            if row['source'] == 'teams' and not chat_ready(c, row['source_id']):
                c.execute("UPDATE intake_cleanup SET status='deferred',updated=? WHERE id=?", (now(), row['id']))
                continue
            c.execute("UPDATE intake_cleanup SET status='executing',updated=? WHERE id=?", (now(), row['id']))
        action, args = (('delete_email', {'id': row['source_id']}) if row['source'] == 'email'
                        else ('mark_teams_read', {'chat_id': row['source_id']}))
        try:
            result = communication(action, args)
            if not isinstance(result, dict) or result.get('executed') is not True:
                raise RuntimeError('Cleanup did not return an execution receipt.')
            status, detail = 'executed', json.dumps(result)
        except CommunicationRejected as error:
            status, detail = 'failed_not_executed', str(error)[-4000:]
        except Exception as error:
            status, detail = 'uncertain', str(error)[-4000:]+' Outcome unknown; not repeated automatically.'
        with store.lock, store.connect() as c:
            finish(c, row, status, detail)
