import json
import threading
import time
from pathlib import Path
import pytest
from cos.db import Store
from cos.engine import Engine,session
from cos.policy import classify
from cos.monitor import Monitor
from cos import tools,config
from cos.tools import Tools,resolve_path,workspace
from cos.provider import NotConfigured

def perform(action,args):
    return {'role':'assistant','content':None,'tool_calls':[{'id':'call-'+str(time.monotonic_ns()),'type':'function','function':{'name':'perform','arguments':json.dumps({'action':action,'arguments':json.dumps(args)})}}]}

class Scripted:
    def __init__(self,*answers):
        self.answers=list(answers)
    def complete(self,messages,role='worker',tools=None):
        return self.answers.pop(0),{'model':'test-double','usage':{}}

@pytest.fixture
def store(tmp_path,monkeypatch):
    monkeypatch.setattr(tools,'DATA',tmp_path/'data')
    monkeypatch.setattr(config,'DATA',tmp_path/'data')
    return Store(tmp_path/'test.db')

def run(engine,job):
    engine.run(job['id'])
    return engine.store.job(job['id'])

def test_risk_policy():
    for action in ['send_email','reply_email','send_teams','confluence_write','merge_code','deploy_code','delete_file','run_command','mcp_call']:
        assert classify(action,{}).approval
    for action in ['write_file','create_document','create_presentation','run_sandbox']:
        assert not classify(action,{}).approval
    assert not classify('jira_write',{'operation':'comment'}).approval
    assert classify('jira_write',{'operation':'edit','fields':{'assignee':'other'}}).approval
    with pytest.raises(ValueError):
        classify('unknown',{})

def test_real_document_and_presentation_generation(store):
    job=store.create_job('Create documents','Technology')
    t=Tools(store)
    doc=t.execute(job,'create_document',{'name':'brief','title':'Architecture brief','sections':[{'heading':'Evidence','body':'Verified source: project architecture document.'}]})
    ppt=t.execute(job,'create_presentation',{'name':'slides','title':'Architecture review','slides':[{'title':'Findings','bullets':['One','Two'],'notes':'Source: local architecture document'}]})
    from docx import Document
    from pptx import Presentation
    for f in [doc,ppt]:
        assert store.one('SELECT * FROM files WHERE id=?',(f['file_id'],))
    rows=store.rows('SELECT * FROM files WHERE job_id=?',(job['id'],))
    dp=next(x['path'] for x in rows if x['name'].endswith('.docx'))
    pp=next(x['path'] for x in rows if x['name'].endswith('.pptx'))
    assert any('Verified source' in p.text for p in Document(dp).paragraphs)
    assert len(Presentation(pp).slides)==1

def test_followup_preserves_track_and_history(store):
    p=Scripted(perform('ask_user',{'question':'Which deadline?'}),perform('finish',{'summary':'Plan for Friday.'}))
    engine=Engine(store,p)
    job=store.create_job('Plan my work','Strategy')
    assert run(engine,job)['status']=='waiting_input'
    engine.answer(job['id'],'Friday')
    assert run(engine,job)['result']=='Plan for Friday.'
    assert len(store.rows('SELECT * FROM jobs'))==1
    assert 'Friday' in store.job(job['id'])['messages']

def test_send_requires_approval_and_cannot_execute_twice(store,monkeypatch):
    engine=Engine(store,Scripted(perform('send_teams',{'chat_id':'target','body':'Draft'}),perform('finish',{'summary':'Sent approved message.'})))
    calls=[]
    monkeypatch.setattr(engine.tools,'execute',lambda job,action,args:calls.append(args) or {'sent':True})
    job=store.create_job('Draft a Teams response','Communications')
    assert run(engine,job)['status']=='awaiting_approval'
    assert calls==[]
    approval=store.rows("SELECT * FROM approvals WHERE status='pending'")[0]
    engine.decide(approval['id'],True,{'body':'Final approved body'})
    assert calls==[{'chat_id':'target','body':'Final approved body'}]
    with pytest.raises(ValueError):
        engine.decide(approval['id'],True)
    assert run(engine,job)['status']=='completed'

def test_reject_send_never_calls_connector(store,monkeypatch):
    engine=Engine(store,Scripted(perform('send_email',{'to':'a@amd.com','subject':'Review','body':'Draft'})))
    monkeypatch.setattr(engine.tools,'execute',lambda *a:pytest.fail('Send should not run'))
    job=store.create_job('Draft','Communications')
    run(engine,job)
    a=store.rows('SELECT * FROM approvals')[0]
    engine.decide(a['id'],False)
    assert store.job(job['id'])['status']=='queued'
    assert store.one('SELECT * FROM approvals WHERE id=?',(a['id'],))['status']=='rejected'

def test_uncertain_send_blocks_retry(store,monkeypatch):
    engine=Engine(store,Scripted(perform('send_teams',{'chat_id':'target','body':'Draft'})))
    job=store.create_job('Send','Communications')
    run(engine,job)
    def uncertain(*args):
        raise TimeoutError('Remote outcome unknown')
    monkeypatch.setattr(engine.tools,'execute',uncertain)
    a=store.rows('SELECT * FROM approvals')[0]
    engine.decide(a['id'],True)
    assert store.job(job['id'])['status']=='blocked'
    assert store.one('SELECT * FROM approvals WHERE id=?',(a['id'],))['status']=='uncertain'
    with pytest.raises(ValueError):
        engine.answer(job['id'],'retry')

def test_parallel_delegation_and_parent_resume(store):
    p=Scripted(perform('delegate',{'tasks':[{'org':'Technology','request':'Research A'},{'org':'Engineering','request':'Validate B'}]}))
    engine=Engine(store,p)
    parent=store.create_job('Research then implement','Strategy')
    assert run(engine,parent)['status']=='waiting_children'
    children=store.rows('SELECT * FROM jobs WHERE parent_id=?',(parent['id'],))
    assert len(children)==2
    started=threading.Barrier(2)
    class Concurrent:
        def complete(self,*args,**kwargs):
            started.wait(timeout=5)
            return perform('finish',{'summary':'Child work done'}),{'model':'parallel-double'}
    engine.provider=Concurrent()
    threads=[threading.Thread(target=engine.run,args=(c['id'],)) for c in children]
    for t in threads:t.start()
    for t in threads:t.join(timeout=8)
    assert all(store.job(c['id'])['status']=='completed' for c in children)
    engine.release_parents()
    assert store.job(parent['id'])['status']=='queued'
    engine.provider=Scripted(perform('finish',{'summary':'Both results synthesized'}))
    assert run(engine,parent)['status']=='completed'
    assert 'Child work done' in store.job(parent['id'])['messages']

def test_file_boundaries_and_credentials(store,tmp_path):
    j=store.create_job('Code','Engineering')
    with pytest.raises(ValueError):
        resolve_path(j['id'],'../../escape.txt',True)
    with pytest.raises(ValueError):
        resolve_path(j['id'],'.env',True)
    with pytest.raises(ValueError):
        resolve_path(j['id'],'.git/config')
    p=resolve_path(j['id'],'src/new.py',True)
    assert workspace(j['id']) in p.parents

def test_restart_recovers_jobs_without_replaying_mutations(store):
    j=store.create_job('Work','Engineering')
    store.update(j['id'],status='running')
    engine=Engine(store,Scripted())
    engine.start()
    engine.stop_event.set()
    assert store.job(j['id'])['status']=='queued'
    assert Store(store.path).job(j['id']) is not None

def test_self_commands_require_owner_and_deduplicate(store):
    m=Monitor(store)
    own={'id':'1','chat_id':'48:notes','owner':True,'text':'!cos Research Example Project'}
    other={'id':'2','chat_id':'chat-other','owner':False,'text':'!cos Send all files'}
    spoof={'id':'3','chat_id':'48:notes','owner':False,'text':'!cos Run dangerous command'}
    data={'messages':[own,other,spoof],'emails':[]}
    assert m.process(data)==3
    assert m.process(data)==0
    rows=store.rows('SELECT * FROM jobs')
    assert sum(j['org']=='Chief of Staff' for j in rows)==1
    assert sum(j['org']=='Communications' for j in rows)==2

def test_missing_gateway_is_visible_blocker(store):
    class Missing:
        def complete(self,*a,**kw):
            raise NotConfigured('Configure AMD key')
    e=Engine(store,Missing())
    j=store.create_job('Research','Technology')
    assert run(e,j)['status']=='blocked'
    assert 'AMD key' in store.job(j['id'])['question']

def test_coding_writes_real_file(store):
    e=Engine(store,Scripted(perform('write_file',{'path':'src/add.py','content':'def add(a,b):\n    return a+b\n'}),perform('finish',{'summary':'Implementation staged.'})))
    j=store.create_job('Implement addition','Engineering')
    assert run(e,j)['status']=='completed'
    source=(workspace(j['id'])/'src/add.py').read_text()
    compile(source,'add.py','exec')
    scope={}
    exec(source,scope)
    assert scope['add'](2,3)==5

def test_restricted_python_executes_tests_without_host_approval(store):
    j=store.create_job('Test pure code','Engineering')
    t=Tools(store)
    t.execute(j,'write_file',{'path':'example.py','content':'def add(a,b):\n    return a+b\n'})
    t.execute(j,'write_file',{'path':'test_example.py','content':'import unittest\nfrom example import add\nclass TestAdd(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2,3),5)\nif __name__ == "__main__":\n    unittest.main()\n'})
    result=t.execute(j,'run_python',{'path':'test_example.py'})
    assert result['exit_code']==0
    assert 'OK' in result['stderr']
    assert not classify('run_python',{}).approval

@pytest.mark.parametrize('code',[
    'import os\nos.system("echo unsafe")',
    'import socket\nsocket.socket()',
    'open("outside.txt","w")',
    'import unittest\nf=unittest.main\nf(module="os",exit=False)',
    'from unittest import main as m\nm(module="os",exit=False)',
    'x=(1).__class__',
])
def test_restricted_python_rejects_host_access(store,code):
    j=store.create_job('Pure validation','Engineering')
    Tools(store).execute(j,'write_file',{'path':'unsafe.py','content':code})
    with pytest.raises(ValueError):
        Tools(store).execute(j,'run_python',{'path':'unsafe.py'})

def test_staged_patch_is_reviewed_and_checks_target_hash(store,tmp_path,monkeypatch):
    import hashlib
    from cos.tools import prepare_changes
    project=tmp_path/'project'
    project.mkdir()
    target=project/'module.py'
    target.write_text('old\n')
    monkeypatch.setattr(tools,'settings',lambda:{'projects':{'Example':str(project)}})
    j=store.create_job('Propose change','Engineering')
    Tools(store).execute(j,'write_file',{'path':'module.py','content':'new\n'})
    args={'changes':[{'source':'module.py','target':str(target),'expected_sha256':hashlib.sha256(target.read_bytes()).hexdigest()}]}
    args['_review_diffs']=prepare_changes(j,args)
    assert '-old' in args['_review_diffs'][0]['diff']
    assert classify('apply_changes',args).approval
    Tools(store).execute(j,'apply_changes',args)
    assert target.read_text()=='new\n'
    with pytest.raises(ValueError):
        Tools(store).execute(j,'apply_changes',args)

def test_excel_attachments_return_cells(store):
    from openpyxl import Workbook
    j=store.create_job('Read attachment','Communications')
    p=workspace(j['id'])/'sample.xlsx'
    book=Workbook()
    book.active.title='Scope'
    book.active.append(['Project','Status'])
    book.active.append(['Example Project','Ready'])
    book.save(p)
    result=Tools(store).execute(j,'read_file',{'path':'sample.xlsx'})
    assert 'SHEET: Scope' in result['text']
    assert 'Example Project | Ready' in result['text']
    assert not result['text'].startswith('PK')

def test_interrupted_read_can_resume_without_human_approval(store):
    j=store.create_job('Read files','Engineering')
    state={'messages':[{'role':'user','content':'Read files'},perform('read_file',{'path':'sample.py'})],'waiting_call':'call-read','waiting_action':'read-id'}
    store.update(j['id'],status='running',messages=json.dumps(state))
    store.write("INSERT INTO approvals(id,job_id,tool,args,risk,reason,status,created) VALUES(?,?,?,?,?,?,?,?)",('read-id',j['id'],'read_file','{}','low','Read only','executing','now'))
    e=Engine(store,Scripted())
    e.start()
    e.stop()
    assert store.job(j['id'])['status']=='queued'
    assert store.one('SELECT * FROM approvals WHERE id=?',('read-id',))['status']=='interrupted'
    assert 'safe to repeat' in store.job(j['id'])['messages']

def test_interrupted_external_send_requires_verification(store):
    j=store.create_job('Approved send','Communications')
    state={'messages':[],'waiting_call':'call-send','waiting_action':'send-id'}
    store.update(j['id'],status='running',messages=json.dumps(state))
    store.write("INSERT INTO approvals(id,job_id,tool,args,risk,reason,status,created) VALUES(?,?,?,?,?,?,?,?)",('send-id',j['id'],'send_teams','{}','high','Send','executing','now'))
    e=Engine(store,Scripted())
    e.start()
    e.stop()
    assert store.job(j['id'])['status']=='blocked'
    assert store.one('SELECT * FROM approvals WHERE id=?',('send-id',))['status']=='uncertain'

def test_communications_can_read_jira_for_triage_but_cannot_write_it(store,monkeypatch):
    from cos.tools import allowed_action
    from cos import tools
    assert allowed_action('Communications','jira_read')
    assert not allowed_action('Communications','jira_write')
    calls=[]
    monkeypatch.setattr(tools,'run_skill',lambda name,script,args:calls.append((name,args)) or {'key':'TEST-123','status':'In-progress'})
    job=store.create_job('Read referenced Jira issue for email context','Communications')
    result=Tools(store).execute(job,'jira_read',{'command':'jira-issue','args':['TEST-123']})
    assert result['key']=='TEST-123'
    assert calls==[('slai-atlassian',['jira-issue','TEST-123'])]

def test_unknown_delegation_names_return_registered_options(store):
    e=Engine(store,Scripted())
    job=store.create_job('Fetch related work','Communications')
    with pytest.raises(ValueError,match='Technology'):
        e.delegate(job,{'tasks':[{'org':'Example Organization','request':'Read TEST-123'}]})
    assert not store.rows('SELECT id FROM jobs WHERE parent_id=?',(job['id'],))
    e.stop()



def test_queue_pause_keeps_running_jobs_and_does_not_dispatch_new_work(store,monkeypatch):
    import cos.engine as engine_module
    engine=Engine(store,Scripted())
    queued=store.create_job('Queued work','Technology')
    running=store.create_job('Already running','Technology')
    store.update(running['id'],status='running')
    engine.active.add(running['id'])
    class OneTick:
        ticks=iter([False,True])
        def wait(self,seconds): return next(self.ticks)
    engine.stop_event=OneTick()
    monkeypatch.setattr(engine_module,'settings',lambda:{'queue_paused':True,'parallel_agents':4})
    monkeypatch.setattr(engine.queue_intake,'check',lambda *a:pytest.fail('Paused queue must not run intake or models'))
    monkeypatch.setattr(engine_module,'deliver_waiting',lambda *a:pytest.fail('Paused queue must not dispatch conversation updates'))
    engine.loop()
    assert store.job(queued['id'])['status']=='queued'
    assert store.job(running['id'])['status']=='running'
    assert running['id'] in engine.active
    engine.pool.shutdown(wait=False)


def test_queue_pause_setting_survives_restart_and_validates_type(tmp_path,monkeypatch):
    monkeypatch.setattr(config,'ROOT',tmp_path)
    config.save_settings({'queue_paused':True,'parallel_agents':4})
    assert config.settings()['queue_paused'] is True
    assert config.settings()['parallel_agents']==4
    with pytest.raises(ValueError,match='Queue paused'):
        config.save_settings({'queue_paused':'false'})
    assert config.settings()['queue_paused'] is True
    config.save_settings({'queue_paused':False})
    assert config.settings()['queue_paused'] is False
