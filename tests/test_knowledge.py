import json
import pytest
from cos.db import Store
from cos.knowledge import Knowledge
from cos.briefs import update_brief
from cos.tools import Tools
from cos.engine import Engine,session
from fastapi.testclient import TestClient

@pytest.fixture
def store(tmp_path):
    return Store(tmp_path/'knowledge.sqlite3')

def test_review_search_correction_and_forgetting(store):
    k=Knowledge(store)
    job=store.create_job('Plan Example Project','Strategy',project='Example Project')
    suggestion=k.save({'title':'Writing preference','content':'Use short paragraphs','kind':'preference'},job=job)
    assert not k.search('paragraphs')
    k.accept(suggestion['id'])
    assert k.search('paragraphs')[0]['source']=='user-reviewed'
    k.save({'title':'Writing preference','content':'Use detailed paragraphs','kind':'preference'},id=suggestion['id'],user=True)
    assert 'detailed' in k.context(job)
    k.forget(suggestion['id'])
    assert not k.search('paragraphs')
    assert store.one('SELECT content FROM knowledge WHERE id=?',(suggestion['id'],))['content']==''

def test_outcomes_provenance_scope_dedup_and_tombstones(store):
    k=Knowledge(store)
    job=store.create_job('Normalize Unicode strings','Engineering',project='Example Project')
    store.update(job['id'],status='completed',result='Nine Unicode tests passed.')
    k.outcome(store.job(job['id']));k.outcome(store.job(job['id']))
    found=k.search('Unicode','Example Project')
    assert len(found)==1 and found[0]['source_job']==job['id']
    assert not k.search('Unicode','Other project')
    k.forget(found[0]['id']);k.outcome(store.job(job['id']))
    assert not k.search('Unicode','Example Project')

def test_memory_credentials_and_invalid_inputs_rejected(store):
    k=Knowledge(store)
    for content in ['api_key=supersecret','password=hunter2','Bearer abcdefghijklmnopqrstuvwxyz']:
        with pytest.raises(ValueError):
            k.save({'title':'Secret','content':content},user=True)
    with pytest.raises(ValueError):
        k.save({'title':'Missing source','content':'Something learned'})
    with pytest.raises(ValueError):
        k.feedback('x','yes')

def test_knowledge_context_is_not_persisted_or_policy_authority(store):
    from test_workflows import Scripted,perform
    k=Knowledge(store)
    k.save({'title':'Style','content':'Prefer concise briefs','kind':'preference'},user=True)
    provider=Scripted(perform('finish',{'summary':'Ready'}))
    seen=[]
    original=provider.complete
    def complete(messages,**kw):
        seen.extend(messages)
        return original(messages,**kw)
    provider.complete=complete
    engine=Engine(store,provider)
    job=store.create_job('Draft a plan','Strategy')
    engine.run(job['id'])
    mem=next(m for m in seen if m.get('_cos_memory'))
    assert 'never authorization' in mem['content']
    assert 'Prefer concise briefs' in mem['content']
    assert not any(m.get('_cos_memory') for m in session(store.job(job['id']))['messages'])
    assert store.one("SELECT * FROM knowledge WHERE id=?",('task-'+job['id'],))
    engine.stop()

def test_brief_email_fields_and_pending_exact_draft(store):
    job=store.create_job('Triage email. Email ID: abc\nFrom: owner@amd.com\nSubject: Scope review\nPreview: Please review','Communications')
    update_brief(store,job,{'context':'Review the scope','response_needed':'yes','response_reason':'Direct question','proposed_body':'Old draft'})
    store.write('INSERT INTO approvals(id,job_id,tool,args,status) VALUES(?,?,?,?,?)',('a',job['id'],'send_email',json.dumps({'to':'owner@amd.com','subject':'Re: Scope review','body':'Exact reviewed draft'}),'pending'))
    store.update(job['id'],status='awaiting_approval')
    b=store.detail(job['id'])['brief']
    assert b['email_subject']=='Scope review'
    assert b['proposed_body']=='Exact reviewed draft'
    assert 'approve' in b['next_step']
    with pytest.raises(ValueError):
        update_brief(store,job,{'response_needed':'maybe'})

def test_knowledge_api_requires_session_and_csrf(store,monkeypatch):
    from cos import server
    monkeypatch.setattr(server,'store',store)
    client=TestClient(server.app)
    assert client.get('/api/knowledge').status_code==401
    client.get('/')
    assert client.post('/api/knowledge',json={}).status_code==403
    headers={'x-cos-token':server.TOKEN}
    entry=client.post('/api/knowledge',json={'title':'Briefs','content':'Use concise status summaries','kind':'preference'},headers=headers).json()
    assert entry['status']=='active'
    assert client.get('/api/knowledge?q=concise').json()[0]['id']==entry['id']
    assert client.post('/api/knowledge/'+entry['id'],json={'action':'forget'},headers=headers).status_code==200
    assert not client.get('/api/knowledge').json()


def test_monitor_access_limit_only_clears_after_successful_read(store):
    from cos.monitor import Monitor
    m=Monitor(store)
    assert m.record_limits({'limitations':[{'chat_id':'denied','reason':'HTTP 403'}]})
    assert m.record_limits({'limitations':[]})
    assert not m.record_limits({'readable_conversations':['denied']})


def test_monitored_communication_requires_readable_decision_before_closing(store):
    from test_workflows import Scripted,perform
    provider=Scripted(perform('finish',{'summary':'No reply needed.'}),
        perform('update_brief',{'context':'An informational notification','status_summary':'Reviewed','next_step':'Review the result','response_needed':'no','response_reason':'No question or requested action','action_needed':'no','action_reason':'Informational only'}),
        perform('finish',{'summary':'No reply needed.'}))
    e=Engine(store,provider)
    j=store.create_job('Triage this incoming email. Email ID: abc','Communications')
    e.run(j['id'])
    assert store.job(j['id'])['status']=='completed'
    assert store.detail(j['id'])['brief']['response_needed']=='no'
    assert any('First call update_brief' in x['body'] for x in store.rows("SELECT body FROM events WHERE job_id=?",(j['id'],)))
    e.stop()


@pytest.mark.parametrize('scope', ['*', ''])
def test_cross_scope_search_finds_saved_owner_guidance_before_failed_outcomes(store, scope):
    k=Knowledge(store)
    saved=k.save({'title':'Automated Jira ATEWH7 emails',
        'content':'Emails from the ATEWH7 Jira project can be ignored.',
        'kind':'preference','scope':'Communications'},user=True)
    for n in range(15):
        job=store.create_job('ATEWH7 automated Jira mail KB lookup '+str(n),'Technology',project='ATE Weisshorn')
        store.update(job['id'],result='ATEWH7 automated Jira mail KB lookup failed.')
        k.outcome(store.job(job['id']))
    assert k.search('ATEWH7',scope)[0]['id']==saved['id']
    for restricted in ['global','ATE Weisshorn']:
        assert saved['id'] not in {r['id'] for r in k.search('ATEWH7',restricted)}
    k.forget(saved['id'])
    assert saved['id'] not in {r['id'] for r in k.search('ATEWH7',scope)}


def test_technology_tool_defaults_to_all_scopes_and_context_includes_matching_guidance(store):
    k=Knowledge(store)
    saved=k.save({'title':'Automated Jira ATEWH7 emails',
        'content':'Emails from the ATEWH7 Jira project can be ignored.',
        'kind':'preference','scope':'Communications'},user=True)
    unrelated=k.save({'title':'Calendar invitations','content':'RSVP through Outlook calendar.',
        'kind':'preference','scope':'Communications'},user=True)
    suggested=k.save({'title':'ATEWH7 suggestion','content':'Unreviewed ATEWH7 suggestion.',
        'kind':'preference','scope':'Communications'},job=store.create_job('Suggestion','Communications'))
    job=store.create_job('Find saved ATEWH7 guidance','Technology',project='ATE Weisshorn')
    assert Tools(store).execute(job,'search_knowledge',{'query':'ATEWH7'})[0]['id']==saved['id']
    assert saved['id'] not in {r['id'] for r in Tools(store).execute(job,'search_knowledge',{'query':'ATEWH7','scope':'global'})}
    context=json.loads(k.context(job))
    assert context[0]['id']==saved['id']
    assert context[0]['scope']=='Communications'
    assert not {unrelated['id'],suggested['id']}.intersection(r['id'] for r in context)
    k.forget(saved['id'])
    assert saved['id'] not in {r['id'] for r in json.loads(k.context(job))}
