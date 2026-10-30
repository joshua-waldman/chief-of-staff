import json
from pathlib import Path

import pytest
from cos.db import Store
from cos.engine import Engine, session
from cos.delivery import recover, validate_finish
from cos.tools import Tools
from cos import tools, config
from test_workflows import Scripted, perform


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, 'DATA', tmp_path/'data')
    monkeypatch.setattr(config, 'DATA', tmp_path/'data')
    store=Store(tmp_path/'delivery.db')
    return store, Engine(store)


def test_director_must_synthesize_and_only_final_report_reaches_owner(setup):
    store, engine=setup
    parent=store.create_job('Review the repository methodology and provide one report', 'Strategy')
    engine.provider=Scripted(perform('delegate', {'tasks':[{'org':'Technology','request':'Inspect implementation'}]}))
    engine.run(parent['id'])
    child=store.one('SELECT * FROM jobs WHERE parent_id=?',(parent['id'],))
    source=engine.tools.execute(child,'write_file',{'path':'notes.txt','content':'Supporting research'})
    store.complete_result(child['id'],{'result_kind':'strategy','deliverable_file_ids':[source['file_id']]},'Intermediate implementation findings',1)
    assert not store.pending_results()
    engine.release_parents()
    report=engine.tools.execute(store.job(parent['id']),'write_file',{'path':'methodology-report.txt','content':'Final report content'})
    engine.provider=Scripted(
        perform('finish',{'summary':'No reply needed; research is complete.','result_kind':'triage'}),
        perform('finish',{'summary':'# Repository methodology report\n\nFinal analysis with limitations.', 'result_kind':'strategy','final_output':True,'file_ids':[report['file_id']]}))
    engine.run(parent['id'])
    assert store.job(parent['id'])['status']=='completed'
    assert store.rows("SELECT * FROM events WHERE kind='tool_error' AND body LIKE 'Delegated findings%'")
    review=store.result_review(parent['id'])
    assert review['file_ids']==[report['file_id']]
    assert 'Intermediate implementation findings' not in review['summary']
    assert len(review['items'])==1
    assert Store(store.path).result_review(parent['id'])==review
    # A later FYI must not resurface the already incorporated child research.
    store.update(parent['id'],status='queued')
    engine.provider=Scripted(perform('finish',{'summary':'Later informational update','result_kind':'triage'}))
    engine.run(parent['id'])
    assert Store(store.path).result_review(parent['id'])==review


def test_inline_final_report_keeps_citations_out_of_review_items(setup):
    store, engine=setup
    job=store.create_job('Write a methodology report','Technology')
    source=engine.tools.execute(job,'write_file',{'path':'evidence/source.txt','content':'Evidence'})
    summary='# Methodology report\n\nAnalysis cites [source]('+source['url']+').'
    engine.provider=Scripted(perform('finish',{'summary':summary,'result_kind':'strategy','final_output':True}))
    engine.run(job['id'])
    detail=Store(store.path).detail(job['id'])['result_review']
    assert len(detail['items'])==1 and detail['items'][0]['kind']=='inline'
    assert detail['items'][0]['content']==summary
    assert detail['files']==[]
    with store.connect() as c:
        with pytest.raises(ValueError,match='source evidence'):
            validate_finish(c,store.job(job['id']),{}, {'final_output':True,'result_kind':'strategy','summary':'Report','file_ids':[source['file_id']]})


def test_legacy_triage_bundle_is_resumed_once_without_authoring_or_replaying(setup):
    store, engine=setup
    root=store.create_job('Original meeting research request','Communications')
    child=store.create_job('Read repository','Technology',parent_id=root['id'])
    source=engine.tools.execute(child,'write_file',{'path':'evidence.txt','content':'Saved source evidence'})
    store.complete_result(child['id'],{'result_kind':'strategy','deliverable_file_ids':[source['file_id']]},'Saved findings',1)
    store.complete_result(root['id'],{'result_kind':'triage','deliverable_file_ids':[]},'Research done but summary remains unresolved',1)
    before=store.job(child['id'])
    assert recover(store,apply=False)==[root['id']]
    assert store.job(root['id'])['status']=='completed'
    assert recover(store)==[root['id']]
    assert store.job(root['id'])['status']=='queued'
    assert not store.pending_results()
    assert store.job(child['id'])==before
    assert recover(store)==[]
    engine.prepare_delivery_inputs(root['id'])
    files=store.rows('SELECT * FROM files WHERE job_id=?',(root['id'],))
    assert any('delivery-inputs' in f['name'] for f in files)
    assert any(Path(f['path']).read_text(encoding='utf-8')=='Saved source evidence' for f in files)
    assert 'final_output' not in json.loads(store.job(child['id'])['messages'])
    assert not Store(store.path).pending_results()


def test_recovery_honors_review_and_pending_external_actions(setup):
    store, engine=setup
    roots=[]
    for guard in ('reviewed','pending','cancelled'):
        root=store.create_job('Research request','Communications')
        child=store.create_job('Research','Technology',parent_id=root['id'])
        store.complete_result(child['id'],{'result_kind':'strategy','deliverable_file_ids':[]},'Findings',1)
        store.complete_result(root['id'],{'result_kind':'triage','deliverable_file_ids':[]},'Triage closure',1)
        if guard=='reviewed': store.review_result(root['id'],store.result_review(root['id'])['revision'])
        elif guard=='pending': store.write("INSERT INTO approvals(id,job_id,status) VALUES('pending',?,'pending')",(root['id'],))
        else: store.event(root['id'],'cancelled','Owner cancelled')
        roots.append(store.job(root['id']))
    assert recover(store)==[]
    assert [store.job(r['id']) for r in roots]==roots



def test_unfinished_synthesis_is_corrected_without_owner_input(setup):
    store, engine=setup
    parent=store.create_job('Triage this incoming Teams conversation. Complete the promised report','Communications')
    store.update(parent['id'],messages=json.dumps({'messages':[],'delivery_pending':True}))
    child=store.create_job('Research implementation','Technology',parent_id=parent['id'])
    store.complete_result(child['id'],{'result_kind':'strategy','deliverable_file_ids':[]},'Research notes',1)
    base={'context':'Meeting report','status_summary':'Research returned','next_step':'Produce report',
        'response_needed':'no','response_reason':'No reply requested','action_needed':'yes',
        'action_reason':'COS must produce the report','requested_action':'Complete methodology report'}
    engine.tools.execute(store.job(parent['id']),'update_brief',base)
    final={'summary':'# Methodology report\n\nThe completed analysis and evidence limits.', 'result_kind':'strategy','final_output':True}
    class DirectorScripted(Scripted):
        roles=[]
        def complete(self,messages,role='worker',tools=None):
            self.roles.append(role)
            return super().complete(messages,role,tools)
    engine.provider=DirectorScripted(perform('finish',final),
        perform('update_brief',{**base,'action_needed':'no','action_reason':'Report produced; optional reading remains','requested_action':'','next_step':'Read the completed report'}),
        perform('finish',final))
    engine.run(parent['id'])
    assert store.job(parent['id'])['status']=='completed'
    assert set(engine.provider.roles)=={'director'}
    assert not store.rows("SELECT * FROM events WHERE job_id=? AND kind='question'",(parent['id'],))
    assert len(store.result_review(parent['id'])['items'])==1
