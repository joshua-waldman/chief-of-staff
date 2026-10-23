import json

import pytest

from cos.briefs import update_brief
from cos.conversations import save_source
from cos.db import Store
from cos.engine import Engine
from cos.message_triage import category, first_pass


@pytest.mark.parametrize('response,action,requested,expected', [
    ('no','yes','ignore','action'),
    ('yes','yes','reading','response_action'),
    ('yes','no','ignore','response'),
    ('no','unknown','ignore','review'),
    ('unknown','no','reading','review'),
    ('no','no','ignore','ignore'),
    ('no','no','reading','reading'),
    ('no','no','review','review'),
])
def test_category_keeps_work_and_uncertainty_visible(response,action,requested,expected):
    assert category(dict(response_needed=response,action_needed=action,triage_category=requested)) == expected


def test_incomplete_body_cannot_be_dismissed(tmp_path):
    store=Store(tmp_path/'triage.db')
    job=store.create_job('Triage this incoming email conversation.','Communications')
    with store.connect() as c:
        save_source(c,job['id'],'email','mail','sender@example.com','Subject','','Preview only',False)
    brief=update_brief(store,job,dict(response_needed='no',action_needed='no',triage_category='ignore'))
    assert brief['triage_category']=='review'
    assert brief['action_needed']==brief['response_needed']=='unknown'
    with pytest.raises(ValueError,match='Unknown triage'):
        update_brief(store,job,dict(triage_category='delete'))


@pytest.mark.parametrize('older_request',[False,True])
def test_finish_resolves_older_previews_before_deciding_attention(tmp_path,monkeypatch,older_request):
    from cos import tools
    store=Store(tmp_path/'older.db')
    job=store.create_job('Triage this incoming email conversation.','Communications')
    with store.connect() as c:
        save_source(c,job['id'],'email','old','sender@example.com','Update','2026-01-01','Preview only',False)
        save_source(c,job['id'],'email','latest','sender@example.com','Update','2026-01-02','Automated owner field change.',True)
    decision=dict(context='Automated alert',status_summary='No follow-up needed',next_step='None',
                  response_needed='no',response_reason='No reply requested',action_needed='no',
                  action_reason='No owner work',requested_action='None.',triage_category='ignore')
    # Reproduce the persisted mismatch: the model said no/no but the guard
    # changed it to unknown because a historical source was still a preview.
    update_brief(store,job,decision)
    reads=[]
    def read(*args):
        reads.append(args)
        return {'body':'Please investigate the failure.' if older_request else 'Automated owner field change.'}
    monkeypatch.setattr(tools,'run_skill',read)
    class Provider:
        def __init__(self): self.calls=0
        def complete(self,messages,**kwargs):
            self.calls+=1
            if self.calls==1:
                return perform('finish',{'summary':'No follow-up needed.'}),{}
            if self.calls==2:
                error=json.loads(messages[-1]['content'])['error']
                assert 'source bodies remain unread' in error
                assert 'old' in error
                assert store.job(job['id'])['status']=='running'
                return perform('read_mail',{'id':'old'}),{}
            if self.calls==3:
                revised={**decision}
                if older_request:
                    revised.update(action_needed='yes',action_reason='Older email requests an investigation',
                                   requested_action='Investigate the failure',triage_category='action')
                return perform('update_brief',revised),{}
            return perform('finish',{'summary':'Investigation remains.' if older_request else 'No follow-up needed.'}),{}
    engine=Engine(store,Provider())
    try: engine.run(job['id'])
    finally: engine.pool.shutdown()
    result=store.job(job['id'])
    assert len(reads)==1
    assert result['status']==('waiting_input' if older_request else 'completed')
    assert result['question']==('Follow-up remains: Investigate the failure' if older_request else '')
    assert not store.rows("SELECT * FROM review_messages WHERE body LIKE '%Follow-up remains: None%'")


@pytest.mark.parametrize('action,requested,expected',[
    ('unknown','None.','Full message text is needed'),
    ('yes','None outstanding.','Investigate the failure'),
    ('yes','Investigate the failure','Investigate the failure'),
])
def test_unresolved_action_describes_evidence_gap_or_real_work(tmp_path,action,requested,expected):
    from cos.briefs import unresolved_action
    store=Store(tmp_path/'followup.db')
    job=store.create_job('Triage this incoming email conversation.','Communications')
    update_brief(store,job,dict(action_needed=action,requested_action=requested,
                               action_reason='Investigate the failure',triage_reason='Full message text is needed'))
    assert unresolved_action(store,job)==expected


def test_compact_pass_uses_full_body_and_leaves_resumed_work_intact(tmp_path):
    store=Store(tmp_path/'triage.db')
    job=store.create_job('Triage this incoming email conversation. Email ID: mail','Communications')
    with store.connect() as c:
        save_source(c,job['id'],'email','mail','sender@example.com','Subject','','Full body: final paragraph',True)
    prompt=first_pass(store,job,{'messages':[]})
    evidence=json.loads(prompt[1]['content'])
    assert evidence['owner_email']=='owner@example.com'
    assert evidence['sources'][0]['body']=='Full body: final paragraph'
    assert evidence['sources'][0]['complete']==1
    assert first_pass(store,job,{'source_revision':1}) is None
    assert first_pass(store,{**job,'step':2},{}) is None
    with store.connect() as c:
        save_source(c,job['id'],'email','mail','sender@example.com','Subject','','x'*61000,True)
    assert first_pass(store,job,{}) is None


def perform(action,args):
    return {'role':'assistant','content':None,'tool_calls':[{'id':action,'type':'function','function':{'name':'perform','arguments':json.dumps({'action':action,'arguments':json.dumps(args)})}}]}


def test_engine_records_initial_decision_then_finishes_informational_message(tmp_path):
    store=Store(tmp_path/'triage.db')
    job=store.create_job('Triage this incoming Teams conversation. Chat ID: chat','Communications')
    with store.connect() as c:
        save_source(c,job['id'],'teams','message','Participant','','','The release shipped today.',True)
    class Provider:
        def __init__(self):
            self.calls=[]
        def complete(self,messages,**kwargs):
            self.calls.append(messages.copy())
            if len(self.calls)==1:
                assert 'Tool catalog:' not in messages[0]['content']
                assert 'The release shipped today.' in messages[1]['content']
                return perform('update_brief',dict(context='Release update',status_summary='Release shipped',next_step='Read summary',email_excerpt='The release shipped today.',response_needed='no',response_reason='Informational',action_needed='no',action_reason='No work requested',triage_category='reading',triage_reason='Informational release update')),{}
            return perform('finish',{'summary':'The release shipped today.'}),{}
    provider=Provider()
    engine=Engine(store,provider)
    try:
        engine.run(job['id'])
    finally:
        engine.pool.shutdown()
    detail=store.detail(job['id'])
    assert detail['status']=='completed'
    assert detail['brief']['triage_category']=='reading'
    assert len(provider.calls)==2
    assert 'Tool catalog:' in provider.calls[1][0]['content']
    assert not detail['children']
    assert not [a for a in detail['approvals'] if a['tool'].startswith('send')]


def test_queue_api_returns_decisions_and_summaries_without_source_bodies(tmp_path,monkeypatch):
    from cos import server
    store=Store(tmp_path/'triage.db')
    monkeypatch.setattr(server,'store',store)
    job=store.create_job('Triage this incoming email conversation.','Communications')
    update_brief(store,job,dict(response_needed='no',action_needed='yes',triage_category='action',email_excerpt='Investigate the release failure.'))
    row=server.jobs()[0]
    assert row['triage_category']=='action'
    assert row['triage_summary']=='Investigate the release failure.'
    assert 'brief_body' not in row
    assert 'request' not in row


def test_email_preview_is_read_before_body_decision(tmp_path,monkeypatch):
    from cos import tools
    store=Store(tmp_path/'email.db')
    job=store.create_job('Triage this incoming email conversation.\nEmail ID: mail\nPreview: Release update','Communications')
    with store.connect() as c:
        save_source(c,job['id'],'email','mail','sender@example.com','Release','','Release update',False)
    reads=[]
    def read(*args):
        reads.append(args)
        return {'body':'The release shipped. No follow-up requested.','from':'sender@example.com','subject':'Release'}
    monkeypatch.setattr(tools,'run_skill',read)
    class Provider:
        def __init__(self):
            self.calls=0
        def complete(self,messages,**kwargs):
            self.calls+=1
            if self.calls==1:
                evidence=json.loads(messages[1]['content'])
                assert not evidence['sources'][0]['complete']
                return perform('read_mail',{'id':'mail'}),{}
            if self.calls==2:
                evidence=json.loads(messages[1]['content'])
                assert evidence['sources'][0]['complete']
                assert 'No follow-up requested.' in evidence['sources'][0]['body']
                return perform('update_brief',dict(context='Release',status_summary='Shipped',next_step='Read summary',email_excerpt='The release shipped.',response_needed='no',response_reason='No reply requested',action_needed='no',action_reason='No follow-up requested',triage_category='reading')),{}
            return perform('finish',{'summary':'The release shipped.'}),{}
    provider=Provider()
    engine=Engine(store,provider)
    try:
        engine.run(job['id'])
    finally:
        engine.pool.shutdown()
    assert len(reads)==1
    assert store.detail(job['id'])['brief']['triage_category']=='reading'
    assert store.job(job['id'])['status']=='completed'


def informational(store):
    job=store.create_job('Triage this incoming email conversation.', 'Communications')
    with store.connect() as c:
        save_source(c,job['id'],'email','mail','sender@example.com','FYI','','Alex owns the next step. You are copied for awareness.',True)
    update_brief(store,job,dict(context='FYI',status_summary='Summary ready',next_step='No action',email_excerpt='Alex owns the next step.',response_needed='no',response_reason='FYI; no owner reply requested',action_needed='no',action_reason='Alex is assigned the work',triage_category='reading'))
    store.update(job['id'],status='completed',result='Alex owns the next step.')
    return store.job(job['id'])


@pytest.mark.parametrize('approved',[True,False])
def test_optional_read_finishes_without_another_model_pass(tmp_path,approved):
    from cos.message_triage import read_targets,request_read
    store=Store(tmp_path/'read.db')
    job=informational(store)
    targets=read_targets(store,job)
    assert len(targets)==1
    assert targets[0]['args']['id']=='mail'
    engine=Engine(store)
    executed=[]
    engine.tools.execute=lambda job,tool,args: executed.append((tool,args)) or {'ok':True}
    try:
        result=request_read(store,job['id'],'mark_email_read','mail')
        assert store.job(job['id'])['status']=='awaiting_approval'
        assert not executed
        engine.decide(result['approval_id'],approved)
        assert store.job(job['id'])['status']=='completed'
        assert len(executed)==int(approved)
        assert store.job(job['id'])['result']=='Alex owns the next step.'
        assert not store.detail(job['id'])['children']
        assert bool(read_targets(store,store.job(job['id']))) is not approved
    finally:
        engine.pool.shutdown()


def test_summary_read_rejects_other_sources_and_new_activity(tmp_path):
    from cos.message_triage import request_read
    store=Store(tmp_path/'read.db')
    job=informational(store)
    with pytest.raises(ValueError):
        request_read(store,job['id'],'mark_email_read','another-mail')
    store.write('INSERT INTO communication_inbox(job_id,body,created) VALUES(?,?,?)',(job['id'],'New request','now'))
    with pytest.raises(ValueError):
        request_read(store,job['id'],'mark_email_read','mail')
    assert not store.detail(job['id'])['approvals']


def test_no_personal_obligation_clears_old_proposals(tmp_path):
    store=Store(tmp_path/'read.db')
    job=informational(store)
    brief=update_brief(store,job,dict(requested_action='Investigate',action_plan='Delegate',proposed_body='Thanks!',next_step='Ask the owner what to do'))
    assert not any(brief.get(k) for k in ('requested_action','action_plan','proposed_body'))
    assert brief['next_step'].startswith('No further action required.')


@pytest.mark.parametrize('action,args',[
    ('delegate',{'tasks':[{'org':'Technology','request':'Investigate Alexs work'}]}),
    ('reply_email',{'id':'mail','body':'Thanks!'}),
])
def test_informational_triage_blocks_unnecessary_work_and_replies(tmp_path,action,args):
    store=Store(tmp_path/'guard.db')
    job=informational(store)
    store.update(job['id'],status='queued')
    class Provider:
        def __init__(self): self.calls=0
        def complete(self,messages,**kwargs):
            self.calls+=1
            if self.calls==1: return perform(action,args),{}
            return perform('finish',{'summary':'Alex owns the next step.'}),{}
    engine=Engine(store,Provider())
    try: engine.run(job['id'])
    finally: engine.pool.shutdown()
    detail=store.detail(job['id'])
    assert detail['status']=='completed'
    assert not detail['approvals']
    assert not detail['children']
    assert any(e['kind']=='tool_error' for e in detail['events'])


def test_teams_summary_read_targets_the_entire_bound_chat(tmp_path):
    from cos.message_triage import read_targets
    store=Store(tmp_path/'teams.db')
    job=informational(store)
    store.write('DELETE FROM communication_sources WHERE job_id=?',(job['id'],))
    with store.connect() as c:
        save_source(c,job['id'],'teams','message','Participant','','','FYI',True)
        c.execute('INSERT INTO communication_threads VALUES(?,?,?)',('teams','chat',job['id']))
    targets=read_targets(store,job)
    assert targets==[{'tool':'mark_teams_read','args':{'chat_id':'chat'},'label':'Mark entire Teams chat as read'}]


def test_summary_read_http_requires_explicit_source_approval(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from cos import server
    store=Store(tmp_path/'http.db')
    job=informational(store)
    monkeypatch.setattr(server,'store',store)
    client=TestClient(server.app)
    client.cookies.set('cos_session',server.TOKEN)
    url='/api/jobs/'+job['id']+'/mark-read'
    body={'tool':'mark_email_read','target_id':'mail'}
    assert client.post(url,json=body).status_code==403
    response=client.get('/api/jobs/'+job['id']).json()
    assert response['read_targets'][0]['args']['id']=='mail'
    response=client.post(url,json=body,headers={'x-cos-token':server.TOKEN})
    assert response.status_code==200
    approval=store.one('SELECT * FROM approvals WHERE id=?',(response.json()['approval_id'],))
    assert approval['status']=='pending'
    assert approval['tool']=='mark_email_read'
    assert client.post(url,json=body,headers={'x-cos-token':server.TOKEN}).status_code==400
