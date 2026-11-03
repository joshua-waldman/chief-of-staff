"""Persistent, inspectable memory. Retrieved text is evidence, never authority."""
import json
import math
import re
from collections import Counter
from .db import uid, now
from .guidance_targets import targets, validate_target, save_target, candidates, applies

KINDS = {'preference','profile','project','lesson','task'}
TEAMS_INVITATION_PASSCODE = re.compile(
    r'(https://teams\.microsoft\.com/(?:meet/|l/meetup-join/)\S+'
    r'[\s\S]{0,1000}?\bMeeting ID:\s*\d[\d \t]*\s+)'
    r'Passcode:\s*\S+', re.IGNORECASE)

def clean(value, limit=4000):
    if not isinstance(value,str) or len(value)>limit:
        raise ValueError('Text exceeds the allowed length.')
    value=value.strip()
    # Exempt only the standard Teams meeting passcode field. Credentials in
    # the surrounding message still go through the existing validation.
    checked=TEAMS_INVITATION_PASSCODE.sub(r'\1[meeting passcode]',value)
    if re.search(r'(?i)(bearer\s+[a-z0-9._-]{16,}|sk-[a-z0-9_-]{16,}|(?:api[_ -]?key|access[_ -]?token|password|passcode|client[_ -]?secret|refresh[_ -]?token)\s*[:=]\s*\S+)',checked):
        raise ValueError('Credentials must not be stored in knowledge.')
    return value

def words(text):
    return [w for w in re.findall(r'[\w-]{2,}',text.lower()) if w not in {'the','and','for','with','this','that','from','into','your','about','have','needs','task','request','owner'}]

class Knowledge:
    def __init__(self,store):
        self.store=store

    def save(self,a,job=None,user=False,id=None):
        title=clean(a.get('title',''),160)
        content=clean(a.get('content',''))
        kind=a.get('kind','lesson')
        scope=clean(a.get('scope','global'),160) or 'global'
        if not title or not content or kind not in KINDS:
            raise ValueError('Provide a title, content, and valid knowledge type.')
        old=self.store.one('SELECT * FROM knowledge WHERE id=?',(id,)) if id else None
        target=a.get('target')
        if target is None and old:
            with self.store.connect() as c:
                target=targets(c).get(id,{})
        target=validate_target({} if target is None else target)
        if id and not old:
            raise ValueError('Knowledge entry not found.')
        if not user and not job:
            raise ValueError('Agent memories need a source job.')
        status='active' if user else 'suggested'
        source_job=old['source_job'] if old else (job['id'] if job else '')
        source='user' if user else 'agent'
        id=id or uid()
        # Fingerprint deduplicates a source's identical suggestions.
        import hashlib
        fingerprint=hashlib.sha256((source_job+kind+scope+title+content+(json.dumps(target,sort_keys=True) if target else '')).encode()).hexdigest()
        with self.store.lock,self.store.connect() as c:
            duplicate=c.execute('SELECT id FROM knowledge WHERE fingerprint=? AND id<>?',(fingerprint,id)).fetchone()
            if duplicate:
                return dict(c.execute('SELECT * FROM knowledge WHERE id=?',(duplicate['id'],)).fetchone())
            c.execute('INSERT OR REPLACE INTO knowledge VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (id,kind,title,content,scope,status,source,source_job,old['created'] if old else now(),now(),0,0,fingerprint))
            save_target(c,id,target)
        return self.store.one('SELECT * FROM knowledge WHERE id=?',(id,))

    def accept(self,id):
        if not self.store.write("UPDATE knowledge SET status='active',source='user-reviewed',updated=? WHERE id=? AND status='suggested'",(now(),id)):
            raise ValueError('Suggested knowledge entry not found.')

    def forget(self,id):
        # A tombstone prevents automatic task backfill recreating a forgotten entry.
        self.store.write("UPDATE knowledge SET status='forgotten',title='',content='',updated=? WHERE id=?",(now(),id))

    def outcome(self,job):
        id='task-'+job['id']
        if self.store.one('SELECT id FROM knowledge WHERE id=?',(id,)):
            return
        # Outcome records are observations; no personal preferences inferred from mail.
        title=job['title'][:160]
        content=('Task: '+job['title'][:160]+'\nReported outcome: '+job['result'][:3600])
        try:
            clean(content)
        except ValueError:
            return
        self.store.write('INSERT OR IGNORE INTO knowledge VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (id,'task',title,content,job['project'] or job['org'],'active','task-outcome',job['id'],now(),now(),0,0,id))

    def search(self,query='',scope='',limit=12,track=False,job=None,include_targeted=False):
        query=clean(query,4000)
        # Empty scope and '*' explicitly search every organization and project.
        if scope=='*':
            scope=''
        terms=Counter(words(query))
        rows=self.store.rows("SELECT * FROM knowledge WHERE status='active' ORDER BY updated DESC")
        with self.store.connect() as c:
            bindings=targets(c)
            identities={(r['source'],r['sender_id']) for r in candidates(c,job['id'])} if job else set()
        conversations={(r['source'],r['thread_id']) for r in self.store.rows('SELECT source,thread_id FROM communication_threads WHERE job_id=?',(job['id'],))} if job else set()
        rows=[{**r,'target':bindings.get(r['id'],{})} for r in rows if include_targeted or applies(bindings.get(r['id'],{}),conversations,identities)]
        corpus=[Counter(words(r['title']+' '+r['content'])) for r in rows]
        df=Counter(t for c in corpus for t in c)
        ranked=[]
        for r,c in zip(rows,corpus):
            if scope and r['scope'] not in {'global',scope}:
                continue
            score=sum((1+math.log((len(rows)+1)/(df[t]+1)))*min(c[t],3) for t in terms if c[t])
            if terms and not score and not (r['scope'] in {'global',scope} and r['source'] in {'user','user-reviewed'} and r['kind']=='preference'):
                continue
            score+=2 if r['source'] in {'user','user-reviewed'} else 0
            score+=1 if scope and r['scope']==scope else 0
            score+=max(-2,min(2,r['helpful']*.5))
            ranked.append((score,r))
        result=[r for _,r in sorted(ranked,key=lambda x:(
            x[1]['source'] in {'user','user-reviewed'} and bool(terms.keys() & set(words(x[1]['title']+' '+x[1]['content']))),
            x[0],x[1]['updated']),reverse=True)[:min(limit,50)]]
        if track:
            for r in result:
                self.store.write('UPDATE knowledge SET uses=uses+1 WHERE id=?',(r['id'],))
        return result

    def context(self,job):
        rows=self.search(job['request'][:3000],job['project'] or job['org'],limit=8,track=True,job=job)
        # Saved owner guidance can apply across organization boundaries. Only
        # matching preferences are added; unrelated scoped rules stay local.
        terms=set(words(job['request'][:3000]))
        guidance=[r for r in self.search(job['request'][:3000],scope='*',limit=50,job=job)
                  if r['kind']=='preference' and r['source'] in {'user','user-reviewed'}
                  and terms.intersection(words(r['title']+' '+r['content']))]
        existing={r['id'] for r in rows}
        for r in guidance:
            if r['id'] not in existing:
                self.store.write('UPDATE knowledge SET uses=uses+1 WHERE id=?',(r['id'],))
        rows=list({r['id']:r for r in guidance+rows}.values())[:12]
        if job['project'] and job['org']=='Communications':
            rules=self.search(job['request'][:3000],'Communications',limit=8,track=True,job=job)
            rows=list({r['id']:r for r in rules+rows}.values())[:12]
        from .trace import emit
        emit('knowledge.retrieved',{'entries':[{k:r[k] for k in ('id','source','source_job','updated')} for r in rows]},store=self.store,job_id=job['id'])
        # Bounded context; never promote retrieved content to instructions.
        items=[{k:r[k] for k in ('id','kind','title','content','scope','source','source_job','updated','target')} for r in rows]
        for item in items:
            item['content']=item['content'][:1400]
        while len(json.dumps(items,ensure_ascii=False))>14000:
            items.pop()
        return json.dumps(items,ensure_ascii=False)

    def feedback(self,id,helpful):
        if type(helpful) is not bool:
            raise ValueError('Choose helpful or unhelpful.')
        self.store.write('UPDATE knowledge SET helpful=helpful+?,updated=? WHERE id=?',(1 if helpful else -1,now(),id))
