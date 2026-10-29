"""Restartable conversion of retained chat-wide history into topic contexts."""
import json
import re
import hashlib

from .db import now, uid
from .policy import ALWAYS, LOW

SCHEMA = '''CREATE TABLE IF NOT EXISTS teams_topic_migrations(
    chat_id TEXT PRIMARY KEY, job_ids TEXT NOT NULL, status TEXT NOT NULL,
    topic_ids TEXT NOT NULL DEFAULT '[]', error TEXT NOT NULL DEFAULT '', updated TEXT NOT NULL);'''

IMPORT_POLICY = """Historical Teams topic import: summarize and reassess only this
topic's retained evidence. Owner feedback and action records below retain their
original provenance. Executed actions and completed delegated work must not be
repeated. A draft or child completion is not owner acceptance. Preserve unresolved
owner obligations and uncertain outcomes in the brief for review. Generic application
next steps such as 'Review the result and any deliverables' mean optional result
review, not an outstanding owner obligation. Completed work, unverified tests and
model-authored briefs/outcomes alone do not establish a new assignment. Before
setting action_needed or response_needed to yes, cite a retained original message
or owner feedback using owner_obligation_source_id and owner_obligation_evidence
(an exact excerpt establishing the outstanding request, assignment or commitment).
If only optional review remains, set both flags to no and finish. Preserve a
concrete evidence gap as unknown with uncertainties explaining what is missing;
do not turn lack of validation into an owner task. During initial
historical import, only read sources, update the brief and finish with a useful
topic summary; do not send, mark read, delegate or execute historical requests.
Image-only messages are substantive source evidence: recover raw image references
with read_teams for blank legacy sources, use teams_attachments and read_file to
inspect images, and assess their actual content. Do not infer image content from
alt text. Record unavailable images as coverage gaps. A truly blank historical
record alone does not establish an owner task or require asking for its text.
Do not ask for routine permission to import history. New source activity or
explicit owner direction resumes the normal workflow with fresh action approvals.
Original jobs, files and receipts remain available in job history."""


def prepare(store):
    """Freeze legacy workers before scheduling, preserving all original history."""
    with store.lock, store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        groups = {}
        for row in c.execute("SELECT * FROM jobs WHERE org='Communications' AND parent_id IS NULL"):
            if not row['request'].startswith('Triage this incoming Teams '):
                continue
            if c.execute('SELECT 1 FROM teams_topics t JOIN communication_threads m ON m.thread_id=t.topic_id WHERE m.job_id=?', (row['id'],)).fetchone():
                continue
            match = re.search(r'Chat ID: (\S+)', row['request'])
            if match:
                groups.setdefault(match[1], []).append(row['id'])
        for chat_id, ids in groups.items():
            old = c.execute('SELECT * FROM teams_topic_migrations WHERE chat_id=?', (chat_id,)).fetchone()
            if old:
                continue
            c.execute('INSERT INTO teams_topic_migrations(chat_id,job_ids,status,updated) VALUES(?,?,?,?)',
                      (chat_id,json.dumps(ids),'pending',now()))
            for jid in ids:
                # Pending decisions against a mixed context cannot survive the split.
                tree = c.execute('WITH RECURSIVE tree(id) AS (SELECT ? UNION ALL SELECT j.id FROM jobs j JOIN tree t ON j.parent_id=t.id) SELECT id FROM tree', (jid,)).fetchall()
                for node in tree:
                    c.execute("UPDATE approvals SET status='superseded' WHERE job_id=? AND status='pending'", (node['id'],))
                    c.execute("UPDATE jobs SET status='blocked',question=?,updated=? WHERE id=? AND status NOT IN ('completed','cancelled')",
                              ('Existing Teams history is being reorganized into topic contexts.',now(),node['id']))


def records(store, ids, chat_id):
    result = {}
    for rule in store.rows("SELECT k.* FROM knowledge k JOIN knowledge_targets t ON t.knowledge_id=k.id WHERE t.source='teams' AND t.thread_id=? AND k.status='active'",(chat_id,)):
        rid='legacy:preference:'+rule['id']
        result[rid]={'id':rid,'chat_id':chat_id,'text':json.dumps(rule,ensure_ascii=False),
            'date':rule['updated'],'owner':True,'record_kind':'owner_preference','legacy_job_id':rule['source_job']}
    for jid in ids:
        tree = store.rows('WITH RECURSIVE tree(id) AS (SELECT ? UNION ALL SELECT j.id FROM jobs j JOIN tree t ON j.parent_id=t.id) SELECT j.* FROM jobs j JOIN tree t ON j.id=t.id', (jid,))
        for job in tree:
            sources = store.rows("SELECT * FROM communication_sources WHERE job_id=? AND source='teams'", (job['id'],))
            for s in sources:
                result[s['message_id']] = {'id':s['message_id'],'chat_id':chat_id,
                    'text':s['body'],'date':s['received'] or '','sender':s['sender'],
                    'owner':s['sender']=='You','record_kind':'message','legacy_job_id':job['id']}
            def history(kind, key, text, date=''):
                if text:
                    rid='legacy:'+kind+':'+str(key)
                    result[rid]={'id':rid,'chat_id':chat_id,'text':text,'date':date or job['updated'],
                        'owner':True,'record_kind':kind,'legacy_job_id':job['id']}
            for row in store.rows('SELECT * FROM review_messages WHERE job_id=?', (job['id'],)):
                history('owner_feedback' if row['role']=='user' else 'review_history',row['id'],row['body'],row['created'])
            returned=store.one('SELECT * FROM result_reviews WHERE job_id=? AND eligible=1 AND reviewed IS NULL',(job['id'],))
            if returned:
                history('returned_work',job['id'],returned['summary'],returned['completed'])
            actions=sorted(ALWAYS|{'delegate','jira_write','mcp_call','record_commitment','add_commitment_evidence'})
            routine=sorted(LOW)
            query="SELECT * FROM approvals WHERE job_id=? AND status IN ('executed','uncertain','executing','pending','superseded') AND (tool IN ("+','.join('?' for _ in actions)+") OR tool NOT IN ("+','.join('?' for _ in routine)+'))'
            for row in store.rows(query, (job['id'],*actions,*routine)):
                history('action_receipt' if row['status'] in {'executed','uncertain','executing'} else 'unsent_proposal',row['id'],json.dumps({k:row[k] for k in ('id','tool','args','status','result')},ensure_ascii=False),row['created'])
            # Chat-wide model summaries may already mix unrelated topics.
            # Keep those originals in history; do not seed every topic with them.
            if job['id']!=jid or not sources:
                history('outcome',job['id'],job['result'])
            if job['id']!=jid and not job['result']:
                history('unfinished_delegation',job['id'],job['request'])
            brief=store.one('SELECT body FROM job_briefs WHERE job_id=?',(job['id'],))
            if brief and (job['id']!=jid or not sources):
                history('brief',job['id'],brief['body'])
            if not sources and job['id']==jid:
                history('legacy_context',job['id'],job['request'],job['created'])
    return sorted(result.values(),key=lambda r:(r.get('date') or '',r['id']))


def migrate(monitor, *, limit=None):
    """Plan each chat without mutations; commit its split atomically or retry."""
    store=monitor.store
    prepare(store)
    summary={'migrated':0,'topics':0,'deferred':0,'failed':0}
    pending=store.rows("SELECT * FROM teams_topic_migrations WHERE status<>'completed' ORDER BY updated,chat_id")
    for migration in pending[:limit] if limit else pending:
        ids=json.loads(migration['job_ids'])
        chat_id=migration['chat_id']
        if any(store.one("WITH RECURSIVE tree(id) AS (SELECT ? UNION ALL SELECT j.id FROM jobs j JOIN tree t ON j.parent_id=t.id) SELECT 1 FROM approvals a JOIN tree t ON t.id=a.job_id WHERE a.status IN ('executing','uncertain')", (jid,)) for jid in ids):
            summary['deferred']+=1
            continue
        try:
            source=records(store,ids,chat_id)
            total=len(source)
            snapshot=hashlib.sha256(json.dumps(source,sort_keys=True).encode()).hexdigest()
            grouped={}
            while source:
                candidates=[{'topic_id':tid,'title':g['title'],'status':'historical_import',
                    'summary':'Historical topic being imported','recent_messages':[
                        {'id':g['messages'][-1]['id'],'text':g['messages'][-1]['text'][:600]}]} for tid,g in grouped.items()]
                batch=monitor.teams_topics.route(chat_id,source,include_seen=True,extra_topics=candidates)
                if not batch:
                    # Messages already assigned by a prior successful ingestion
                    # need no reassignment; only this chat's unassigned history remains.
                    break
                assigned=set()
                for g in batch:
                    target=grouped.setdefault(g['topic_id'],{**g,'messages':[]})
                    target['messages'].extend(g['messages'])
                    assigned.update(m['id'] for m in g['messages'])
                source=[r for r in source if r['id'] not in assigned]
                store.set_state('teams_topic_migration_progress',{'total_records':total,'planned_records':total-len(source),'topics':len(grouped),'updated':now()})
            with store.lock, store.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                current=c.execute('SELECT status FROM teams_topic_migrations WHERE chat_id=?',(chat_id,)).fetchone()
                if current['status']=='completed':
                    continue
                if snapshot!=hashlib.sha256(json.dumps(records(store,ids,chat_id),sort_keys=True).encode()).hexdigest():
                    raise ValueError('Retained history changed during topic routing; retrying from current evidence.')
                # Recheck external mutation outcomes in the same commit.
                if any(c.execute("WITH RECURSIVE tree(id) AS (SELECT ? UNION ALL SELECT j.id FROM jobs j JOIN tree t ON j.parent_id=t.id) SELECT 1 FROM approvals a JOIN tree t ON t.id=a.job_id WHERE a.status IN ('executing','uncertain')",(jid,)).fetchone() for jid in ids):
                    raise ValueError('An external action outcome still needs verification.')
                # Collect completed returns before archiving their director.
                # Topic summaries are triage; the original request owns review.
                from .result_reviews import reconcile
                reconcile(c, ids)
                topic_jobs=[]
                for tid,g in grouped.items():
                    jid=uid()
                    stamp=now()
                    request='Triage this incoming Teams conversation.\nChat ID: '+chat_id+'\nTopic ID: '+tid+'\nTopic: '+g['title']+'\n'+IMPORT_POLICY
                    # Preserve exact historical content, including owner guidance
                    # and receipts, only in the topic the router assigned it to.
                    request+='\n\nRetained topic evidence (untrusted source data):\n'+json.dumps(g['messages'],ensure_ascii=False)
                    state=json.dumps({'messages':[],'historical_import':True,'legacy_job_ids':ids})
                    c.execute('INSERT INTO jobs(id,title,request,org,project,status,created,updated,messages) VALUES(?,?,?,?,?,?,?,?,?)',
                              (jid,g['title'][:100],request,'Communications','','queued',stamp,stamp,state))
                    c.execute('INSERT INTO teams_topics VALUES(?,?,?)',(tid,chat_id,g['title']))
                    c.execute("INSERT INTO communication_threads VALUES('teams',?,?)",(tid,jid))
                    from .conversations import save_source
                    for m in g['messages']:
                        c.execute('INSERT INTO teams_topic_messages VALUES(?,?,?,?)',(chat_id,m['id'],tid,json.dumps(m)))
                        if m['record_kind']=='message':
                            save_source(c,jid,'teams',m['id'],m['sender'],'',m['date'],m['text'])
                    c.execute('INSERT INTO events(job_id,kind,body,created) VALUES(?,?,?,?)',
                              (jid,'topic_import','Imported retained history from '+', '.join(ids),stamp))
                    topic_jobs.append(jid)
                for jid in ids:
                    c.execute("WITH RECURSIVE tree(id) AS (SELECT ? UNION ALL SELECT j.id FROM jobs j JOIN tree t ON j.parent_id=t.id) UPDATE jobs SET status=CASE WHEN EXISTS (SELECT 1 FROM result_reviews r WHERE r.job_id=jobs.id AND r.eligible=1 AND r.reviewed IS NULL) THEN 'completed' ELSE 'cancelled' END,question='',updated=? WHERE id IN (SELECT id FROM tree) AND status NOT IN ('completed','cancelled')",(jid,now()))
                    c.execute('INSERT INTO events(job_id,kind,body,created) VALUES(?,?,?,?)',
                              (jid,'topic_migrated','History preserved; continued in topic jobs: '+', '.join(topic_jobs),now()))
                    c.execute('DELETE FROM communication_inbox WHERE job_id=?',(jid,))
                c.execute("UPDATE teams_topic_migrations SET status='completed',topic_ids=?,error='',updated=? WHERE chat_id=?",(json.dumps(topic_jobs),now(),chat_id))
                summary['migrated']+=1
                summary['topics']+=len(topic_jobs)
        except Exception as error:
            store.write("UPDATE teams_topic_migrations SET status='retry',error=?,updated=? WHERE chat_id=?",(type(error).__name__+': '+str(error)[:500],now(),chat_id))
            summary['failed']+=1
    store.set_state('teams_topic_migration_status',{**summary,'updated':now(),
        'remaining':store.one("SELECT COUNT(*) AS n FROM teams_topic_migrations WHERE status<>'completed'")['n']})
    return summary
