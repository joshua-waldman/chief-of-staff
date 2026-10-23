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


def test_compact_pass_uses_full_body_and_leaves_resumed_work_intact(tmp_path):
    store=Store(tmp_path/'triage.db')
    job=store.create_job('Triage this incoming email conversation. Email ID: mail','Communications')
    with store.connect() as c:
        save_source(c,job['id'],'email','mail','sender@example.com','Subject','','Full body: final paragraph',True)
    prompt=first_pass(store,job,{'messages':[]})
    evidence=json.loads(prompt[1]['content'])
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
