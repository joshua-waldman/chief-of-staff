"""File handoffs must survive long install paths and isolate failed parents."""
import hashlib
import json
from pathlib import Path
import pytest

from cos import tools
from cos.engine import Engine,session
from cos.tools import file_record,workspace,Tools
from test_context import env


def waiting_parent(store,with_file=True):
    parent=store.create_job('Combine child results','Technology')
    child=store.create_job('Produce findings','Technology',parent_id=parent['id'])
    ids=[]
    if with_file:
        path=workspace(child['id'])/'roadmap_owner_review_stage_tasks.docx'
        path.write_bytes(b'verified deliverable')
        ids.append(file_record(store,child['id'],path)['file_id'])
    store.update(child['id'],status='completed',result='Verified findings',
                 messages=json.dumps({'messages':[],'deliverable_file_ids':ids}))
    store.update(parent['id'],status='waiting_children',
                 messages=json.dumps({'messages':[],'waiting_call':'original-delegate-call'}))
    return parent,child,ids


def test_compact_handoffs_preserve_names_bytes_and_provenance(env,tmp_path,monkeypatch):
    # Reproduce a long installation directory without requiring Windows long
    # path support to create the test fixture itself.
    data=tmp_path/('a'*max(1,125-len(str(tmp_path))))
    monkeypatch.setattr(tools,'DATA',data)
    parent,child,ids=waiting_parent(env)
    source=env.one('SELECT * FROM files WHERE id=?',(ids[0],))
    legacy=workspace(parent['id'])/'delegated'/child['id']/ids[0]/source['name']
    assert len(str(legacy))>=260
    engine=Engine(env)
    engine.release_parents()
    assert env.job(parent['id'])['status']=='queued'
    result=json.loads(session(env.job(parent['id']))['messages'][-1]['content'])
    transferred=result[0]['files'][0]
    assert len(transferred['path'])<260
    assert transferred['name']==source['name']
    assert Path(transferred['path']).read_bytes()==b'verified deliverable'
    digest=env.one('SELECT sha256 FROM artifact_provenance WHERE file_id=?',(transferred['file_id'],))
    assert digest['sha256']==hashlib.sha256(b'verified deliverable').hexdigest()
    # Repeating a transfer reuses its file record; forwarding it to another
    # child retains its display name and format despite its compact disk name.
    again=engine.handoff_file(parent['id'],source,'delegated')
    assert again['file_id']==transferred['file_id']
    engine.delegate(parent,{'tasks':[{'org':'Technology','request':'Read returned file','file_ids':[again['file_id']]}]})
    forwarded=env.one('SELECT * FROM files WHERE job_id IN (SELECT id FROM jobs WHERE parent_id=? AND id!=?)',
                      (parent['id'],child['id']))
    assert forwarded['name']==source['name'] and forwarded['mime']==source['mime']
    assert len(forwarded['path'])<260
    assert Path(forwarded['path']).read_bytes()==b'verified deliverable'


def test_failed_handoff_does_not_block_other_parents_or_dispatch_and_can_retry(env,monkeypatch):
    broken,child,ids=waiting_parent(env)
    healthy,_,_=waiting_parent(env,with_file=False)
    queued=env.create_job('Unrelated queued work','Technology')
    engine=Engine(env)
    real_copy=__import__('shutil').copy2
    def fail_copy(*args,**kwargs):
        raise OSError('Simulated destination failure')
    monkeypatch.setattr('cos.engine.shutil.copy2',fail_copy)
    engine.release_parents()
    assert env.job(broken['id'])['status']=='blocked'
    saved=session(env.job(broken['id']))
    assert saved['waiting_call']=='original-delegate-call'
    assert 'Simulated destination failure' in saved['handoff_error']
    assert env.job(healthy['id'])['status']=='queued'
    assert queued['id'] in {j['id'] for j in engine.dispatch_jobs()}
    assert env.job(child['id'])['status']=='completed'
    assert Path(env.one('SELECT path FROM files WHERE id=?',(ids[0],))['path']).exists()
    engine.release_parents()
    assert len(env.rows("SELECT * FROM events WHERE job_id=? AND kind='handoff_failed'",(broken['id'],)))==1
    monkeypatch.setattr('cos.engine.shutil.copy2',real_copy)
    engine.answer(broken['id'],'Retry the saved handoff')
    assert env.job(broken['id'])['status']=='waiting_children'
    engine.release_parents()
    recovered=env.job(broken['id'])
    assert recovered['status']=='queued'
    receipt=session(recovered)['messages'][-1]
    assert receipt['tool_call_id']=='original-delegate-call'
    assert json.loads(receipt['content'])[0]['files'][0]['name'].endswith('.docx')


def test_missing_child_file_is_isolated(env):
    broken,_,ids=waiting_parent(env)
    healthy,_,_=waiting_parent(env,with_file=False)
    Path(env.one('SELECT path FROM files WHERE id=?',(ids[0],))['path']).unlink()
    Engine(env).release_parents()
    assert env.job(broken['id'])['status']=='blocked'
    assert env.job(healthy['id'])['status']=='queued'


def test_revision_requires_selected_file_and_rewrites_old_deliverable_link(env):
    parent,source,ids=waiting_parent(env)
    engine=Engine(env)
    engine.release_parents()
    local=env.one('SELECT * FROM files WHERE job_id=?',(parent['id'],))
    request='Revise /api/files/'+ids[0]
    count=len(env.rows('SELECT id FROM jobs'))
    with pytest.raises(ValueError,match='Include local file_id '+local['id']):
        engine.delegate(parent,{'tasks':[{'org':'Technology','request':request}]})
    assert len(env.rows('SELECT id FROM jobs'))==count
    engine.delegate(parent,{'tasks':[{'org':'Technology','request':request,'file_ids':[local['id']]}]})
    child=env.one('SELECT * FROM jobs WHERE parent_id=? AND id<>?',(parent['id'],source['id']))
    transferred=env.one('SELECT * FROM files WHERE job_id=?',(child['id'],))
    assert child['request']=='Revise /api/files/'+transferred['id']
    # Use plain text bytes for a real extraction through the tool.
    p=workspace(child['id'])/'notes.txt'
    p.write_text('Source-grounded tasks',encoding='utf-8')
    fid=file_record(env,child['id'],p)['file_id']
    for ref in [fid,'/api/files/'+fid]:
        assert Tools(env).execute(child,'read_file',{'path':ref})['text']=='Source-grounded tasks'


def test_artifact_references_do_not_grant_cross_job_access(env):
    source=env.create_job('Private source','Technology')
    p=workspace(source['id'])/'source.txt'
    p.write_text('Private source bytes',encoding='utf-8')
    fid=file_record(env,source['id'],p)['file_id']
    unrelated=env.create_job('Unrelated task','Technology')
    with pytest.raises(ValueError,match='not attached'):
        Tools(env).execute(unrelated,'read_file',{'path':'/api/files/'+fid})
    with pytest.raises(ValueError,match='Attach the source'):
        Engine(env).delegate(unrelated,{'tasks':[{'org':'Technology','request':'Read /api/files/'+fid}]})
    assert not env.rows('SELECT id FROM jobs WHERE parent_id=?',(unrelated['id'],))


def test_old_links_resolve_only_after_a_copy_is_attached(env):
    source=env.create_job('Produce source','Technology')
    p=workspace(source['id'])/'source.txt'
    p.write_text('Verified original content',encoding='utf-8')
    fid=file_record(env,source['id'],p)['file_id']
    target=env.create_job('Read returned document','Technology')
    file=env.one('SELECT * FROM files WHERE id=?',(fid,))
    Engine(env).handoff_file(target['id'],file,'inputs')
    p.unlink()  # Reading must use the local copy, never the source workspace.
    assert Tools(env).execute(target,'read_file',{'path':'/api/files/'+fid})['text']=='Verified original content'
    assert Tools(env).execute(target,'read_file',{'path':fid})['text']=='Verified original content'
