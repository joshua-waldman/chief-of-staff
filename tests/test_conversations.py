import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from cos.db import Store
from cos.monitor import Monitor
from cos.engine import Engine, session
from cos.conversations import deliver, deliver_waiting, reconcile_legacy


def message(id, chat='chat', owner=False, text=None):
    return {'id':id, 'chat_id':chat, 'owner':owner, 'text':text or id, 'date':id}


def ingest(monitor, *messages, emails=()):
    return monitor.process({'messages':list(messages), 'emails':list(emails)})


def perform(action, args):
    return {'role':'assistant', 'content':None, 'tool_calls':[{'id':'call-'+action, 'type':'function', 'function':{'name':'perform', 'arguments':json.dumps({'action':action, 'arguments':args})}}]}


@pytest.fixture
def monitor(tmp_path,monkeypatch):
    from cos import conversations,tools
    monkeypatch.setattr(tools,'DATA',tmp_path/'data')
    original=conversations.settings
    monkeypatch.setattr(conversations,'settings',lambda:{**original(),'conversation_settle_seconds':0})
    return Monitor(Store(tmp_path/'conversations.db'))


def test_chat_burst_is_one_ordered_request_and_chats_stay_separate(monitor):
    assert ingest(monitor,message('03'),message('01'),message('02'),message('04','other')) == 2
    jobs = monitor.store.rows('SELECT * FROM jobs ORDER BY created')
    assert len(jobs) == 2
    request = jobs[0]['request']
    assert request.index('Message ID: 01') < request.index('Message ID: 02') < request.index('Message ID: 03')
    assert ingest(monitor,message('01'),message('02')) == 0
    assert len(monitor.store.rows('SELECT * FROM jobs')) == 2


def test_later_activity_reuses_durable_job_after_restart(monitor):
    ingest(monitor,message('01'))
    job = monitor.store.one('SELECT * FROM jobs')
    monitor.store.update(job['id'],status='completed',result='No reply needed',messages=json.dumps({'messages':[{'role':'user','content':job['request']},perform('finish',{'summary':'No reply needed'})]}))
    restarted = Monitor(Store(monitor.store.path))
    ingest(restarted,message('02',text='Follow-up ask'))
    current = restarted.store.job(job['id'])
    assert current['status'] == 'queued'
    assert len(restarted.store.rows('SELECT * FROM jobs')) == 1
    messages = session(current)['messages']
    assert messages[-2]['tool_call_id'] == 'call-finish'
    assert 'Follow-up ask' in messages[-1]['content']


def test_owner_reply_updates_existing_chat_without_starting_owner_only_work(monitor):
    assert ingest(monitor,message('01',owner=True)) == 0
    assert ingest(monitor,message('02'),message('03',owner=True,text='Already handled')) == 1
    job = monitor.store.one('SELECT * FROM jobs')
    assert 'Already handled' in job['request']
    monitor.store.update(job['id'],status='waiting_input')
    ingest(monitor,message('04',owner=True,text='Sent the answer myself'))
    current = monitor.store.job(job['id'])
    assert current['status'] == 'queued'
    assert 'Sent the answer myself' in current['messages']


def test_email_conversation_identity_groups_replies_but_not_matching_subjects(monitor):
    def email(id, thread):
        return {'id':id,'conversationId':thread,'subject':'Same subject','from':{'emailAddress':{'address':'other@amd.com'}}}
    assert ingest(monitor,emails=[email('a','thread'),email('b','thread'),email('c','different')]) == 2
    assert ingest(monitor,emails=[email('d','thread')]) == 0
    assert len(monitor.store.rows('SELECT * FROM jobs')) == 2
    assert monitor.store.one('SELECT * FROM communication_threads WHERE thread_id=?',('thread',))


def test_concurrent_ingestion_never_duplicates_a_conversation(monitor):
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda id:ingest(monitor,message(id)),['01','02','03','01']))
    assert len(monitor.store.rows('SELECT * FROM jobs')) == 1
    assert len(monitor.store.rows("SELECT * FROM seen WHERE source='teams'")) == 3


def test_pending_send_is_superseded_and_cannot_be_approved(monitor,monkeypatch):
    store = monitor.store
    ingest(monitor,message('01'))
    job = store.one('SELECT * FROM jobs')
    store.update(job['id'],status='awaiting_approval',messages=json.dumps({'messages':[{'role':'user','content':'Original'},perform('send_teams',{})], 'waiting_call':'call-send_teams', 'waiting_action':'approval'}))
    store.write("INSERT INTO approvals(id,job_id,tool,args,status) VALUES(?,?,?,?,?)",('approval',job['id'],'send_teams','{}','pending'))
    ingest(monitor,message('02',text='Actually never mind'))
    engine = Engine(store)
    monkeypatch.setattr(engine.tools,'execute',lambda *a:pytest.fail('Outdated send must not execute'))
    with pytest.raises(ValueError):
        engine.decide('approval',True)
    assert store.one('SELECT status FROM approvals')['status'] == 'superseded'
    assert store.job(job['id'])['status'] == 'queued'
    assert 'Actually never mind' in store.job(job['id'])['messages']


@pytest.mark.parametrize('status',['executing','uncertain'])
def test_updates_wait_for_external_outcome_resolution(monitor,status):
    ingest(monitor,message('01'))
    store = monitor.store
    job = store.one('SELECT * FROM jobs')
    store.update(job['id'],status='blocked')
    store.write("INSERT INTO approvals(id,job_id,tool,args,status) VALUES(?,?,?,?,?)",('action',job['id'],'send_teams','{}',status))
    ingest(monitor,message('02'))
    assert store.job(job['id'])['status'] == 'blocked'
    assert store.one('SELECT * FROM communication_inbox')
    assert not deliver(store,job['id'])
    store.write("UPDATE approvals SET status='executed'")
    deliver_waiting(store)
    assert store.job(job['id'])['status'] == 'queued'
    assert not store.one('SELECT * FROM communication_inbox')


def test_activity_arriving_during_inference_discards_stale_response(monitor,monkeypatch):
    ingest(monitor,message('01'))
    job = monitor.store.one('SELECT * FROM jobs')
    calls=[]
    class Provider:
        def complete(self,messages,**kwargs):
            calls.append(json.dumps(messages))
            if len(calls)==1:
                ingest(monitor,message('02',text='New information'))
                return perform('send_teams',{'chat_id':'chat','body':'Stale answer'}),{}
            return perform('ask_user',{'question':'Clarify the new information?'}),{}
    # Brief enforcement is irrelevant to this scheduling test.
    monitor.store.write("UPDATE jobs SET request=?,org='Technology' WHERE id=?",('Conversation scheduling test',job['id']))
    engine=Engine(monitor.store,Provider())
    monkeypatch.setattr(engine.tools,'execute',lambda *a:pytest.fail('Stale action executed'))
    engine.run(job['id'])
    assert len(calls)==2
    assert 'New information' in calls[1]
    assert not monitor.store.rows('SELECT * FROM approvals')
    assert monitor.store.job(job['id'])['status']=='waiting_input'


def test_legacy_duplicates_consolidate_and_preserve_owner_direction(monitor):
    store=monitor.store
    jobs=[]
    for id in ['01','02','03']:
        request='Triage this incoming Teams message. Chat ID: legacy Message ID: '+id+'\nMessage '+id
        job=store.create_job(request,'Communications')
        store.update(job['id'],status='waiting_input')
        jobs.append(job)
    for job in jobs[:2]:
        store.write('INSERT INTO review_messages(job_id,role,body,created) VALUES(?,?,?,?)',(job['id'],'user','Owner direction '+job['id'],'now'))
    reconcile_legacy(store)
    deliver_waiting(store)
    active=store.rows("SELECT * FROM jobs WHERE status<>'cancelled'")
    assert len(active)==1
    assert active[0]['status']=='queued'
    assert 'Message 01' in active[0]['messages'] and 'Message 03' in active[0]['messages']
    assert 'Owner direction' in active[0]['messages']
    assert len(store.rows("SELECT * FROM events WHERE kind='consolidated'"))==2
    ingest(monitor,message('04',chat='legacy'))
    assert len(store.rows('SELECT * FROM jobs'))==3
    assert len(store.rows("SELECT * FROM jobs WHERE status<>'cancelled'"))==1


def test_legacy_uncertain_jobs_are_never_consolidated(monitor):
    store=monitor.store
    for id in ['01','02']:
        job=store.create_job('Triage this incoming Teams message. Chat ID: legacy Message ID: '+id,'Communications')
        store.update(job['id'],status='blocked')
        store.write("INSERT INTO approvals(id,job_id,status) VALUES(?,?,?)",(id,job['id'],'uncertain'))
    reconcile_legacy(store)
    assert not store.rows("SELECT * FROM jobs WHERE status='cancelled'")
    assert not store.rows('SELECT * FROM communication_threads')


def test_legacy_delegation_is_retired_with_prior_sends_preserved(monitor):
    store=monitor.store
    request='Triage this incoming Teams message. Chat ID: legacy Message ID: '
    first=store.create_job(request+'01','Communications')
    second=store.create_job(request+'02','Communications')
    child=store.create_job('Old delegated task','Engineering',parent_id=first['id'])
    store.update(first['id'],status='waiting_children')
    # Prefer the job containing explicit owner feedback.
    store.write('INSERT INTO review_messages(job_id,role,body) VALUES(?,?,?)',(second['id'],'user','Continue here'))
    store.write("INSERT INTO approvals(id,job_id,tool,args,status,result) VALUES(?,?,?,?,?,?)",('sent',first['id'],'send_teams','{"body":"Already sent answer"}','executed','{"sent":true}'))
    reconcile_legacy(store)
    deliver_waiting(store)
    assert store.job(child['id'])['status']=='cancelled'
    assert store.job(first['id'])['status']=='cancelled'
    assert store.job(second['id'])['status']=='queued'
    assert 'Already sent answer' in store.job(second['id'])['messages']
    assert store.one('SELECT status FROM approvals')['status']=='executed'


def test_completed_legacy_history_is_adopted_without_reopening(monitor):
    store=monitor.store
    for id in ['01','02']:
        job=store.create_job('Triage this incoming Teams message. Chat ID: legacy Message ID: '+id,'Communications')
        store.update(job['id'],status='completed')
    reconcile_legacy(store)
    deliver_waiting(store)
    assert len(store.rows("SELECT * FROM jobs WHERE status='completed'"))==2
    assert len(store.rows('SELECT * FROM communication_threads'))==1
    assert not store.rows('SELECT * FROM communication_inbox')
