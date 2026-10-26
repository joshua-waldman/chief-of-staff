import json
import hashlib
from pathlib import Path
import pytest

from cos.db import Store
from cos.commitments import Commitments
from cos.policy import classify
from cos.tools import Tools,allowed_action


@pytest.fixture
def env(tmp_path):
    store=Store(tmp_path/'register.db')
    job=store.create_job('Review release planning meeting','Communications')
    text='[00:01:00] Alex: I will prepare the release plan by Friday.\n[00:02:00] Alex: The release plan is delivered.\n[00:03:00] Taylor: I will prepare the release plan by Friday.'
    path=tmp_path/'transcript.txt';path.write_text(text,encoding='utf-8')
    store.write('INSERT INTO files VALUES(?,?,?,?,?,?)',('transcript',job['id'],path.name,str(path),'text/plain','now'))
    store.set_state('meeting_sources:'+job['id'],{'transcript':{'identity':'recording-one','source':'https://example.com/recording/one','chat_id':'meeting-one','sha256':hashlib.sha256(path.read_bytes()).hexdigest()}})
    args={'owner':'Alex','deliverable':'Prepare the release plan','source_file_id':'transcript','source_timestamp':'00:01:00','meeting_date':'2026-10-22','evidence':text.splitlines()[0]}
    return store,job,Commitments(store),args


def review(registry,item,action,**extra):
    return registry.review(item['id'],{'action':action,'expected_version':item['version'],**extra})


def another_source(store,tmp_path,identity='recording-two'):
    job=store.create_job('Review a later meeting','Communications')
    text='[00:05:00] Alex: I completed and shared the release plan.'
    path=tmp_path/(job['id']+'.txt');path.write_text(text,encoding='utf-8')
    file_id='transcript-'+job['id']
    store.write('INSERT INTO files VALUES(?,?,?,?,?,?)',(file_id,job['id'],path.name,str(path),'text/plain','now'))
    store.set_state('meeting_sources:'+job['id'],{file_id:{'identity':identity,'source':'https://example.com/recording/two','chat_id':'meeting-two','sha256':hashlib.sha256(path.read_bytes()).hexdigest()}})
    return job,{'source_file_id':file_id,'source_timestamp':'00:05:00','evidence':text,'meeting_date':'2026-10-23'}


def test_source_linked_proposal_is_durable_and_idempotent(env):
    store,job,registry,args=env
    item=registry.record(job,args)
    assert item['status']=='proposed' and item['version']==1
    assert item['evidence'][0]['source_job']==job['id']
    assert item['evidence'][0]['source_url']=='https://example.com/recording/one'
    assert registry.attention_count()==1
    repeated=registry.record(job,args)
    assert repeated['id']==item['id'] and repeated['version']==1 and repeated['reused']
    assert len(repeated['evidence'])==1
    reopened=Commitments(Store(store.path))
    assert reopened.detail(item['id'])['deliverable']==args['deliverable']


@pytest.mark.parametrize('changes',[
    {'evidence':'[00:01:00] Invented assignment'},
    {'source_timestamp':'00:09:00'},
    {'source_timestamp':'no timestamp'},
    {'meeting_date':'2026-02-30'},
    {'deadline_date':'2026-10-30'},
    {'deadline_certainty':'inferred','deadline_text':'Friday'},
    {'deadline_certainty':'explicit','deadline_text':'Saturday'},
    {'status':'open'},
    {'owner':''},
    {'deadline_certainty':{}},
])
def test_invalid_or_unsupported_source_claims_cannot_create_records(env,changes):
    store,job,registry,args=env
    with pytest.raises(ValueError):registry.record(job,{**args,**changes})
    assert registry.search()['total']==0


def test_other_jobs_cannot_cite_unregistered_transcripts(env):
    store,job,registry,args=env
    other=store.create_job('Other meeting','Communications')
    with pytest.raises(ValueError,match='registered'):registry.record(other,args)


def test_idempotency_and_merge_suggestions_respect_scope(env):
    store,job,registry,args=env
    first=registry.record(job,{**args,'scope':'Project One'})
    second=registry.record(job,{**args,'scope':'Project Two'})
    assert first['id']!=second['id']
    assert not second['possible_matches']
    assert registry.search(scope='Project One')['total']==1


def test_modified_or_legacy_transcripts_require_fresh_provenance(env):
    store,job,registry,args=env
    path=Path(store.one('SELECT path FROM files WHERE id=?',('transcript',))['path'])
    path.write_text(args['evidence']+'\nModified source',encoding='utf-8')
    with pytest.raises(ValueError,match='integrity'):registry.record(job,args)
    sources=store.state('meeting_sources:'+job['id'])
    sources['transcript'].pop('sha256')
    store.set_state('meeting_sources:'+job['id'],sources)
    with pytest.raises(ValueError,match='integrity'):registry.record(job,args)


def test_deadline_wording_and_inference_are_kept_separate(env):
    store,job,registry,args=env
    item=registry.record(job,{**args,'deadline_certainty':'inferred','deadline_text':'Friday','deadline_date':'2026-10-30','deadline_basis':'Calendar date inferred from the meeting context; confirm with owner.'})
    assert item['deadline_certainty']=='inferred'
    assert item['deadline_text']=='Friday' and item['deadline_date']=='2026-10-30'
    assert item['status']=='proposed'
    item=review(registry,item,'edit',fields={'deadline_date':'','deadline_certainty':'unknown'})
    assert not item['deadline_date']
    assert item['history'][-1]['actor']=='owner'


def test_completion_evidence_from_later_meeting_needs_review(env,tmp_path):
    store,job,registry,args=env
    item=review(registry,registry.record(job,args),'accept')
    later,source=another_source(store,tmp_path)
    item=registry.add_evidence(later,{**source,'commitment_id':item['id'],'kind':'completion'})
    assert item['status']=='open' and item['completion_pending']
    assert {e['source_job'] for e in item['evidence']}=={job['id'],later['id']}
    before=item['version']
    assert registry.add_evidence(later,{**source,'commitment_id':item['id'],'kind':'completion'})['version']==before
    evidence_id=next(e['id'] for e in item['evidence'] if e['kind']=='completion')
    item=review(registry,item,'complete',evidence_id=evidence_id)
    assert item['status']=='completed' and not item['completion_pending']
    assert registry.attention_count()==0
    event=json.loads(item['history'][-1]['details'])
    assert event['verification']=='source-reviewed' and event['evidence_id']==evidence_id


def test_status_changes_require_owner_verification_and_preserve_closed_state(env):
    store,job,registry,args=env
    item=registry.record(job,args)
    with pytest.raises(ValueError):review(registry,item,'complete',note='Not accepted yet')
    item=review(registry,item,'accept')
    with pytest.raises(ValueError,match='Verify completion'):review(registry,item,'complete')
    item=review(registry,item,'complete',note='I verified the published deliverable.')
    assert item['status']=='completed'
    assert registry.record(job,args)['status']=='completed'
    with pytest.raises(ValueError):review(registry,item,'edit',fields={'owner':'Taylor'})
    item=review(registry,item,'reopen',note='A revision is required.')
    item=review(registry,item,'dismiss',note='Superseded by the new plan.')
    assert registry.record(job,args)['status']=='dismissed'
    assert registry.search()['total']==0 and registry.search(status='all')['total']==1


def test_review_rejects_stale_version_and_boolean_version(env,tmp_path):
    store,job,registry,args=env
    original=registry.record(job,args)
    current=review(registry,original,'accept')
    with pytest.raises(ValueError,match='changed'):review(registry,original,'dismiss',note='Stale dismissal')
    with pytest.raises(ValueError,match='version'):registry.review(current['id'],{'action':'dismiss','expected_version':True,'note':'Invalid version'})
    later,source=another_source(store,tmp_path)
    registry.add_evidence(later,{**source,'commitment_id':current['id'],'kind':'completion'})
    with pytest.raises(ValueError,match='changed'):review(registry,current,'complete',note='Review missed the new evidence')


def test_merging_repeated_work_retains_all_sources_and_tombstone(env,tmp_path):
    store,job,registry,args=env
    target=review(registry,registry.record(job,args),'accept')
    later,source=another_source(store,tmp_path)
    proposal_args={**args,**source,'deliverable':'Prepare the release plan'}
    duplicate=registry.record(later,proposal_args)
    assert target['id'] in {m['id'] for m in duplicate['possible_matches']}
    merged=review(registry,duplicate,'merge',note='Same owner deliverable discussed again.',target_id=target['id'],target_version=target['version'])
    assert merged['status']=='merged' and merged['merged_into']==target['id']
    combined=registry.detail(target['id'])
    assert combined['status']=='open' and len(combined['evidence'])==2
    assert len(merged['evidence'])==1
    assert registry.record(later,proposal_args)['id']==target['id']
    assert registry.search()['total']==1 and registry.search(status='all')['total']==2
    assert combined['history'][-1]['action']=='merged_from'


def test_merge_is_not_automatic_and_checks_both_versions_owner_and_scope(env):
    store,job,registry,args=env
    first=registry.record(job,args)
    second=registry.record(job,{**args,'deliverable':'Prepare the release plan document'})
    assert registry.search()['total']==2 and second['possible_matches']
    with pytest.raises(ValueError,match='reason|Explain'):review(registry,second,'merge',target_id=first['id'],target_version=first['version'])
    with pytest.raises(ValueError,match='different'):review(registry,second,'merge',note='Same work',target_id=second['id'],target_version=second['version'])
    first=review(registry,first,'edit',fields={'owner':'Taylor'})
    with pytest.raises(ValueError):review(registry,second,'merge',note='Wrong target owner',target_id=first['id'],target_version=first['version'])


def test_completion_cannot_cite_another_records_evidence(env,tmp_path):
    store,job,registry,args=env
    first=review(registry,registry.record(job,args),'accept')
    second=review(registry,registry.record(job,{**args,'deliverable':'Write a different release memo'}),'accept')
    later,source=another_source(store,tmp_path)
    first=registry.add_evidence(later,{**source,'commitment_id':first['id'],'kind':'completion'})
    evidence=next(e['id'] for e in first['evidence'] if e['kind']=='completion')
    with pytest.raises(ValueError,match='belonging'):review(registry,second,'complete',evidence_id=evidence,note='Even with a note, do not misattribute source evidence.')


def test_search_pagination_and_context_keep_uncertain_status(env):
    store,job,registry,args=env
    for index in range(4):registry.record(job,{**args,'deliverable':'Release plan '+str(index)})
    page=registry.search(query='release',limit=2,offset=2)
    assert page['total']==4 and len(page['items'])==2
    assert not registry.search(owner='Taylor')['items']
    context=json.loads(registry.context(job))
    assert context and all(r['status']=='proposed' and r['deadline_certainty']=='unknown' for r in context)


def test_agent_tools_can_only_propose_and_cite_evidence(env):
    store,job,registry,args=env
    item=Tools(store).execute(job,'record_commitment',args)
    assert item['status']=='proposed'
    assert Tools(store).execute(job,'search_commitments',{'query':'release'})['total']==1
    for action in ('record_commitment','add_commitment_evidence','search_commitments'):
        assert not classify(action,{}).approval
    assert not allowed_action('Strategy','record_commitment')
    assert not allowed_action('Communications','review_commitment')
    assert classify('send_email',{}).approval
    assert classify('delegate',{},'Communications').approval


def test_commitment_api_auth_csrf_status_and_attention(env,monkeypatch):
    from fastapi.testclient import TestClient
    from cos import server
    store,job,registry,args=env;monkeypatch.setattr(server,'store',store)
    monkeypatch.setattr(server,'gateway_key',lambda:'')
    item=registry.record(job,args)
    client=TestClient(server.app)
    assert client.get('/api/commitments').status_code==401
    client.get('/')
    assert client.get('/api/commitments').json()['total']==1
    assert client.get('/api/status').json()['commitments_attention']==1
    route='/api/commitments/'+item['id']
    assert client.post(route,json={'action':'accept','expected_version':1}).status_code==403
    headers={'X-COS-Token':server.TOKEN}
    result=client.post(route,json={'action':'accept','expected_version':1},headers=headers)
    assert result.status_code==200 and result.json()['status']=='open'
    assert client.post(route,json={'action':'dismiss','expected_version':1,'note':'Stale'},headers=headers).status_code==400
    assert client.post(route,json={'action':'complete','expected_version':2,'note':'I verified the result.'},headers=headers).json()['status']=='completed'
    assert client.get('/api/status').json()['commitments_attention']==0
    assert client.get('/api/commitments?status=completed').json()['total']==1
    assert client.get('/static/commitments.js').status_code==200
