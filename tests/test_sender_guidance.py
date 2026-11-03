"""Sender rules require exact source identities and never broaden legacy scopes."""
import json
import pytest
from cos.db import Store
from cos.knowledge import Knowledge
from cos.conversations import save_source, cache_source_read
from cos.guidance_targets import candidates, sender_identity, targets
from cos.reviews import submit
from cos.engine import Engine
from cos.intake import EmailIntake

CONTACT='8:orgid:11111111-2222-3333-4444-555555555555'
OTHER='8:orgid:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'

@pytest.fixture
def store(tmp_path):
    return Store(tmp_path/'test.sqlite3')


def job(store,source='email',identity='alerts@example.test',thread='one',label=None):
    j=store.create_job('Review update','Communications')
    store.update(j['id'],status='completed')
    store.write('INSERT INTO communication_threads VALUES(?,?,?)',(source,thread,j['id']))
    with store.connect() as c:
        save_source(c,j['id'],source,'message',label or identity,'Update','2026-11-01','Please review',sender_id=identity)
    return store.job(j['id'])


def rule(store,target,user=True,source_job=None):
    return Knowledge(store).save({'title':'Notification handling','kind':'preference',
        'content':'Ignore routine notifications unless they assign me work.','target':target},user=user,job=source_job)


def test_sender_binding_crosses_conversations_but_not_identity_or_source(store):
    first=job(store); second=job(store,thread='two'); other=job(store,identity='other@example.test',thread='three')
    teams=job(store,'teams',CONTACT,'four',label='alerts@example.test')
    saved=rule(store,{'source':'email','sender_id':'ALERTS@example.test'})
    k=Knowledge(store)
    assert saved['id'] in {r['id'] for r in k.search('',job=first)}
    assert saved['id'] in {r['id'] for r in k.search('',job=second)}
    assert not k.search('',job=other) and not k.search('',job=teams) and not k.search('')
    assert k.search('',include_targeted=True)
    assert json.loads(k.context(second))[0]['target']['sender_id']=='alerts@example.test'


def test_display_name_and_quoted_address_cannot_establish_teams_identity(store):
    j=job(store,'teams',CONTACT,label='Shared display name')
    cache_source_read(store,j['id'],'read_teams',{'chat_id':'one'},[
        {'id':'message','from':'https://service.test/contacts/'+CONTACT,'imdisplayname':'Changed name','content':'Updated body'}])
    assert store.detail(j['id'])['sender_candidates']==[{'source':'teams','sender_id':CONTACT,'label':'Changed name'}]
    other=job(store,'teams',OTHER,'two',label='Changed name')
    saved=rule(store,{'source':'teams','sender_id':CONTACT})
    assert Knowledge(store).search('',job=j)[0]['id']==saved['id']
    assert not Knowledge(store).search('',job=other)
    legacy=store.create_job('Legacy','Communications')
    with store.connect() as c:
        save_source(c,legacy['id'],'teams','old',CONTACT,'','','Quoted '+CONTACT)
        assert candidates(c,legacy['id'])==[]


def test_owner_selects_recorded_identity_and_editing_preserves_binding(store):
    j=job(store,'teams',CONTACT,label='Participant')
    chosen={'source':'teams','sender_id':CONTACT}
    result=submit(Engine(store),j['id'],'Ignore routine updates',remember=True,
        applies_to='Routine updates, except requests',remember_target='sender',remember_sender=chosen)
    with store.connect() as c: assert targets(c)[result['memory_id']]==chosen
    k=Knowledge(store)
    k.save({'title':'Corrected','kind':'preference','content':'Ignore only automated updates'},user=True,id=result['memory_id'])
    with store.connect() as c: assert targets(c)[result['memory_id']]==chosen
    k.save({'title':'Corrected','kind':'preference','content':'Ignore only automated updates',
        'target':{'source':'teams','thread_id':'one'}},user=True,id=result['memory_id'])
    assert not store.rows('SELECT * FROM knowledge_sender_targets')
    with store.connect() as c: assert targets(c)[result['memory_id']]=={'source':'teams','thread_id':'one'}
    k.forget(result['memory_id']);assert not k.search('',job=j)


@pytest.mark.parametrize('selection',[None,{}, {'source':'teams','sender_id':OTHER},
    {'source':'teams','sender_id':'Participant'}, {'source':'email','sender_id':'stranger@example.test'}])
def test_review_cannot_bind_guessed_or_unrelated_senders(store,selection):
    j=job(store,'teams',CONTACT,label='Participant')
    with pytest.raises(ValueError):
        submit(Engine(store),j['id'],'Ignore',remember=True,applies_to='Routine',remember_target='sender',remember_sender=selection)
    assert not store.rows('SELECT * FROM knowledge')
    assert not store.rows('SELECT * FROM review_messages')
    assert store.job(j['id'])['status']=='completed'


def test_legacy_scope_and_unapproved_suggestions_are_unchanged(store):
    j=job(store)
    saved=rule(store,{'source':'email','thread_id':'one'})
    suggested=rule(store,{'source':'email','sender_id':'alerts@example.test'},False,j)
    # Reopening storage applies only additive schema, without accepting suggestions.
    fresh=Store(store.path)
    with fresh.connect() as c: assert targets(c)[saved['id']]=={'source':'email','thread_id':'one'}
    assert fresh.one('SELECT status FROM knowledge WHERE id=?',(suggested['id'],))['status']=='suggested'
    assert suggested['id'] not in {r['id'] for r in Knowledge(fresh).search('',job=j)}


def email(identity):
    return {'id':identity,'subject':'Update','from':{'emailAddress':{'address':identity}},'bodyPreview':'FYI'}


def test_intake_enforces_sender_binding_even_when_model_claims_it_applies(store):
    saved=rule(store,{'source':'email','sender_id':'alerts@example.test'})
    class Provider:
        def complete(self,messages,**kwargs):
            return {'content':json.dumps({'decisions':[{'thread_id':tid,'ignore':True,
                'rule_ids':[saved['id']],'reason':'Routine'} for tid in ['same','other','mixed']]})},{}
    result=EmailIntake(store,Provider()).classify({'same':[email('ALERTS@example.test')],
        'other':[email('other@example.test')],
        'mixed':[email('alerts@example.test'),email('other@example.test')]})
    assert set(result)=={'same'}


def test_source_metadata_is_captured_by_monitor(store):
    from cos.monitor import Monitor
    monitor=Monitor(store)
    monitor.enqueue_teams_topic({'chat_id':'chat','topic_id':'teams-topic:one','title':'Topic','messages':[
        {'id':'one','owner':False,'date':'2026-11-01','text':'Update','sender':'Participant','sender_id':CONTACT}]})
    j=store.one('SELECT id FROM jobs')
    assert store.detail(j['id'])['sender_candidates'][0]['sender_id']==CONTACT


def test_sender_api_auth_csrf_edit_and_selection(store,monkeypatch):
    from fastapi.testclient import TestClient
    from cos import server
    monkeypatch.setattr(server,'store',store);monkeypatch.setattr(server,'engine',Engine(store))
    j=job(store)
    client=TestClient(server.app)
    assert client.get('/api/knowledge').status_code==401
    client.get('/')
    payload={'message':'Ignore routine notices','remember':True,'remember_target':'sender',
        'remember_sender':{'source':'email','sender_id':'alerts@example.test'},'applies_to':'Routine, except requests'}
    route='/api/jobs/'+j['id']+'/review'
    assert client.post(route,json=payload).status_code==403
    headers={'X-COS-Token':server.TOKEN}
    result=client.post(route,json=payload,headers=headers)
    assert result.status_code==200
    saved=client.get('/api/knowledge').json()[0]
    assert saved['target']==payload['remember_sender']
    entry={'title':saved['title'],'kind':'preference','content':'Keep exceptions','target':{}}
    assert client.post('/api/knowledge/'+saved['id'],json={'action':'edit','entry':entry},headers=headers).status_code==200
    assert client.get('/api/knowledge').json()[0]['target']=={}


@pytest.mark.parametrize('target',[{'source':[],'sender_id':'alerts@example.test'},
    {'source':'email','sender_id':'x'*321+'@example.test'},
    {'source':'email','sender_id':'alerts@example.test','thread_id':'one'},
    {'source':'teams','sender_id':'Display Name'}])
def test_invalid_or_ambiguous_bindings_are_rejected(store,target):
    with pytest.raises(ValueError): rule(store,target)
    assert not store.rows('SELECT * FROM knowledge')
