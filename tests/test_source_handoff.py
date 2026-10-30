import json
from pathlib import Path

import pytest
from cos import config, tools
from cos.db import Store
from cos.engine import Engine, session, perform_schema
from test_workflows import Scripted, perform


def native(action,args):
    answer=perform(action,{})
    answer['tool_calls'][0]['function']['arguments']=json.dumps({'action':action,'arguments':args})
    return answer


def test_latest_returned_report_survives_compacted_history_and_is_primary_evidence(setup):
    store,engine=setup
    parent=store.create_job('Synthesize methodology','Strategy')
    old=store.create_job('Read overview','Technology',parent_id=parent['id'])
    store.update(old['id'],status='completed',result='Implementation not inspected')
    child=store.create_job('Inspect implementation','Technology',parent_id=parent['id'])
    report='Pinned implementation inspected.\n'+'Verified source findings.\n'*250
    store.update(child['id'],status='completed',result=report)
    state={'messages':[],'delivery_pending':True}
    returned=engine.returned_work(parent,state)
    assert returned[0]['id']==child['id']
    evidence=store.one('SELECT * FROM files WHERE id=?',(returned[0]['full_result_file_id'],))
    assert Path(evidence['path']).parent.name=='sources'
    assert Path(evidence['path']).read_text(encoding='utf-8')==report
    state['messages']=[{'role':'system','content':'Compacted old conversation: implementation unknown'}]
    assert engine.returned_work(parent,state)==returned
    assert len(store.rows('SELECT * FROM files WHERE job_id=?',(parent['id'],)))==2
    assert not store.pending_results()


def test_returned_synthesis_has_bounded_verification_even_when_new_pages_arrive(setup):
    from cos import progress
    store,engine=setup
    parent=store.create_job('Synthesize methodology','Strategy')
    child=store.create_job('Inspect source','Technology',parent_id=parent['id'])
    store.update(child['id'],status='completed',result='Verified audit with explicit gaps')
    state={'delivery_pending':True,'total_model_steps':23}
    engine.focus_returned_synthesis(parent,state)
    assert not state.get('stall_recovery')
    state['total_model_steps']=24
    engine.focus_returned_synthesis(parent,state)
    attempt=state['stall_recovery']
    assert attempt['phase']=='deliver' and attempt['remaining']==8
    progress.limit_recovery_read(state,'read_file')
    progress.observe(state,'read_file',{'path':'source.txt'},{'text':'new source page','sha256':'new'})
    attempt['remaining']=7
    engine.focus_returned_synthesis(parent,state)
    assert attempt['active'] and attempt['verification_reads']==1 and attempt['remaining']==7
    progress.limit_recovery_read(state,'read_file')
    with pytest.raises(ValueError,match='verification budget exhausted'):
        progress.limit_recovery_read(state,'read_file')
    attempt['remaining']=0
    engine.focus_returned_synthesis(parent,state)
    assert progress.recovery_step(state,12)=='exhausted'
    assert not store.pending_results()


@pytest.fixture
def setup(tmp_path,monkeypatch):
    monkeypatch.setattr(tools,'DATA',tmp_path/'data')
    monkeypatch.setattr(config,'DATA',tmp_path/'data')
    store=Store(tmp_path/'source-handoff.db')
    return store,Engine(store)


def blocked_reader(store,engine):
    parent=store.create_job('Produce a repository methodology report','Strategy')
    store.update(parent['id'],messages=json.dumps({'messages':[],'delivery_pending':True}))
    engine.provider=Scripted(perform('delegate',{'tasks':[{'org':'Technology','source_only':True,'request':'Read implementation only'}]}))
    engine.run(parent['id'])
    child=store.one('SELECT * FROM jobs WHERE parent_id=?',(parent['id'],))
    engine.tools.execute(child,'write_file',{'path':'evidence/source.txt','content':'Actual retained source'})
    engine.tools.execute(child,'write_file',{'path':'audit-draft.md','content':'Unreviewed source audit draft'})
    engine.tools.execute(child,'write_file',{'path':'context/checkpoint.txt','content':'Internal recovery history'})
    store.update(child['id'],status='blocked',question='Verification read loop exhausted')
    return parent,store.job(child['id'])


def test_native_object_arguments_round_trip_without_nested_path_json(setup):
    store,engine=setup
    schema=perform_schema(['read_file','finish'])['function']
    assert schema['strict'] is False
    assert schema['parameters']['properties']['arguments']['type']=='object'
    assert schema['parameters']['properties']['action']['enum']==['read_file','finish']
    job=store.create_job('Read retained source','Technology')
    source=engine.tools.execute(job,'write_file',{'path':'source.txt','content':'Known evidence'})
    path=store.one('SELECT path FROM files WHERE id=?',(source['file_id'],))['path']
    engine.provider=Scripted(native('read_file',{'path':path}),native('finish',{'summary':'Read source','result_kind':'strategy'}))
    engine.run(job['id'])
    assert store.job(job['id'])['status']=='completed'
    assert not store.rows("SELECT * FROM events WHERE job_id=? AND kind='tool_error'",(job['id'],))
    assert store.one("SELECT status FROM approvals WHERE job_id=? AND tool='read_file'",(job['id'],))['status']=='executed'


def test_blocked_source_handoff_preserves_evidence_and_does_not_publish_draft(setup):
    store,engine=setup
    parent,child=blocked_reader(store,engine)
    before=store.job(child['id'])
    engine.release_parents()
    assert store.job(parent['id'])['status']=='queued'
    message=session(store.job(parent['id']))['messages'][-1]
    returned=json.loads(message['content'])[-1]
    assert returned['status']=='blocked' and returned['partial'] is True
    assert returned['source_reader_complete'] is False
    assert len(returned['files'])==2
    assert all(f['evidence_only'] for f in returned['files'])
    assert {Path(f['path']).read_text(encoding='utf-8') for f in returned['files']}=={'Actual retained source','Unreviewed source audit draft'}
    assert store.job(child['id'])==before
    assert not store.pending_results()
    report=engine.tools.execute(store.job(parent['id']),'write_file',{'path':'final-report.txt','content':'The final report'})
    engine.provider=Scripted(native('finish',{'summary':'# Methodology report\n\nFinal analysis with stated coverage gaps.','result_kind':'strategy','final_output':True,'file_ids':[report['file_id']]}))
    engine.run(parent['id'])
    review=Store(store.path).result_review(parent['id'])
    assert review['file_ids']==[report['file_id']] and len(review['items'])==1
    assert store.job(child['id'])==before


def test_legacy_empty_failure_return_is_repaired_once_without_restarting_reader(setup):
    store,engine=setup
    parent,child=blocked_reader(store,engine)
    ordinary=store.create_job('Another strategy task','Strategy')
    state=session(store.job(parent['id']))
    engine.add_result(state['messages'],state.pop('waiting_call'),[{'id':child['id'],'status':'blocked','files':[]}])
    store.update(parent['id'],status='queued',messages=json.dumps(state))
    before=store.job(child['id'])
    engine.recover_source_handoffs()
    messages=store.job(parent['id'])['messages']
    assert 'retained evidence' in messages
    assert engine.ready_jobs(None)[0]['id']==parent['id']
    assert store.job(child['id'])==before
    count=len(store.rows('SELECT * FROM files WHERE job_id=?',(parent['id'],)))
    engine.recover_source_handoffs()
    assert store.job(parent['id'])['messages']==messages
    assert len(store.rows('SELECT * FROM files WHERE job_id=?',(parent['id'],)))==count
    assert len(store.rows("SELECT * FROM events WHERE job_id=? AND kind='source_handoff_recovered'",(parent['id'],)))==1
    assert not store.pending_results()


def test_no_handoff_recovery_through_unresolved_external_action(setup):
    store,engine=setup
    parent,child=blocked_reader(store,engine)
    state=session(store.job(parent['id'])); state.pop('waiting_call')
    store.update(parent['id'],status='queued',messages=json.dumps(state))
    store.write("INSERT INTO approvals(id,job_id,status) VALUES('uncertain-action',?,'uncertain')",(parent['id'],))
    before=store.job(parent['id'])
    engine.recover_source_handoffs()
    assert store.job(parent['id'])==before



def test_replacement_reader_reuses_retained_sources_without_completing_parent(setup):
    store,engine=setup
    parent,blocked=blocked_reader(store,engine)
    replacement=store.create_job('Verify remaining source details','Technology',parent_id=parent['id'])
    store.update(replacement['id'],messages=json.dumps({'messages':[],'source_only':True}))
    engine.recover_source_handoffs()
    assert store.job(parent['id'])['status']=='waiting_children'
    state=session(store.job(replacement['id']))
    assert 'Retained evidence' in state['followup']
    assert state['messages']==[] and state['delivery_ready'] is True
    files=store.rows('SELECT * FROM files WHERE job_id=?',(replacement['id'],))
    assert any(Path(f['path']).read_text(encoding='utf-8')=='Unreviewed source audit draft' for f in files)
    before=store.job(replacement['id'])
    engine.recover_source_handoffs()
    assert store.job(replacement['id'])==before
    assert store.job(blocked['id'])['status']=='blocked'
    assert not store.pending_results()



def test_research_mode_without_source_flag_still_preserves_partial_inputs(setup):
    store,engine=setup
    parent,child=blocked_reader(store,engine)
    state=session(child); state.pop('source_only'); state['research_only']=True
    store.update(child['id'],messages=json.dumps(state))
    replacement=store.create_job('Read-only implementation research','Technology',parent_id=parent['id'])
    store.update(replacement['id'],messages=json.dumps({'messages':[],'research_only':True}))
    engine.recover_source_handoffs()
    state=session(store.job(replacement['id']))
    assert state['source_handoff_versions'].get(child['id'])
    assert 'Retained evidence' in state['followup']
    assert store.job(child['id'])['status']=='blocked'


def test_textual_source_mode_is_not_a_substitute_for_enforced_mode(setup):
    store,engine=setup
    parent=store.create_job('Research source','Strategy')
    with pytest.raises(ValueError,match='task object field'):
        engine.validate_delegation(parent,{'tasks':[{'org':'Technology','request':'source_only:true. Read the implementation'}]})
    engine.validate_delegation(parent,{'tasks':[{'org':'Technology','source_only':True,'request':'source_only:true. Read the implementation'}]})



def test_retained_source_verification_returns_partial_inputs_at_bound(setup):
    from cos.engine import SOURCE_HANDOFF_MODEL_BUDGET
    store,engine=setup
    parent,blocked=blocked_reader(store,engine)
    replacement=store.create_job('Verify only the remaining source gap','Technology',parent_id=parent['id'])
    store.update(replacement['id'],messages=json.dumps({'messages':[],'research_only':True}))
    engine.recover_source_handoffs()
    state=session(store.job(replacement['id']))
    state['total_model_steps']=state['source_handoff_started_models']+SOURCE_HANDOFF_MODEL_BUDGET
    store.update(replacement['id'],messages=json.dumps(state))
    engine.provider=Scripted()  # No inference or repeat source read at the bound.
    engine.run(replacement['id'])
    assert store.job(replacement['id'])['status']=='blocked'
    assert store.rows("SELECT * FROM events WHERE job_id=? AND kind='source_partial_return'",(replacement['id'],))
    engine.release_parents()
    assert store.job(parent['id'])['status']=='queued'
    returned=json.loads(session(store.job(parent['id']))['messages'][-1]['content'])
    assert all(r['partial'] and not r['source_reader_complete'] for r in returned)
    assert all(r['files'] for r in returned)
    assert not store.pending_results()
