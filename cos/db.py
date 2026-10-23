import json
import sqlite3
import threading
import uuid
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
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,parent_id TEXT,title TEXT,request TEXT,org TEXT,project TEXT,status TEXT,created TEXT,updated TEXT,result TEXT DEFAULT '',question TEXT DEFAULT '',messages TEXT DEFAULT '[]',step INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT,kind TEXT,body TEXT,created TEXT);
            CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY,job_id TEXT,tool TEXT,args TEXT,risk TEXT,reason TEXT,status TEXT,created TEXT,result TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY,job_id TEXT,name TEXT,path TEXT,mime TEXT,created TEXT);
            CREATE TABLE IF NOT EXISTS seen(source TEXT,item_id TEXT,created TEXT,PRIMARY KEY(source,item_id));
            CREATE TABLE IF NOT EXISTS communication_threads(source TEXT,thread_id TEXT,job_id TEXT,PRIMARY KEY(source,thread_id));
            CREATE TABLE IF NOT EXISTS communication_inbox(id INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT,body TEXT,created TEXT);
            CREATE TABLE IF NOT EXISTS communication_sources(job_id TEXT,source TEXT,message_id TEXT,sender TEXT,subject TEXT,received TEXT,body TEXT,complete INTEGER,PRIMARY KEY(job_id,source,message_id));
            CREATE INDEX IF NOT EXISTS communication_pending ON communication_inbox(job_id,id);
            CREATE TABLE IF NOT EXISTS conversation_activity(job_id TEXT PRIMARY KEY,first_seen TEXT,last_seen TEXT);
            CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS organizations(name TEXT PRIMARY KEY,leader TEXT,purpose TEXT,created TEXT);
            ''')
            orgs = [('Communications','Chief Communications Officer','Monitor and triage communications, propose action delegation for approval, and draft grounded responses.'),('Technology','Chief Technology Officer','Research with traceable evidence.'),('Self-Improvement','Director of Self-Improvement','Improve this application and create skills and organizations.'),('Engineering','Director of Engineering','Implement and validate project changes.'),('Strategy','Chief Strategy Officer','Develop scope, plans, priorities, and acceptance criteria.')]
            c.executemany('INSERT OR IGNORE INTO organizations VALUES(?,?,?,?)', [(*o, now()) for o in orgs])
            c.execute("UPDATE organizations SET purpose=? WHERE name='Communications' AND purpose=?",(orgs[0][2],'Monitor, triage, and draft grounded responses.'))

    def connect(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

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

    def detail(self,id):
        j = self.job(id)
        if not j:
            return None
        j['events'] = self.rows('SELECT * FROM events WHERE job_id=? ORDER BY id',(id,))
        j['approvals'] = self.rows('SELECT * FROM approvals WHERE job_id=? ORDER BY created',(id,))
        j['files'] = self.rows('SELECT * FROM files WHERE job_id=?',(id,))
        from .tools import is_recovery_file
        archived=self.state('recovery_archive:'+id,{}).get('members',{})
        archived_ids={meta['file_id'] for meta in archived.values()}
        j['files']=[file for file in j['files'] if file['id'] not in archived_ids]
        j['archived_recovery_count']=len(archived)
        for file in j['files']:
            file['purpose']='recovery' if is_recovery_file(id,file['path']) else 'deliverable'
        j['children'] = self.rows('SELECT * FROM jobs WHERE parent_id=? ORDER BY created',(id,))
        from .briefs import detail_brief
        j['brief']=detail_brief(self,j)
        j['review_messages']=self.rows('SELECT * FROM review_messages WHERE job_id=? ORDER BY id',(id,))
        j['source_messages']=self.rows('SELECT * FROM communication_sources WHERE job_id=? ORDER BY received,message_id',(id,))
        from .capacity import Capacity
        raw=json.loads(j['messages'])
        saved=raw.get('capacity',{}) if isinstance(raw,dict) else {}
        if saved and j['status'] in {'waiting_capacity','capacity_paused'}:
            j['capacity']={**saved,**Capacity(self).get(saved['model'])}
        j.pop('messages',None)
        return j

    def state(self,key,default=None):
        row = self.one('SELECT value FROM state WHERE key=?',(key,))
        return json.loads(row['value']) if row else default

    def set_state(self,key,value):
        self.write('INSERT OR REPLACE INTO state VALUES(?,?)',(key,json.dumps(value)))

    def mark_seen(self,source,id):
        return self.write('INSERT OR IGNORE INTO seen VALUES(?,?,?)',(source,id,now())) == 1
