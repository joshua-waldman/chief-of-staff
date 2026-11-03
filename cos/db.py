import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from .config import DATA

def now():
    return datetime.now(timezone.utc).isoformat()

def uid():
    return uuid.uuid4().hex

class Store:
    def __init__(self, path=None):
        self.path = str(path or DATA / 'chief.sqlite3')
        self.lock = threading.RLock()
        with self.connect() as c:
            c.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS trace_events(id TEXT PRIMARY KEY,job_id TEXT,created TEXT,phase TEXT,payload TEXT);
            CREATE INDEX IF NOT EXISTS trace_job ON trace_events(job_id,created);
            CREATE TABLE IF NOT EXISTS artifact_provenance(file_id TEXT PRIMARY KEY,sha256 TEXT,size INTEGER,created TEXT);
            CREATE TABLE IF NOT EXISTS knowledge(id TEXT PRIMARY KEY,kind TEXT,title TEXT,content TEXT,scope TEXT,status TEXT,source TEXT,source_job TEXT,created TEXT,updated TEXT,uses INTEGER,helpful INTEGER,fingerprint TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS knowledge_fingerprint ON knowledge(fingerprint);
            CREATE TABLE IF NOT EXISTS knowledge_targets(knowledge_id TEXT PRIMARY KEY,source TEXT NOT NULL,thread_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS review_messages(id INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT,role TEXT,body TEXT,created TEXT);
            CREATE INDEX IF NOT EXISTS review_job ON review_messages(job_id,id);
            CREATE TABLE IF NOT EXISTS job_briefs(job_id TEXT PRIMARY KEY,body TEXT,updated TEXT);
            CREATE TABLE IF NOT EXISTS job_names(job_id TEXT PRIMARY KEY,name TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS result_reviews(job_id TEXT PRIMARY KEY,revision TEXT NOT NULL,summary TEXT NOT NULL,file_ids TEXT NOT NULL,completed TEXT NOT NULL,reviewed TEXT);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,parent_id TEXT,title TEXT,request TEXT,org TEXT,project TEXT,status TEXT,created TEXT,updated TEXT,result TEXT DEFAULT '',question TEXT DEFAULT '',messages TEXT DEFAULT '[]',step INTEGER DEFAULT 0);
            CREATE INDEX IF NOT EXISTS jobs_parent_status ON jobs(parent_id,status,created,id);
            CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created DESC);
            CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT,kind TEXT,body TEXT,created TEXT);
            CREATE INDEX IF NOT EXISTS events_job_kind ON events(job_id,kind,created);
            CREATE INDEX IF NOT EXISTS events_job_id ON events(job_id,id);
            CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY,job_id TEXT,tool TEXT,args TEXT,risk TEXT,reason TEXT,status TEXT,created TEXT,result TEXT DEFAULT '');
            CREATE INDEX IF NOT EXISTS approvals_job_status ON approvals(job_id,status,created);
            CREATE INDEX IF NOT EXISTS approvals_status ON approvals(status,created);
            CREATE TABLE IF NOT EXISTS action_verifications(action_id TEXT PRIMARY KEY,attempts INTEGER NOT NULL DEFAULT 0,next_check TEXT NOT NULL DEFAULT '',started TEXT NOT NULL DEFAULT '',evidence TEXT NOT NULL DEFAULT '',checked TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY,job_id TEXT,name TEXT,path TEXT,mime TEXT,created TEXT);
            CREATE INDEX IF NOT EXISTS files_job ON files(job_id);
            CREATE TABLE IF NOT EXISTS seen(source TEXT,item_id TEXT,created TEXT,PRIMARY KEY(source,item_id));
            CREATE TABLE IF NOT EXISTS communication_threads(source TEXT,thread_id TEXT,job_id TEXT,PRIMARY KEY(source,thread_id));
            CREATE TABLE IF NOT EXISTS teams_topics(topic_id TEXT PRIMARY KEY,chat_id TEXT NOT NULL,title TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS teams_topics_chat ON teams_topics(chat_id);
            CREATE TABLE IF NOT EXISTS teams_topic_messages(chat_id TEXT,message_id TEXT,topic_id TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(chat_id,message_id));
            CREATE INDEX IF NOT EXISTS teams_topic_members ON teams_topic_messages(topic_id);
            CREATE TABLE IF NOT EXISTS communication_inbox(id INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT,body TEXT,created TEXT);
            CREATE TABLE IF NOT EXISTS communication_sources(job_id TEXT,source TEXT,message_id TEXT,sender TEXT,subject TEXT,received TEXT,body TEXT,complete INTEGER,PRIMARY KEY(job_id,source,message_id));
            CREATE INDEX IF NOT EXISTS communication_pending ON communication_inbox(job_id,id);
            CREATE TABLE IF NOT EXISTS conversation_activity(job_id TEXT PRIMARY KEY,first_seen TEXT,last_seen TEXT);
            CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS organizations(name TEXT PRIMARY KEY,leader TEXT,purpose TEXT,created TEXT);
            ''')
            from .commitments import SCHEMA as commitment_schema
            c.executescript(commitment_schema)
            from .schedules import SCHEMA as schedule_schema
            c.executescript(schedule_schema)
            from .teams_migration import SCHEMA as teams_migration_schema
            c.executescript(teams_migration_schema)
            if 'payload' not in {r['name'] for r in c.execute('PRAGMA table_info(teams_topic_messages)')}:
                c.execute('BEGIN IMMEDIATE')
                c.execute("ALTER TABLE teams_topic_messages ADD COLUMN payload TEXT NOT NULL DEFAULT '{}'")
                for row in c.execute('''SELECT m.chat_id,m.message_id,s.sender,s.received,s.body
                        FROM teams_topic_messages m
                        LEFT JOIN communication_threads t ON t.source='teams' AND t.thread_id=m.topic_id
                        LEFT JOIN communication_sources s ON s.job_id=t.job_id AND s.source='teams' AND s.message_id=m.message_id''').fetchall():
                    payload={'id':row['message_id'],'chat_id':row['chat_id'],
                        'text':row['body'] or '', 'date':row['received'] or '',
                        'sender':row['sender'] or '', 'owner':row['sender']=='You', 'record_kind':'message'}
                    c.execute('UPDATE teams_topic_messages SET payload=? WHERE chat_id=? AND message_id=?',
                              (json.dumps(payload),row['chat_id'],row['message_id']))
            orgs = [('Communications','Chief Communications Officer','Monitor and triage communications, propose action delegation for approval, and draft grounded responses.'),('Technology','Chief Technology Officer','Research with traceable evidence.'),('Self-Improvement','Director of Self-Improvement','Improve this application and create skills and organizations.'),('Engineering','Director of Engineering','Implement and validate project changes.'),('Strategy','Chief Strategy Officer','Develop scope, plans, priorities, and acceptance criteria.')]
            c.executemany('INSERT OR IGNORE INTO organizations VALUES(?,?,?,?)', [(*o, now()) for o in orgs])
            c.execute("UPDATE organizations SET purpose=? WHERE name='Communications' AND purpose=?",(orgs[0][2],'Monitor, triage, and draft grounded responses.'))

            if 'eligible' not in {row['name'] for row in c.execute('PRAGMA table_info(result_reviews)')}:
                c.execute('ALTER TABLE result_reviews ADD COLUMN eligible INTEGER NOT NULL DEFAULT 1')
            from .result_reviews import eligible, reconcile
            # Reconcile the old broad inbox without marking anything owner-reviewed.
            old_results=c.execute('''SELECT j.*,r.job_id AS review_job_id FROM jobs j LEFT JOIN result_reviews r ON r.job_id=j.id
                WHERE j.status='completed' AND j.parent_id IS NULL AND r.reviewed IS NULL''').fetchall()
            for job in old_results:
                try:
                    state=json.loads(job['messages'])
                except (ValueError,TypeError):
                    continue
                if isinstance(state,dict) and 'deliverable_file_ids' in state:
                    previous=c.execute('SELECT * FROM result_reviews WHERE job_id=?',(job['id'],)).fetchone()
                    # A newer conversation pass does not invalidate an older saved review.
                    if previous and previous['eligible'] and previous['completed'] < job['updated']:
                        continue
                    reviewable=eligible(c,job,state)
                    if job['review_job_id']:
                        if not reviewable:
                            c.execute('UPDATE result_reviews SET eligible=0 WHERE job_id=?',(job['id'],))
            reconcile(c)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def rows(self, sql, args=()):
        with self.connect() as c:
            return [dict(r) for r in c.execute(sql,args).fetchall()]

    def one(self, sql, args=()):
        rows = self.rows(sql,args)
        return rows[0] if rows else None

    def write(self, sql, args=()):
        with self.lock, self.connect() as c:
            return c.execute(sql,args).rowcount

    def create_job(self, request, org='Chief of Staff', project='', parent_id=None):
        id = uid()
        self.write('INSERT INTO jobs(id,parent_id,title,request,org,project,status,created,updated) VALUES(?,?,?,?,?,?,?,?,?)', (id,parent_id,request[:100],request,org,project,'queued',now(),now()))
        self.event(id,'created','Request received by the Chief of Staff.')
        from .trace import emit,fingerprint
        emit('request.created',{'parent_job_id':parent_id,'organization':org,'request':fingerprint(request)},store=self,job_id=id)
        return self.job(id)

    def job(self,id):
        return self.one('SELECT * FROM jobs WHERE id=?',(id,))

    def update(self,id,**fields):
        allowed = {'org','project','status','result','question','messages','step','title'}
        if set(fields)-allowed:
            raise ValueError('Invalid job fields')
        fields['updated'] = now()
        self.write('UPDATE jobs SET '+','.join(f'{k}=?' for k in fields)+' WHERE id=?',(*fields.values(),id))

    def event(self,id,kind,body):
        self.write('INSERT INTO events(job_id,kind,body,created) VALUES(?,?,?,?)',(id,kind,body,now()))

    def detail(self,id,compact=False):
        columns='id,parent_id,title,request,org,project,status,created,updated,result,question,step' if compact else '*'
        j = self.one('SELECT '+columns+' FROM jobs WHERE id=?',(id,))
        if not j:
            return None
        if compact:
            j['event_count']=self.one("SELECT COUNT(*) n FROM events WHERE job_id=? AND kind<>'model'",(id,))['n']
            j['events']=list(reversed(self.rows("SELECT * FROM events WHERE job_id=? AND kind<>'model' ORDER BY id DESC LIMIT 30",(id,))))
        else:
            j['events'] = self.rows('SELECT * FROM events WHERE job_id=? ORDER BY id',(id,))
        approval_filter=" AND status IN ('pending','executing','uncertain','verification_inconclusive')" if compact else ''
        j['approvals'] = self.rows('SELECT a.*,v.evidence AS verification_evidence FROM approvals a LEFT JOIN action_verifications v ON v.action_id=a.id WHERE job_id=?'+approval_filter+' ORDER BY created',(id,))
        j['files'] = self.rows('SELECT * FROM files WHERE job_id=?',(id,))
        from .tools import is_recovery_file
        archived=self.state('recovery_archive:'+id,{}).get('members',{})
        archived_ids={meta['file_id'] for meta in archived.values()}
        j['files']=[file for file in j['files'] if file['id'] not in archived_ids]
        j['archived_recovery_count']=len(archived)
        for file in j['files']:
            file['purpose']='recovery' if is_recovery_file(id,file['path']) else 'deliverable'
        columns='id,parent_id,title,org,project,status,created,updated' if compact else '*'
        j['children'] = self.rows('SELECT '+columns+' FROM jobs WHERE parent_id=? ORDER BY created',(id,))
        from .briefs import detail_brief
        j['brief']=detail_brief(self,j)
        j['review_messages']=self.rows('SELECT * FROM review_messages WHERE job_id=? ORDER BY id',(id,))
        j['result_review']=self.result_review(id) if j['status']!='cancelled' else None
        if j['result_review']:
            review=j['result_review']
            ids=set(review['file_ids'])
            if not review.get('final_output'):
                ids |= set(re.findall(r'/api/files/([A-Za-z0-9_-]+)',review['summary']))
            # Summaries may retain a child's original URL after a parent handoff.
            review['files']=[]
            for file_id in sorted(ids):
                file=self.one('SELECT * FROM files WHERE id=?',(file_id,))
                if file and not is_recovery_file(file['job_id'],file['path']):
                    review['files'].append({**file,'purpose':'deliverable'})
        j['source_messages']=self.rows('SELECT * FROM communication_sources WHERE job_id=? ORDER BY received,message_id',(id,))
        from .capacity import Capacity
        raw={}
        if not compact or j['status'] in {'waiting_capacity','capacity_paused'}:
            messages=j.get('messages') if not compact else self.one('SELECT messages FROM jobs WHERE id=?',(id,))['messages']
            raw=json.loads(messages)
        saved=raw.get('capacity',{}) if isinstance(raw,dict) else {}
        if saved and j['status'] in {'waiting_capacity','capacity_paused'}:
            j['capacity']={**saved,**Capacity(self).get(saved['model'])}
        j.pop('messages',None)
        return j

    def complete_result(self,id,state,summary,step):
        """Commit the returned work and its owner review notification together."""
        stamp=now()
        with self.lock,self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            job=c.execute('SELECT * FROM jobs WHERE id=?',(id,)).fetchone()
            if not job or job['status']=='cancelled':
                return
            c.execute("UPDATE jobs SET status='completed',result=?,question='',messages=?,step=?,updated=? WHERE id=?",
                (summary,json.dumps({**state,'messages':[m for m in state.get('messages',[]) if not m.get('_cos_memory')]}),step,stamp,id))
            if not job['parent_id']:
                from .result_reviews import eligible, review_payload
                review_summary,file_ids=review_payload(c,job,state,summary)
                reviewable=eligible(c,job,state)
                previous=c.execute('SELECT eligible,reviewed FROM result_reviews WHERE job_id=?',(id,)).fetchone()
                # Unrelated triage must not replace returned work still awaiting review.
                if reviewable or state.get('result_kind')=='action_receipt' or not previous or not previous['eligible'] or previous['reviewed']:
                    c.execute('INSERT OR REPLACE INTO result_reviews(job_id,revision,summary,file_ids,completed,reviewed,eligible) VALUES(?,?,?,?,?,NULL,?)',
                        (id,uid(),review_summary,json.dumps(file_ids),stamp,int(reviewable)))
            else:
                # Publish returned work even if the director is waiting for
                # another child, blocked, or processing newer source activity.
                from .result_reviews import reconcile
                root = job
                seen = {id}
                while root['parent_id'] and root['parent_id'] not in seen:
                    seen.add(root['parent_id'])
                    parent = c.execute('SELECT * FROM jobs WHERE id=?', (root['parent_id'],)).fetchone()
                    if not parent:
                        break
                    root = parent
                if not root['parent_id']:
                    reconcile(c, [root['id']], event_kind='result_ready')

    def result_review(self,id):
        row=self.one('SELECT * FROM result_reviews WHERE job_id=? AND eligible=1',(id,))
        if row:
            row['file_ids']=json.loads(row['file_ids'])
            from .tools import is_recovery_file
            from .result_reviews import review_content
            # Fetch only the flag needed here, rather than transferring and
            # parsing the entire saved model history for every review-list poll.
            job=self.one("SELECT CASE WHEN json_valid(messages) THEN json_extract(messages,'$.final_review') ELSE 0 END AS final_review FROM jobs WHERE id=?",(id,))
            final=bool(job and job['final_review'])
            ids=set(row['file_ids'])
            if not final:
                ids |= set(re.findall(r'/api/files/([A-Za-z0-9_-]+)',row['summary']))
            else:
                row['final_output']=True
            files=[]
            for file_id in sorted(ids):
                file=self.one('SELECT * FROM files WHERE id=?',(file_id,))
                if file and not is_recovery_file(file['job_id'],file['path']):
                    files.append(file)
            row.update(review_content(row['summary'],files))
        return row

    def pending_results(self):
        rows=self.rows('''SELECT j.id,j.title,j.org,j.project,r.revision,r.completed,
            substr(r.summary,1,600) AS summary FROM result_reviews r JOIN jobs j ON j.id=r.job_id
            WHERE r.eligible=1 AND r.reviewed IS NULL AND j.status<>'cancelled' AND j.parent_id IS NULL
            ORDER BY r.completed DESC,j.id''')
        for row in rows:
            review=self.result_review(row['id'])
            if review:
                row['review_title']=review['review_title']
                row['items']=[{key:item[key] for key in ('title','kind','file_ids')} for item in review['items']]
        return rows

    def review_result(self,id,revision):
        with self.lock,self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('''SELECT r.*,j.status FROM result_reviews r JOIN jobs j ON j.id=r.job_id
                WHERE r.job_id=?''',(id,)).fetchone()
            if not row or not row['eligible'] or row['status']=='cancelled' or row['revision']!=revision:
                raise ValueError('This result has changed. Open the latest completed result before marking it reviewed.')
            if not row['reviewed']:
                stamp=now()
                c.execute('UPDATE result_reviews SET reviewed=? WHERE job_id=?',(stamp,id))
                c.execute('INSERT INTO events(job_id,kind,body,created) VALUES(?,?,?,?)',
                    (id,'result_reviewed','Owner marked the returned result reviewed.',stamp))
        return {'ok':True}

    def state(self,key,default=None):
        row = self.one('SELECT value FROM state WHERE key=?',(key,))
        return json.loads(row['value']) if row else default

    def set_state(self,key,value):
        self.write('INSERT OR REPLACE INTO state VALUES(?,?)',(key,json.dumps(value)))

    def mark_seen(self,source,id):
        return self.write('INSERT OR IGNORE INTO seen VALUES(?,?,?)',(source,id,now())) == 1
