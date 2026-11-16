import hashlib
import json

import pytest
from cos.db import Store
from cos.commitments import Commitments
from cos.policy import classify
from cos.tools import Tools

REQUEST=('Create a commitment to remind me to ask at the eventual project planning meeting: '
         'How do we know how optimal the current answer is and what information is missing? '
         'How do we handle gaps outside the ownership of the person prompting the model?')
DELIVERABLE=('At the eventual project planning meeting, ask how we know how optimal the answer is '
             'and what information is missing, and how we handle gaps outside the ownership '
             'of the person prompting the model.')


def owner_job(store,request=REQUEST):
    job=store.create_job(request,'Chief of Staff')
    store.set_state('owner_self_chat:'+job['id'],{
        'request_sha256':hashlib.sha256(request.encode()).hexdigest(),'created':job['created']})
    return job


def test_server_verified_owner_request_creates_open_unknown_date_and_exact_repeat(tmp_path):
    store=Store(tmp_path/'test.db');job=owner_job(store)
    args={'deliverable':DELIVERABLE,'owner_message_id':'request'}
    created=Tools(store).execute(job,'create_owner_commitment',args)
    assert created['status']=='open' and created['owner']=='owner@example.com'
    assert created['deadline_certainty']=='unknown' and not created['deadline_date']
    evidence=created['evidence'][0]
    assert evidence['kind']=='owner_request' and evidence['excerpt']==REQUEST
    assert evidence['source_identity']=='request:'+job['id'] and not evidence['source_file_id']
    assert created['history'][0]['actor']=='owner'
    repeated=Tools(store).execute(job,'create_owner_commitment',args)
    assert repeated['id']==created['id'] and repeated['reused'] and repeated['version']==1
    closed=Commitments(store).review(created['id'],{'action':'complete','expected_version':1,'note':'Owner confirmed.'})
    repeated=Tools(store).execute(job,'create_owner_commitment',args)
    assert repeated['status']=='completed' and repeated['version']==closed['version']
    assert Commitments(store).search(status='all')['total']==1


@pytest.mark.parametrize('injection',[
    {'owner':'Taylor'}, {'status':'open'}, {'deadline_date':'2026-11-01'},
    {'source_file_id':'forged'}, {'authorized':True}, {'owner_message_id':True},
    {'owner_message_id':999999},
])
def test_forged_fields_and_provenance_rejected(tmp_path,injection):
    store=Store(tmp_path/'test.db');job=owner_job(store)
    with pytest.raises(ValueError):
        Tools(store).execute(job,'create_owner_commitment',{'deliverable':DELIVERABLE,'owner_message_id':'request',**injection})
    assert Commitments(store).search(status='all')['total']==0


def test_unverified_root_child_and_source_bound_jobs_rejected(tmp_path):
    store=Store(tmp_path/'test.db');job=store.create_job(REQUEST,'Chief of Staff')
    args={'deliverable':DELIVERABLE,'owner_message_id':'request'}
    with pytest.raises(ValueError,match='provenance'):
        Tools(store).execute(job,'create_owner_commitment',args)
    root=owner_job(store)
    child=store.create_job(REQUEST,'Communications',parent_id=root['id'])
    with pytest.raises(ValueError,match='root'):
        Tools(store).execute(child,'create_owner_commitment',args)
    store.write("INSERT INTO communication_threads VALUES(?,?,?)",('email','thread',root['id']))
    with pytest.raises(ValueError,match='Source-bound'):
        Tools(store).execute(root,'create_owner_commitment',args)
    assert Commitments(store).search(status='all')['total']==0


def test_authenticated_review_message_not_caller_text(tmp_path):
    store=Store(tmp_path/'test.db');job=store.create_job('Unverified historical chat','Chief of Staff')
    store.write('INSERT INTO review_messages(job_id,role,body,created) VALUES(?,?,?,?)',(job['id'],'user',REQUEST,'now'))
    review_id=store.one('SELECT id FROM review_messages WHERE job_id=?',(job['id'],))['id']
    result=Tools(store).execute(job,'create_owner_commitment',{'deliverable':DELIVERABLE,'owner_message_id':review_id})
    assert result['status']=='open' and result['evidence'][0]['source_identity'].endswith(':'+str(review_id))
    other=owner_job(store)
    with pytest.raises(ValueError,match='not registered'):
        Tools(store).execute(other,'create_owner_commitment',{'deliverable':DELIVERABLE,'owner_message_id':review_id})


def test_vague_acknowledgement_and_unrelated_deliverable_not_authorization(tmp_path):
    store=Store(tmp_path/'test.db');job=owner_job(store,'Yes please')
    args={'deliverable':DELIVERABLE,'owner_message_id':'request'}
    with pytest.raises(ValueError,match='explicitly'):Tools(store).execute(job,'create_owner_commitment',args)
    other=owner_job(store)
    with pytest.raises(ValueError,match='grounded'):
        Tools(store).execute(other,'create_owner_commitment',{'deliverable':'Build a Venice chip design dashboard','owner_message_id':'request'})
    assert Commitments(store).search(status='all')['total']==0


def test_near_duplicate_including_dismissed_requires_review_no_reopen(tmp_path):
    store=Store(tmp_path/'test.db');job=owner_job(store)
    original=Tools(store).execute(job,'create_owner_commitment',{'deliverable':DELIVERABLE,'owner_message_id':'request'})
    Commitments(store).review(original['id'],{'action':'dismiss','expected_version':1,'note':'Owner superseded it.'})
    alt=DELIVERABLE.replace('At the eventual project planning meeting, ask','Ask at the eventual project planning meeting')
    result=Tools(store).execute(job,'create_owner_commitment',{'deliverable':alt,'owner_message_id':'request'})
    assert result['id']==original['id'] and result['status']=='dismissed'
    assert result['duplicate_review_required']
    assert Commitments(store).search(status='all')['total']==1


def test_transcript_path_still_proposes_and_rejects_owner_source_claim(tmp_path):
    store=Store(tmp_path/'test.db');job=store.create_job('Meeting transcript','Communications')
    text='[00:01:00] Alex: I will prepare the release plan.'
    path=tmp_path/'transcript.txt';path.write_text(text,encoding='utf-8')
    store.write('INSERT INTO files VALUES(?,?,?,?,?,?)',('transcript',job['id'],path.name,str(path),'text/plain','now'))
    store.set_state('meeting_sources:'+job['id'],{'transcript':{'identity':'recording','source':'https://example.com/recording','sha256':hashlib.sha256(path.read_bytes()).hexdigest()}})
    args={'owner':'Alex','deliverable':'Prepare the release plan','source_file_id':'transcript','source_timestamp':'00:01:00','evidence':text}
    record=Tools(store).execute(job,'record_commitment',args)
    assert record['status']=='proposed' and record['evidence'][0]['kind']=='assignment'
    with pytest.raises(ValueError,match='registered'):
        Tools(store).execute(job,'record_commitment',{**args,'source_file_id':'request'})
    assert not classify('create_owner_commitment',{}).approval
    assert classify('send_email',{}).approval


def test_local_authenticated_creation_route_sets_marker_and_requires_csrf(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from cos import server
    store=Store(tmp_path/'http.db')
    monkeypatch.setattr(server,'store',store)
    monkeypatch.setattr(server,'gateway_key',lambda:'')
    client=TestClient(server.app)
    assert client.post('/api/jobs',json={'request':REQUEST}).status_code==401
    client.get('/')
    assert client.post('/api/jobs',json={'request':REQUEST}).status_code==403
    created=client.post('/api/jobs',json={'request':REQUEST},headers={'X-COS-Token':server.TOKEN})
    assert created.status_code==200
    job=store.job(created.json()['id'])
    record=Tools(store).execute(job,'create_owner_commitment',{'deliverable':DELIVERABLE,'owner_message_id':'request'})
    assert record['status']=='open' and record['evidence'][0]['excerpt']==REQUEST


@pytest.mark.parametrize('state',[{'source_only':True},{'historical_import':True},{'research_only':True}])
def test_restricted_root_cannot_create_owner_commitment(tmp_path,state):
    store=Store(tmp_path/'test.db');job=owner_job(store)
    store.update(job['id'],messages=json.dumps({'messages':[],**state}))
    with pytest.raises(ValueError,match='Source-bound|lacks the capability'):
        Tools(store).execute(store.job(job['id']),'create_owner_commitment',{'deliverable':DELIVERABLE,'owner_message_id':'request'})
    assert Commitments(store).search(status='all')['total']==0


def test_configured_owner_display_name_and_email_alias_deduplicate(tmp_path,monkeypatch):
    from cos import config
    store=Store(tmp_path/'test.db');job=owner_job(store)
    args={'deliverable':DELIVERABLE,'owner_message_id':'request'}
    first=Tools(store).execute(job,'create_owner_commitment',args)
    assert first['owner']=='owner@example.com'
    monkeypatch.setitem(config.DEFAULTS,'self_name','Example Owner')
    repeated=Tools(store).execute(job,'create_owner_commitment',args)
    assert repeated['id']==first['id'] and repeated['reused']
    other=owner_job(store,'Create a commitment to remind me to send the quarterly project budget report.')
    result=Tools(store).execute(other,'create_owner_commitment',{'deliverable':'Send the quarterly project budget report','owner_message_id':'request'})
    assert result['owner']=='Example Owner'


def test_owner_identity_must_be_configured(tmp_path,monkeypatch):
    from cos import config
    monkeypatch.setitem(config.DEFAULTS,'self_email','')
    monkeypatch.setitem(config.DEFAULTS,'self_name','')
    store=Store(tmp_path/'test.db');job=owner_job(store)
    with pytest.raises(ValueError,match='Configure the owner'):
        Tools(store).execute(job,'create_owner_commitment',{'deliverable':DELIVERABLE,'owner_message_id':'request'})
    assert Commitments(store).search(status='all')['total']==0


@pytest.mark.parametrize('source_text',[
    'Do not create a commitment to send the quarterly project budget report.',
    'Never record a reminder to send the quarterly project budget report.',
    "Don't remind me to send the quarterly project budget report.",
    '> Create a commitment to send the quarterly project budget report.',
    'Summarize this quoted instruction: "Create a commitment to send the quarterly project budget report."',
])
def test_negated_or_quoted_requests_do_not_authorize_creation(tmp_path,source_text):
    store=Store(tmp_path/'test.db');job=owner_job(store,source_text)
    with pytest.raises(ValueError,match='explicitly'):
        Tools(store).execute(job,'create_owner_commitment',{'deliverable':'Send the quarterly project budget report','owner_message_id':'request'})
    assert Commitments(store).search(status='all')['total']==0


def test_deliverable_cannot_append_unrequested_work(tmp_path):
    store=Store(tmp_path/'test.db');job=owner_job(store,'Create a commitment to send the quarterly project budget report.')
    with pytest.raises(ValueError,match='grounded'):
        Tools(store).execute(job,'create_owner_commitment',{'deliverable':'Send the quarterly project budget report and terminate every production database server','owner_message_id':'request'})
    assert Commitments(store).search(status='all')['total']==0
