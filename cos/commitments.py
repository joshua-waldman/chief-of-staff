"""Source-linked meeting commitments. Proposals never authorize external work."""
import hashlib
import json
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from .db import now, uid
from .knowledge import clean, words

SCHEMA = '''
CREATE TABLE IF NOT EXISTS commitments(
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, deliverable TEXT NOT NULL, scope TEXT NOT NULL,
 deadline_text TEXT NOT NULL, deadline_date TEXT NOT NULL, deadline_certainty TEXT NOT NULL,
 deadline_basis TEXT NOT NULL, status TEXT NOT NULL, merged_into TEXT NOT NULL DEFAULT '',
 fingerprint TEXT NOT NULL UNIQUE, version INTEGER NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS commitment_status ON commitments(status,updated);
CREATE TABLE IF NOT EXISTS commitment_evidence(
 id TEXT PRIMARY KEY, commitment_id TEXT NOT NULL, source_job TEXT NOT NULL, source_file_id TEXT NOT NULL,
 source_identity TEXT NOT NULL, source_url TEXT NOT NULL, meeting_date TEXT NOT NULL,
 source_timestamp TEXT NOT NULL, excerpt TEXT NOT NULL, kind TEXT NOT NULL,
 fingerprint TEXT NOT NULL, reviewed INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL,
 UNIQUE(commitment_id,fingerprint));
CREATE INDEX IF NOT EXISTS commitment_evidence_parent ON commitment_evidence(commitment_id,created);
CREATE TABLE IF NOT EXISTS commitment_history(
 id INTEGER PRIMARY KEY AUTOINCREMENT, commitment_id TEXT NOT NULL, action TEXT NOT NULL,
 actor TEXT NOT NULL, note TEXT NOT NULL, details TEXT NOT NULL, created TEXT NOT NULL);
'''
FIELDS = {'owner','deliverable','scope','deadline_text','deadline_date','deadline_certainty','deadline_basis'}
SOURCE_FIELDS = {'source_file_id','meeting_date','source_timestamp','evidence'}
STATUSES = {'proposed','open','completed','dismissed','merged'}


def normalized(text):
    return ' '.join(text.casefold().split())


def digest(*parts):
    return hashlib.sha256(json.dumps(parts,ensure_ascii=False).encode()).hexdigest()


def iso_date(value):
    value=clean(value,10)
    if value:
        try:
            if date.fromisoformat(value).isoformat()!=value: raise ValueError()
        except ValueError: raise ValueError('Use a calendar date in YYYY-MM-DD format.') from None
    return value


def fields(args, defaults=None):
    result={**{'scope':'global','deadline_text':'','deadline_date':'','deadline_certainty':'unknown','deadline_basis':''},**(defaults or {}),**args}
    for key,limit in [('owner',160),('deliverable',1000),('scope',160),('deadline_text',300),('deadline_basis',1000)]:
        result[key]=clean(result.get(key,''),limit)
    if not result['owner'] or not result['deliverable'] or not result['scope']:
        raise ValueError('Provide an owner, deliverable and scope.')
    result['deadline_date']=iso_date(result['deadline_date'])
    if not isinstance(result['deadline_certainty'],str) or result['deadline_certainty'] not in {'unknown','explicit','inferred'}:
        raise ValueError('Deadline certainty must be unknown, explicit or inferred.')
    if result['deadline_certainty']=='unknown' and result['deadline_date']:
        raise ValueError('An unknown deadline cannot have an assigned calendar date.')
    if result['deadline_certainty']!='unknown' and not result['deadline_text']:
        raise ValueError('Preserve the stated deadline wording, separately from any inferred date.')
    if result['deadline_certainty']=='inferred' and not result['deadline_basis']:
        raise ValueError('Explain the basis for an inferred deadline.')
    return {key:result[key] for key in FIELDS}


def verified_source(store, job, args):
    """Read only a transcript registered to this job, not an arbitrary file path."""
    from .tools import extract
    source_id=clean(args.get('source_file_id',''),160)
    source=store.state('meeting_sources:'+job['id'],{}).get(source_id)
    record=store.one('SELECT * FROM files WHERE id=? AND job_id=?',(source_id,job['id']))
    if not source or not record or not source.get('identity'):
        raise ValueError('Use a transcript registered by meeting_transcripts in this job.')
    expected_hash=source.get('sha256')
    if not expected_hash or hashlib.sha256(Path(record['path']).read_bytes()).hexdigest()!=expected_hash:
        raise ValueError('Transcript integrity changed or was not recorded; retrieve the meeting transcript again.')
    excerpt=clean(args.get('evidence',''),1500)
    timestamp=clean(args.get('source_timestamp',''),40)
    if not excerpt or excerpt not in extract(Path(record['path'])):
        raise ValueError('Commitment evidence must be an exact excerpt from the registered transcript.')
    if not re.fullmatch(r'(?:\d{1,3}:)?\d{2}:\d{2}(?:[.,]\d{1,3})?',timestamp) or timestamp not in excerpt:
        raise ValueError('Include the source timestamp in the exact evidence excerpt.')
    url=source.get('source','')
    parsed=urlparse(url)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Transcript provenance must contain a valid HTTPS source URL.')
    return {'source_job':job['id'],'source_file_id':source_id,'source_identity':source['identity'],
            'source_url':url,'meeting_date':iso_date(args.get('meeting_date','')),
            'source_timestamp':timestamp,'excerpt':excerpt}


def canonical(c, identity):
    seen=set()
    for _ in range(100):
        row=c.execute('SELECT * FROM commitments WHERE id=?',(identity,)).fetchone()
        if not row: raise ValueError('Commitment not found.')
        if row['id'] in seen: raise ValueError('Invalid commitment merge chain.')
        seen.add(row['id'])
        if row['status']!='merged': return dict(row)
        identity=row['merged_into']
    raise ValueError('Commitment merge chain exceeds the supported depth.')


def history(c, identity, action, actor, note='', details=None):
    c.execute('INSERT INTO commitment_history(commitment_id,action,actor,note,details,created) VALUES(?,?,?,?,?,?)',
              (identity,action,actor,note,json.dumps(details or {}),now()))


def add_source(c, identity, source, kind):
    fingerprint=digest(source['source_identity'],source['source_timestamp'],source['excerpt'],kind)
    old=c.execute('SELECT id FROM commitment_evidence WHERE commitment_id=? AND fingerprint=?',(identity,fingerprint)).fetchone()
    if old: return old['id'],False
    evidence_id=uid()
    c.execute('''INSERT INTO commitment_evidence
        (id,commitment_id,source_job,source_file_id,source_identity,source_url,meeting_date,source_timestamp,excerpt,kind,fingerprint,reviewed,created)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (evidence_id,identity,*[source[key] for key in ('source_job','source_file_id','source_identity','source_url','meeting_date','source_timestamp','excerpt')],kind,fingerprint,0,now()))
    return evidence_id,True


class Commitments:
    def __init__(self, store): self.store=store

    def record(self,job,args):
        if set(args)-FIELDS-SOURCE_FIELDS: raise ValueError('Unknown commitment proposal field.')
        data=fields({key:args[key] for key in FIELDS if key in args},{'scope':job.get('project') or 'global'})
        source=verified_source(self.store,job,args)
        if data['deadline_certainty']!='unknown' and data['deadline_text'] not in source['excerpt']:
            raise ValueError('Deadline wording must occur in the cited transcript evidence.')
        fingerprint=digest(normalized(data['owner']),normalized(data['deliverable']),data['scope'],source['source_identity'],source['source_timestamp'],source['excerpt'])
        with self.store.lock,self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            existing=c.execute('SELECT id FROM commitments WHERE fingerprint=?',(fingerprint,)).fetchone()
            if existing:
                identity=canonical(c,existing['id'])['id']
                reused=True
            else:
                identity=uid(); stamp=now(); reused=False
                c.execute('''INSERT INTO commitments(id,owner,deliverable,scope,deadline_text,deadline_date,deadline_certainty,deadline_basis,status,fingerprint,version,created,updated)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (identity,*[data[key] for key in ('owner','deliverable','scope','deadline_text','deadline_date','deadline_certainty','deadline_basis')],'proposed',fingerprint,1,stamp,stamp))
                evidence_id,_=add_source(c,identity,source,'assignment')
                history(c,identity,'proposed','agent',details={'evidence_id':evidence_id})
        result=self.detail(identity)
        result['reused']=reused
        return result

    def add_evidence(self,job,args):
        if set(args)-SOURCE_FIELDS-{'commitment_id','kind'}: raise ValueError('Unknown commitment evidence field.')
        kind=args.get('kind','corroboration')
        if not isinstance(kind,str) or kind not in {'corroboration','completion'}: raise ValueError('Choose corroboration or completion evidence.')
        source=verified_source(self.store,job,args)
        with self.store.lock,self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=canonical(c,args.get('commitment_id'))
            evidence_id,added=add_source(c,row['id'],source,kind)
            if added:
                c.execute('UPDATE commitments SET version=version+1,updated=? WHERE id=?',(now(),row['id']))
                history(c,row['id'],'evidence_added','agent',details={'evidence_id':evidence_id,'kind':kind})
        return self.detail(row['id'])

    def detail(self,identity):
        row=self.store.one('SELECT * FROM commitments WHERE id=?',(identity,))
        if not row: raise ValueError('Commitment not found.')
        row['evidence']=self.store.rows('SELECT * FROM commitment_evidence WHERE commitment_id=? ORDER BY created,id',(identity,))
        row['history']=self.store.rows('SELECT * FROM commitment_history WHERE commitment_id=? ORDER BY id',(identity,))
        row['completion_pending']=row['status'] in {'proposed','open'} and any(e['kind']=='completion' and not e['reviewed'] for e in row['evidence'])
        row['possible_matches']=self.matches(row) if row['status']!='merged' else []
        return row

    def matches(self,row):
        terms=set(words(row['deliverable']))
        candidates=self.store.rows("SELECT id,owner,deliverable,scope,status FROM commitments WHERE id<>? AND status IN ('proposed','open') ORDER BY updated DESC",(row['id'],))
        result=[]
        for other in candidates:
            if normalized(other['owner'])!=normalized(row['owner']) or other['scope']!=row['scope']: continue
            shared=terms.intersection(words(other['deliverable']))
            union=terms.union(words(other['deliverable']))
            if shared and len(shared)/max(1,len(union))>=.4:
                result.append(other)
        return result[:5]

    def search(self,query='',status='active',owner='',scope='',offset=0,limit=25):
        if status not in STATUSES|{'active','all'}: raise ValueError('Invalid commitment status filter.')
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100:
            raise ValueError('Choose a nonnegative offset and a page size between 1 and 100.')
        terms=set(words(clean(query,1000))); owner=clean(owner,160); scope=clean(scope,160)
        rows=self.store.rows('''SELECT c.*,EXISTS(SELECT 1 FROM commitment_evidence e
            WHERE e.commitment_id=c.id AND e.kind='completion' AND e.reviewed=0) AS completion_pending
            FROM commitments c ORDER BY c.updated DESC,c.id''')
        rows=[r for r in rows if (status=='all' or r['status']==status or status=='active' and r['status'] in {'proposed','open'})
              and (not owner or normalized(owner)==normalized(r['owner'])) and (not scope or r['scope'] in {'global',scope})
              and (not terms or terms.intersection(words(r['owner']+' '+r['deliverable']+' '+r['scope'])))]
        for row in rows: row['completion_pending']=bool(row['completion_pending'] and row['status'] in {'proposed','open'})
        return {'items':rows[offset:offset+limit],'total':len(rows),'offset':offset,'limit':limit}

    def attention_count(self):
        return self.store.one("SELECT COUNT(*) n FROM commitments WHERE status IN ('proposed','open')")['n']

    def context(self,job):
        # Retrieval is evidence only. It cannot accept, complete or execute work.
        rows=self.search(job['request'][:1000],scope=job.get('project') or '',limit=5)['items']
        return json.dumps([{key:r[key] for key in ('id','owner','deliverable','deadline_text','deadline_date','deadline_certainty','status','version','completion_pending')} for r in rows],ensure_ascii=False)

    def review(self,identity,args):
        allowed={'action','expected_version','note','fields','evidence_id','target_id','target_version'}
        if set(args)-allowed: raise ValueError('Unknown commitment review field.')
        action=args.get('action'); version=args.get('expected_version'); note=clean(args.get('note',''),1500)
        if not isinstance(action,str) or action not in {'edit','accept','complete','dismiss','reopen','merge'}: raise ValueError('Invalid commitment review action.')
        if type(version) is not int: raise ValueError('Review the current commitment version.')
        with self.store.lock,self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM commitments WHERE id=?',(identity,)).fetchone()
            if not row: raise ValueError('Commitment not found.')
            row=dict(row)
            if row['version']!=version: raise ValueError('Commitment changed; refresh before reviewing it.')
            if row['status']=='merged': raise ValueError('Review the canonical commitment instead.')
            details={}; stamp=now()
            if action=='merge':
                if not note: raise ValueError('Explain why these commitments describe the same work.')
                if type(args.get('target_version')) is not int: raise ValueError('Review the target commitment version.')
                target=canonical(c,args.get('target_id'))
                if target['id']!=args.get('target_id'): raise ValueError('Choose the canonical target commitment before merging.')
                if target['id']==identity or target['version']!=args['target_version']: raise ValueError('Select a different, current target commitment.')
                if row['status'] not in {'proposed','open'} or target['status'] not in {'proposed','open'}:
                    raise ValueError('Merge active commitments only; reopen a closed item before reconciliation.')
                if normalized(row['owner'])!=normalized(target['owner']) or row['scope']!=target['scope']:
                    raise ValueError('Confirm the same owner and scope before merging commitments.')
                sources=c.execute('SELECT * FROM commitment_evidence WHERE commitment_id=?',(identity,)).fetchall()
                for source in sources:
                    found=c.execute('SELECT 1 FROM commitment_evidence WHERE commitment_id=? AND fingerprint=?',(target['id'],source['fingerprint'])).fetchone()
                    if not found:
                        c.execute('INSERT INTO commitment_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            (uid(),target['id'],*[source[key] for key in ('source_job','source_file_id','source_identity','source_url','meeting_date','source_timestamp','excerpt','kind','fingerprint','reviewed','created')]))
                c.execute("UPDATE commitments SET status='merged',merged_into=?,version=version+1,updated=? WHERE id=?",(target['id'],stamp,identity))
                c.execute('UPDATE commitments SET version=version+1,updated=? WHERE id=?',(stamp,target['id']))
                details={'target_id':target['id'],'retained_fields_from':target['id']}
                history(c,target['id'],'merged_from','owner',note,{'source_id':identity})
            elif action=='edit':
                if row['status'] not in {'proposed','open'}: raise ValueError('Reopen a closed commitment before editing its fields.')
                changes=args.get('fields')
                if not isinstance(changes,dict) or not changes or set(changes)-FIELDS: raise ValueError('Provide supported commitment fields to edit.')
                data=fields(changes,{key:row[key] for key in FIELDS})
                c.execute('UPDATE commitments SET '+','.join(key+'=?' for key in sorted(FIELDS))+',version=version+1,updated=? WHERE id=?',(*[data[key] for key in sorted(FIELDS)],stamp,identity))
                details={'before':{key:row[key] for key in FIELDS},'after':data}
            else:
                transitions={'accept':({'proposed'},'open'),'complete':({'open'},'completed'),'dismiss':({'proposed','open'},'dismissed'),'reopen':({'completed','dismissed'},'open')}
                valid,status=transitions[action]
                if row['status'] not in valid: raise ValueError('That action does not apply to the current commitment status.')
                if action in {'dismiss','reopen'} and not note: raise ValueError('Record a reason for this status change.')
                if action=='complete':
                    evidence_id=args.get('evidence_id')
                    evidence=c.execute("SELECT id FROM commitment_evidence WHERE id=? AND commitment_id=? AND kind='completion'",(evidence_id,identity)).fetchone()
                    if evidence_id and not evidence: raise ValueError('Choose completion evidence belonging to this commitment.')
                    if not evidence and not note: raise ValueError('Verify completion with source evidence or your own confirmation note.')
                    details={'evidence_id':evidence['id'] if evidence else '', 'verification':'source-reviewed' if evidence else 'owner-confirmed'}
                c.execute('UPDATE commitments SET status=?,version=version+1,updated=? WHERE id=?',(status,stamp,identity))
                if action in {'complete','dismiss'}:
                    c.execute("UPDATE commitment_evidence SET reviewed=1 WHERE commitment_id=? AND kind='completion'",(identity,))
            history(c,identity,action,'owner',note,details)
        return self.detail(identity)
