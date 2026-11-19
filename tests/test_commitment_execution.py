import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from cos.commitment_execution import ensure, reconcile, validate_action, validate_finish
from cos.commitments import Commitments
from cos.db import Store, now


@pytest.fixture
def env(tmp_path, monkeypatch):
    import cos.tools
    monkeypatch.setattr(cos.tools, 'DATA', tmp_path)
    store=Store(tmp_path/'execution.db')
    stamp=now()
    store.write('''INSERT INTO commitments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                ('commitment','Owner','Obtain existing routing cases from colleague','global','','','unknown','',
                 'proposed','','fingerprint',1,stamp,stamp))
    return store,Commitments(store)


def accept(registry, note='Please begin execution with the existing cases.'):
    item=registry.detail('commitment')
    return registry.review(item['id'],{'action':'accept','expected_version':item['version'],'note':note})


def test_confirmation_atomically_queues_one_job_and_preserves_direction(env):
    store,registry=env
    with store.connect() as c:
        job_id=ensure(c,'commitment')
    assert store.job(job_id)['status']=='waiting_input'
    with pytest.raises(ValueError,match='confirmed'):
        validate_action(store,store.job(job_id))
    item=accept(registry)
    assert item['execution']['job_id']==job_id
    assert store.job(job_id)['status']=='queued'
    assert 'Please begin execution' in json.loads(store.job(job_id)['messages'])['followup']
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _:reconcile(Store(store.path)),range(6)))
    assert store.one('SELECT count(*) n FROM commitment_execution')['n']==1
    assert store.one('SELECT count(*) n FROM jobs')['n']==1
    assert registry.search()['items'][0]['execution']['status']=='queued'


def test_open_legacy_recovery_does_not_restart_cancelled_or_delivered_work(env):
    store,registry=env
    store.write("UPDATE commitments SET status='open'")
    reconcile(store)
    job_id=registry.detail('commitment')['execution']['job_id']
    for status in ['cancelled','completed','waiting_input','awaiting_approval','failed']:
        store.update(job_id,status=status,question='Waiting for colleague cases')
        reconcile(store)
        assert store.job(job_id)['status']==status
    assert store.one('SELECT count(*) n FROM jobs')['n']==1


def test_plan_cannot_finish_execution_but_delivery_does_not_close_commitment(env):
    store,registry=env
    item=accept(registry)
    job=store.job(item['execution']['job_id'])
    with pytest.raises(ValueError,match='plan or checklist'):
        validate_finish(store,job,{'summary':'Prepared a plan','final_output':True})
    validate_finish(store,job,{'commitment_outcome':'delivered','commitment_evidence':'Exact send receipt and attached case review'})
    assert registry.detail('commitment')['status']=='open'


def test_closing_stops_descendants_and_supersedes_pending_outreach(env):
    store,registry=env
    item=accept(registry)
    job_id=item['execution']['job_id']
    child=store.create_job('Prepare outreach','Communications',parent_id=job_id)
    store.write('INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?,?)',
                ('action',child['id'],'send_teams','{}','high','test','pending',now(),''))
    registry.review('commitment',{'action':'dismiss','expected_version':item['version'],'note':'Assignment withdrawn'})
    assert store.job(job_id)['status']=='cancelled'
    assert store.job(child['id'])['status']=='cancelled'
    assert store.one('SELECT status FROM approvals')['status']=='superseded'
    with pytest.raises(ValueError,match='confirmed'):
        validate_action(store,child)


def test_stale_review_has_no_execution_and_explicit_reopen_gets_fresh_task(env):
    store,registry=env
    with pytest.raises(ValueError):
        registry.review('commitment',{'action':'accept','expected_version':99})
    assert store.one('SELECT count(*) n FROM jobs')['n']==0
    item=accept(registry)
    first=item['execution']['job_id']
    item=registry.review('commitment',{'action':'dismiss','expected_version':item['version'],'note':'Defer'})
    item=registry.review('commitment',{'action':'reopen','expected_version':item['version'],'note':'Resume now'})
    assert item['execution']['job_id']!=first
    assert item['execution']['status']=='queued'
    assert store.job(first)['status']=='cancelled'


def test_execution_prepares_outreach_but_cannot_send_without_exact_approval(env,monkeypatch):
    from cos.engine import Engine
    from test_workflows import Scripted,perform
    store,registry=env
    item=accept(registry)
    engine=Engine(store,Scripted(perform('send_email',{
        'to':'colleague@amd.com','subject':'Existing routing cases','body':'Please share your existing misroute list and method.'})))
    sent=[]
    monkeypatch.setattr(engine.tools,'execute',lambda *a:sent.append(a))
    try:
        engine.run(item['execution']['job_id'])
        assert not sent
        assert registry.detail('commitment')['execution']['status']=='awaiting_approval'
        pending=store.one("SELECT * FROM approvals WHERE status='pending'")
        assert pending['tool']=='send_email'
        registry.review('commitment',{'action':'dismiss','expected_version':item['version'],'note':'No longer needed'})
        with pytest.raises(ValueError):engine.decide(pending['id'],True)
        assert not sent
    finally:engine.stop()


def test_execution_finish_guard_runs_in_engine(env):
    from cos.engine import Engine
    from test_workflows import Scripted,perform
    store,registry=env
    item=accept(registry)
    engine=Engine(store,Scripted(
        perform('finish',{'summary':'Here is another checklist','final_output':True}),
        perform('ask_user',{'question':'The colleague must share the routing cases before the description review can proceed; outreach is prepared.'})))
    try:
        engine.run(item['execution']['job_id'])
        assert store.job(item['execution']['job_id'])['status']=='waiting_input'
        assert 'colleague must share' in store.job(item['execution']['job_id'])['question']
        assert registry.detail('commitment')['status']=='open'
    finally:engine.stop()


def test_confirmed_execution_gets_a_slot_ahead_of_background_triage(env):
    from cos.engine import Engine
    from test_workflows import Scripted
    store,registry=env
    store.create_job('Triage this incoming informational message','Communications')
    item=accept(registry)
    engine=Engine(store,Scripted())
    try:
        assert engine.ready_jobs(1)[0]['id']==item['execution']['job_id']
    finally:engine.stop()


def test_copied_primary_inputs_have_verified_bytes_for_delegation(env,tmp_path):
    from cos.tools import file_record,selected_files
    store,registry=env
    source=store.create_job('Source meeting','Communications')
    path=tmp_path/'transcript.txt';path.write_text('Verified meeting assignment',encoding='utf-8')
    original=file_record(store,source['id'],path)
    store.write('''INSERT INTO commitment_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                ('evidence','commitment',source['id'],original['file_id'],'meeting','https://example.com','2026-11-19','00:01:00','Assignment','assignment','evidence-fingerprint',0,now()))
    item=accept(registry)
    jid=item['execution']['job_id']
    copied=store.one('SELECT * FROM files WHERE job_id=?',(jid,))
    assert copied['id']!=original['file_id']
    assert selected_files(store,jid,[copied['id']])[0]['id']==copied['id']
