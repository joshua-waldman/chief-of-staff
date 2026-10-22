import json
import threading
import pytest
from cos.db import Store
from cos.engine import Engine,session
from cos.reviews import submit
from cos.knowledge import Knowledge
from test_workflows import Scripted,perform

@pytest.fixture
def setup(tmp_path):
    store=Store(tmp_path/'reviews.sqlite3')
    engine=Engine(store,Scripted(perform('send_email',{'to':'colleague@amd.com','subject':'Scope','body':'Original draft'})))
    job=store.create_job('Prepare a reply','Communications')
    engine.run(job['id'])
    approval=store.one("SELECT * FROM approvals WHERE job_id=? AND status='pending'",(job['id'],))
    yield store,engine,job,approval
    engine.stop()

def test_revision_retires_approval_and_requires_fresh_send_approval(setup,monkeypatch):
    store,e,j,a=setup
    sent=[]
    monkeypatch.setattr(e.tools,'execute',lambda job,action,args:sent.append(args) or {'sent':True})
    submit(e,j['id'],'Make it shorter and ask for the deadline.',draft='My edited unsent draft')
    assert store.one('SELECT status FROM approvals WHERE id=?',(a['id'],))['status']=='superseded'
    with pytest.raises(ValueError):
        e.decide(a['id'],True)
    assert not sent
    assert 'My edited unsent draft' in store.job(j['id'])['messages']
    assert store.job(j['id'])['status']=='queued'
    assert not store.rows("SELECT * FROM knowledge WHERE kind='preference'")
    e.provider=Scripted(perform('send_email',{'to':'colleague@amd.com','subject':'Scope','body':'What is the deadline?'}))
    e.run(j['id'])
    newer=store.one("SELECT * FROM approvals WHERE job_id=? AND status='pending'",(j['id'],))
    assert newer['id']!=a['id']
    assert json.loads(newer['args'])['body']=='What is the deadline?'
    assert store.job(j['id'])['status']=='awaiting_approval'
    assert not sent
    chat=store.detail(j['id'])['review_messages']
    assert [x['role'] for x in chat]==['user','assistant']

def test_explicit_reusable_guidance_is_source_linked_and_retrieved(setup):
    s,e,j,a=setup
    result=submit(e,j['id'],'No response needed.',True,'Close informational notifications unless I am assigned an action.','Automated Jira owner-change notices')
    memory=s.one('SELECT * FROM knowledge WHERE id=?',(result['memory_id'],))
    assert memory['status']=='active' and memory['source']=='user'
    assert memory['source_job']==j['id'] and memory['scope']=='Communications'
    assert 'Automated Jira' in memory['content']
    assert 'Close informational' in Knowledge(s).context({**j,'request':'A new message','project':'Example Project'})
    assert len(s.rows('SELECT * FROM jobs'))==1
    # Persistence is independent of a running engine.
    assert Store(s.path).detail(j['id'])['review_messages'][0]['body']=='No response needed.'

def test_invalid_feedback_is_atomic_and_does_not_retire_approval(setup):
    s,e,j,a=setup
    with pytest.raises(ValueError):
        submit(e,j['id'],'Remember this',True,applies_to='')
    assert s.one('SELECT status FROM approvals WHERE id=?',(a['id'],))['status']=='pending'
    assert not s.rows('SELECT * FROM review_messages')
    assert not s.rows('SELECT * FROM knowledge')
    s.write("UPDATE approvals SET status='uncertain' WHERE id=?",(a['id'],))
    with pytest.raises(ValueError):
        submit(e,j['id'],'Try again')
    assert not s.rows('SELECT * FROM review_messages')

def test_feedback_reopens_completed_job_with_balanced_tool_history(setup):
    s,e,j,a=setup
    e.decide(a['id'],False)
    e.provider=Scripted(perform('finish',{'summary':'Closed without sending.'}))
    e.run(j['id'])
    submit(e,j['id'],'Explain why a response is unnecessary.')
    messages=session(s.job(j['id']))['messages']
    terminal=next(i for i,m in enumerate(messages) if m.get('tool_calls') and json.loads(m['tool_calls'][0]['function']['arguments'])['action']=='finish')
    assert messages[terminal+1]['role']=='tool'
    assert messages[-1]['role']=='user'
    assert s.job(j['id'])['status']=='queued'

def test_review_and_send_race_has_only_one_winner(setup,monkeypatch):
    s,e,j,a=setup
    sent=[]
    monkeypatch.setattr(e.tools,'execute',lambda *args:sent.append(True) or {'sent':True})
    barrier=threading.Barrier(2)
    errors=[]
    def revise():
        barrier.wait()
        try:submit(e,j['id'],'Do not send this; revise it.')
        except ValueError:errors.append('review')
    def approve():
        barrier.wait()
        try:e.decide(a['id'],True)
        except ValueError:errors.append('approve')
    threads=[threading.Thread(target=revise),threading.Thread(target=approve)]
    for t in threads:t.start()
    for t in threads:t.join(10)
    assert not any(t.is_alive() for t in threads)
    final=s.one('SELECT status FROM approvals WHERE id=?',(a['id'],))['status']
    assert (final=='superseded' and not sent and errors==['approve']) or (final=='executed' and sent==[True] and errors==['review'])

def test_review_api_session_csrf_and_scope(setup,monkeypatch):
    from fastapi.testclient import TestClient
    from cos import server
    s,e,j,a=setup
    monkeypatch.setattr(server,'store',s);monkeypatch.setattr(server,'engine',e)
    c=TestClient(server.app)
    path='/api/jobs/'+j['id']+'/review'
    assert c.post(path,json={'message':'Shorter'}).status_code==401
    c.get('/')
    assert c.post(path,json={'message':'Shorter'}).status_code==403
    headers={'x-cos-token':server.TOKEN}
    assert c.post(path,json={'message':'Shorter'},headers=headers).status_code==200
    other=s.create_job('Code','Engineering')
    assert c.post('/api/jobs/'+other['id']+'/review',json={'message':'Shorter'},headers=headers).status_code==400
