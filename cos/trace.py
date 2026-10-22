"""Structured diagnostic traces: identifiers and fingerprints, never credentials/bodies."""
import contextvars
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import threading
import time
import traceback
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit
from . import config
from .db import uid,now

CURRENT=contextvars.ContextVar('cos_trace',default={})
LOCK=threading.RLock()
LOGGER=logging.getLogger('cos.diagnostics')
LOGGER.propagate=False
LOGGER.setLevel(logging.INFO)
LOG_PATH=None

def fingerprint(value):
    raw=json.dumps(value,ensure_ascii=False,sort_keys=True,default=str).encode('utf-8')
    return {'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}

def shape(value):
    if isinstance(value,dict):
        return {str(k):('[REDACTED]' if re.search(r'(?i)key|token|secret|password|authorization|cookie',str(k)) else shape(v)) for k,v in value.items()}
    if isinstance(value,list):
        return {'type':'list','count':len(value),'fingerprint':fingerprint(value)}
    if isinstance(value,str):
        return {'type':'text',**fingerprint(value)}
    return value if isinstance(value,(int,float,bool,type(None))) else {'type':type(value).__name__}

def failure(error):
    message=str(error)
    http=re.search(r'(?:Graph API|HTTP)\s+(\d{3})',message)
    code=re.search(r'"code"\s*:\s*"([A-Za-z0-9_.-]{1,100})"',message)
    return {'class':type(error).__name__,'fingerprint':fingerprint(message),
            'http_status':int(http.group(1)) if http else None,
            'service_code':code.group(1) if code else None,
            'stack':[{'file':Path(f.filename).name,'line':f.lineno,'function':f.name} for f in traceback.extract_tb(error.__traceback__)[-10:]]}

@contextmanager
def context(**values):
    token=CURRENT.set({**CURRENT.get(),**values})
    try:yield
    finally:CURRENT.reset(token)

def emit(phase,details=None,store=None,job_id=None):
    """A diagnostic I/O failure must never turn a successful send into a retry."""
    try:
        ctx=CURRENT.get()
        record={'id':uid(),'time':now(),'phase':phase,'process':os.getpid(),
                **{k:v for k,v in ctx.items() if k!='store'},'details':details or {}}
        record['job_id']=job_id or ctx.get('job_id','')
        target=store or ctx.get('store')
        if target and record['job_id']:
            target.write('INSERT INTO trace_events VALUES(?,?,?,?,?)',(record['id'],record['job_id'],record['time'],phase,json.dumps(record)))
        # Test traces stay in their temporary SQLite database.
        if not os.environ.get('PYTEST_CURRENT_TEST'):
            global LOG_PATH
            path=config.DATA/'logs'/'diagnostics.jsonl'
            with LOCK:
                if LOG_PATH!=path:
                    for handler in list(LOGGER.handlers):
                        handler.close();LOGGER.removeHandler(handler)
                    path.parent.mkdir(parents=True,exist_ok=True)
                    handler=RotatingFileHandler(path,maxBytes=10*1024*1024,backupCount=5,encoding='utf-8')
                    handler.setFormatter(logging.Formatter('%(message)s'))
                    LOGGER.addHandler(handler);LOG_PATH=path
                LOGGER.info(json.dumps(record,ensure_ascii=False))
        return record['id']
    except Exception:
        logging.getLogger(__name__).warning('Diagnostic write failed; workflow outcome unchanged.',exc_info=False)
        return None

def job_run(fn):
    @wraps(fn)
    def wrapped(self,id,*args,**kwargs):
        job=self.store.job(id)
        with context(store=self.store,job_id=id,trace_id=uid(),parent_job_id=job.get('parent_id') if job else None,organization=job.get('org') if job else None):
            start=time.monotonic()
            emit('job.started',{'request':fingerprint(job['request']) if job else None,'prior_status':job.get('status') if job else None,'step':job.get('step') if job else None})
            try:return fn(self,id,*args,**kwargs)
            finally:
                result=self.store.job(id)
                emit('job.stopped',{'duration_ms':round((time.monotonic()-start)*1000),'status':result.get('status') if result else None,'step':result.get('step') if result else None})
    return wrapped

def approval_call(fn):
    @wraps(fn)
    def wrapped(self,aid,approved,*args,**kwargs):
        a=self.store.one('SELECT job_id,tool FROM approvals WHERE id=?',(aid,))
        with context(store=self.store,job_id=a['job_id'] if a else '',trace_id=uid(),action_id=aid):
            emit('approval.decision',{'approved':approved,'tool':a['tool'] if a else None})
            try:return fn(self,aid,approved,*args,**kwargs)
            except Exception as error:
                emit('approval.error',failure(error));raise
            finally:
                outcome=self.store.one('SELECT status FROM approvals WHERE id=?',(aid,))
                emit('approval.outcome',{'status':outcome['status'] if outcome else 'missing'})
    return wrapped

def tool_call(fn):
    @wraps(fn)
    def wrapped(self,job,action,a):
        active=self.store.one("SELECT id FROM approvals WHERE job_id=? AND tool=? AND status='executing' ORDER BY created DESC LIMIT 1",(job['id'],action))
        with context(store=self.store,job_id=job['id'],action_id=active['id'] if active else uid(),organization=job['org']):
            meta={'tool':action,'arguments':shape(a)}
            if action=='send_email':
                value=a.get('to','')
                meta['recipient_format']={'semicolons':value.count(';'),'commas':value.count(',')} if isinstance(value,str) else {'array_count':len(value) if isinstance(value,list) else 0}
            if action in {'jira_read','jira_write'}:
                meta['jira_targets']=[v for v in [a.get('id',''),*a.get('args',[])] if isinstance(v,str) and re.fullmatch(r'[A-Z][A-Z0-9_]{1,30}-[1-9][0-9]{0,12}',v)]
                if a.get('operation') in {'comment','edit','create'}:meta['operation']=a['operation']
                if isinstance(a.get('command'),str) and re.fullmatch(r'(?:jira-|conf-)[a-z-]+|check',a['command']):meta['command']=a['command']
            emit('tool.started',meta)
            start=time.monotonic()
            try:
                result=fn(self,job,action,a)
                extra={k:result[k] for k in ('source','sha256','truncated','file_id','exit_code') if isinstance(result,dict) and k in result}
                if isinstance(result,dict) and action in {'jira_read','jira_write'}:
                    receipt={}
                    for key in ('id','key','comment_id','commentId','issueKey'):
                        value=str(result.get(key,''))
                        if re.fullmatch(r'[0-9]{1,20}|[A-Z][A-Z0-9_]{1,30}-[1-9][0-9]{0,12}',value):receipt[key]=value
                    url=result.get('url','')
                    if isinstance(url,str):
                        parsed=urlsplit(url)
                        if parsed.scheme=='https' and parsed.hostname=='amd.atlassian.net' and not parsed.username and not parsed.password:
                            receipt['url']=urlunsplit((parsed.scheme,parsed.netloc,parsed.path,'',''))
                    extra['external_receipt']=receipt
                if isinstance(result,dict) and action=='send_email':
                    extra['submission_accepted']=result.get('message')=='Email sent'
                    extra['recipient_count']=len(result.get('to',[])) if isinstance(result.get('to'),list) else None
                emit('tool.succeeded',{'tool':action,'duration_ms':round((time.monotonic()-start)*1000),'result':fingerprint(result),'provenance':extra})
                return result
            except BaseException as error:
                emit('tool.failed',{'tool':action,'duration_ms':round((time.monotonic()-start)*1000),**failure(error)})
                raise
    return wrapped

def model_call(fn):
    @wraps(fn)
    def wrapped(self,messages,role='worker',tools=None,**options):
        emit('model.started',{'role':role,'messages':fingerprint(messages),'message_count':len(messages),'tools_enabled':bool(tools)})
        start=time.monotonic()
        try:
            answer,meta=fn(self,messages,role=role,tools=tools,**options)
            emit('model.succeeded',{'duration_ms':round((time.monotonic()-start)*1000),'metadata':meta,'output':fingerprint(answer)})
            return answer,meta
        except Exception as error:
            emit('model.failed',{'duration_ms':round((time.monotonic()-start)*1000),**failure(error)})
            raise
    return wrapped

def bundle(store,id):
    if not store.job(id):
        raise ValueError('Job not found')
    jobs=store.rows('WITH RECURSIVE tree(id) AS (SELECT id FROM jobs WHERE id=? UNION ALL SELECT j.id FROM jobs j JOIN tree t ON j.parent_id=t.id) SELECT j.id,j.parent_id,j.org,j.project,j.status,j.created,j.updated,j.step FROM jobs j JOIN tree t ON j.id=t.id LIMIT 1000',(id,))
    result={'schema':1,'exported':now(),'root_job':id,'jobs':jobs,'traces':[],'approvals':[],'files':[],'legacy_events':[]}
    for j in jobs:
        jid=j['id']
        result['traces'] += [json.loads(r['payload']) for r in store.rows('SELECT payload FROM trace_events WHERE job_id=? ORDER BY created',(jid,))]
        for a in store.rows('SELECT * FROM approvals WHERE job_id=?',(jid,)):
            result['approvals'].append({'id':a['id'],'job_id':jid,'tool':a['tool'],'status':a['status'],'risk':a['risk'],'created':a['created'],'arguments':shape(json.loads(a['args'])),'result':fingerprint(a['result'])})
        for f in store.rows('SELECT * FROM files WHERE job_id=?',(jid,)):
            provenance=store.one('SELECT sha256,size,created FROM artifact_provenance WHERE file_id=?',(f['id'],))
            result['files'].append({'id':f['id'],'job_id':jid,'name':f['name'],'mime':f['mime'],'created':f['created'],'provenance':provenance})
        for e in store.rows('SELECT id,kind,body,created FROM events WHERE job_id=? ORDER BY id',(jid,)):
            result['legacy_events'].append({'id':e['id'],'job_id':jid,'kind':e['kind'],'created':e['created'],'body':fingerprint(e['body'])})
    return result
