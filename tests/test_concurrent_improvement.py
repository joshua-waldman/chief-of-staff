import hashlib
import json
import subprocess
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cos import config, engineering, tools, publication
from cos.db import Store, now, uid
from cos.engine import Engine
from cos.local_checks import ActionNotExecuted
from cos.policy import classify
from tests.test_workflows import Scripted, perform


@pytest.fixture
def env(tmp_path,monkeypatch):
    root = tmp_path/'app'
    root.mkdir()
    for module in (tools,engineering):
        monkeypatch.setattr(module,'ROOT',root)
    for module in (tools,config):
        monkeypatch.setattr(module,'DATA',tmp_path/'data')
    for module in (tools,engineering):
        monkeypatch.setattr(module,'settings',lambda:{'projects':{},'engineering_workflows':{}})
    subprocess.run(['git','init','-b','main',str(root)],check=True,capture_output=True)
    git(root,'config','user.name','COS Tests')
    git(root,'config','user.email','cos-tests@example.invalid')
    git(root,'config','core.autocrlf','false')
    (root/'module.py').write_text('value = 1\n',encoding='utf-8')
    (root/'tests').mkdir()
    (root/'tests/test_value.py').write_text('import unittest\nfrom module import value\nclass Check(unittest.TestCase):\n def test_value(self): self.assertGreater(value, 0)\n',encoding='utf-8')
    (root/'.gitignore').write_text('__pycache__/\n.pytest_cache/\n',encoding='utf-8')
    git(root,'add','.')
    git(root,'commit','-m','Initial source')
    return root, Store(tmp_path/'jobs.db')


def git(root,*args):
    return engineering.git(root,*args)


def feature(store,name='Improve value'):
    job = store.create_job(name,'Self-Improvement')
    args = {'name':name}
    args['branch'] = engineering.branch_name(job,args)
    saved = engineering.create_branch(job,args)
    return job,Path(saved['path'])


def patch(store,job,root,value):
    source = tools.workspace(job['id'])/'module.py'
    source.write_text('value = '+str(value)+'\n',encoding='utf-8')
    args = {'changes':[{'source':'module.py','target':str(root/'module.py'),
                        'expected_sha256':hashlib.sha256((root/'module.py').read_bytes()).hexdigest()}]}
    args['_review_diffs'] = tools.prepare_changes(job,args)
    tools.Tools(store).execute(job,'apply_changes',args)


def validate_tests(store,job,root):
    args = {'command':['python','-B','-m','unittest','discover','-s','tests'],'cwd':str(root)}
    result = tools.Tools(store).execute(job,'run_command',args)
    store.write('INSERT INTO approvals(id,job_id,tool,args,status,created,result) VALUES(?,?,?,?,?,?,?)',
                (uid(),job['id'],'run_command',json.dumps(args),'executed',now(),json.dumps(result)))
    return result


def test_parallel_features_never_share_files_or_switch_running_checkout(env):
    live,store = env
    baseline = git(live,'rev-parse','HEAD')
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(feature,store,'Same feature name') for _ in range(2)]
        first,second = [f.result() for f in futures]
    assert first[1]!=second[1]
    patch(store,*first,2)
    patch(store,*second,3)
    assert (first[1]/'module.py').read_text()=='value = 2\n'
    assert (second[1]/'module.py').read_text()=='value = 3\n'
    assert (live/'module.py').read_text()=='value = 1\n'
    assert git(live,'branch','--show-current')=='main'
    assert git(live,'rev-parse','HEAD')==baseline
    assert validate_tests(store,*first)['exit_code']==0
    assert validate_tests(store,*second)['exit_code']==0


def test_explicit_remote_main_base_without_mutating_shared_checkout(env, tmp_path):
    live,store = env
    remote = tmp_path/'remote.git'
    subprocess.run(['git','init','--bare',str(remote)],check=True,capture_output=True)
    git(live,'remote','add','origin',str(remote))
    git(live,'push','-u','origin','main')
    old = git(live,'rev-parse','HEAD')
    git(live,'checkout','-b','advanced')
    (live/'module.py').write_text('value = 2\n',encoding='utf-8')
    git(live,'add','module.py')
    git(live,'commit','-m','Advance remote')
    newer = git(live,'rev-parse','HEAD')
    git(live,'push','origin','HEAD:main')
    git(live,'checkout','main')
    assert git(live,'rev-parse','HEAD') == old
    assert git(live,'rev-parse','refs/remotes/origin/main') == newer
    job = store.create_job('Fresh base', 'Self-Improvement')
    args = {'name':'Fresh base','base_ref':'origin/main','base_commit':newer}
    args['branch'] = engineering.branch_name(job,args)
    result = engineering.create_branch(job,args)
    checkout = Path(result['path'])
    assert result['base_commit'] == newer
    assert git(checkout,'rev-parse','HEAD') == newer
    assert git(live,'rev-parse','HEAD') == old
    assert git(live,'branch','--show-current') == 'main'
    assert engineering.create_branch(job,args)['resumed']
    with pytest.raises(ValueError,match='another base'):
        engineering.create_branch(job,{**args,'base_ref':''})


def test_fresh_base_refuses_stale_or_arbitrary_commit(env, tmp_path):
    live,store = env
    remote = tmp_path/'remote.git'
    subprocess.run(['git','init','--bare',str(remote)],check=True,capture_output=True)
    git(live,'remote','add','origin',str(remote))
    git(live,'push','-u','origin','main')
    old = git(live,'rev-parse','HEAD')
    job = store.create_job('Reject stale base','Self-Improvement')
    args = {'name':'Reject stale base','base_ref':'origin/main','base_commit':'0'*40}
    args['branch'] = engineering.branch_name(job,args)
    with pytest.raises(ValueError,match='Local origin/main differs'):
        engineering.create_branch(job,args)
    with pytest.raises(ValueError,match='requires Self-Improvement'):
        engineering.create_branch(job,{**args,'base_ref':'origin/other','base_commit':old})
    assert not engineering.workspace_metadata(job).exists()
    assert not engineering.workspace_metadata(job).with_suffix('.pending.json').exists()


def test_restart_resumes_owned_worktree_and_cannot_repurpose_job(env):
    _,store = env
    job,root = feature(store)
    patch(store,job,root,2)
    args = {'name':'Improve value'}
    args['branch'] = engineering.branch_name(job,args)
    resumed = engineering.create_branch(store.job(job['id']),args)
    assert resumed['resumed'] and Path(resumed['path'])==root
    assert (root/'module.py').read_text()=='value = 2\n'
    other = {'name':'Other feature'}
    other['branch'] = engineering.branch_name(job,other)
    with pytest.raises(ValueError,match='another feature'):
        engineering.create_branch(job,other)


def test_interrupted_worktree_registration_recovers_without_recreating_branch(env):
    _,store=env
    job,root=feature(store)
    patch(store,job,root,2)
    metadata=engineering.workspace_metadata(job)
    metadata.replace(metadata.with_suffix('.pending.json'))
    saved=engineering.checkout(store.job(job['id']))
    assert Path(saved['path'])==root and metadata.is_file()
    assert (root/'module.py').read_text()=='value = 2\n'


def test_reserved_branch_survives_stop_before_worktree_creation(env):
    live,store=env
    job,root=feature(store)
    saved=engineering.checkout(job)
    git(live,'worktree','remove',str(root))
    metadata=engineering.workspace_metadata(job)
    metadata.replace(metadata.with_suffix('.pending.json'))
    args={'name':'Improve value','branch':saved['branch']}
    recovered=engineering.create_branch(job,args)
    assert recovered['base_commit']==saved['base_commit']
    assert git(root,'rev-parse','HEAD')==saved['base_commit']


def test_existing_unowned_feature_branch_is_never_reused(env):
    live,store=env
    job=store.create_job('Feature','Self-Improvement')
    args={'name':'Feature'}
    args['branch']=engineering.branch_name(job,args)
    git(live,'branch',args['branch'])
    with pytest.raises(ValueError,match='unowned branch'):
        engineering.create_branch(job,args)
    assert not engineering.workspace_metadata(job).exists()
    assert not engineering.workspace_metadata(job).with_suffix('.pending.json').exists()


def test_live_source_and_other_job_workspaces_cannot_be_patched(env):
    live,store = env
    job,root = feature(store)
    other,other_root = feature(store,'Other')
    with pytest.raises(ValueError,match='isolated checkout'):
        patch(store,job,live,2)
    with pytest.raises(ValueError,match='private'):
        patch(store,job,other_root,2)
    with pytest.raises(ActionNotExecuted,match='running application'):
        tools.Tools(store).execute(job,'run_command',{'command':['python','-m','pytest','tests'],'cwd':str(live)})


def test_package_contains_original_new_deleted_and_binary_files_and_real_test_results(env):
    live,store = env
    job,root = feature(store)
    patch(store,job,root,2)
    (root/'new.bin').write_bytes(b'\x00\xffbinary')
    (root/'.gitignore').unlink()
    # Keep test caches outside the package when the ignore file is deleted.
    git(root,'config','core.excludesFile',str(live/'.gitignore'))
    result = validate_tests(store,job,root)
    assert result['exit_code']==0 and result['validation']['unchanged']
    package = publication.prepare(store,job)
    assert package['validated']
    row = store.one('SELECT * FROM files WHERE id=?',(package['file_id'],))
    with zipfile.ZipFile(row['path']) as archive:
        assert archive.read('original/module.py')==publication.git_bytes(live,'show','HEAD:module.py')
        assert archive.read('proposed/module.py')==(root/'module.py').read_bytes()
        assert archive.read('proposed/new.bin')==b'\x00\xffbinary'
        assert 'original/.gitignore' in archive.namelist()
        assert 'proposed/.gitignore' not in archive.namelist()
        assert json.loads(archive.read('manifest.json'))['tests'][0]['result']['exit_code']==0
    assert publication.validate(store,job,package['file_id'])['ready_for_approval']
    # Staging and committing exactly these bytes must preserve validation,
    # including the deleted file's tombstone in the tree fingerprint.
    git(root,'add','-A')
    publication.guard_command(store,job,{'command':['git','commit','-m','Feature'],
                             'cwd':str(root),'publication_file_id':package['file_id']})
    git(root,'commit','-m','Feature')
    publication.guard_command(store,job,{'command':['git','push','origin',engineering.checkout(job)['branch']],
                             'cwd':str(root),'publication_file_id':package['file_id']})


def test_stale_tests_and_failed_checks_do_not_validate_package(env):
    _,store = env
    job,root = feature(store)
    assert validate_tests(store,job,root)['exit_code']==0
    patch(store,job,root,2)
    assert not publication.prepare(store,job)['validated']
    patch(store,job,root,-1)
    assert validate_tests(store,job,root)['exit_code']!=0
    package = publication.prepare(store,job)
    assert not package['validated']
    assert not publication.validate(store,job,package['file_id'])['ready_for_approval']


def test_changed_base_requires_fresh_integration_and_tests(env):
    live,store = env
    job,root = feature(store)
    patch(store,job,root,2)
    validate_tests(store,job,root)
    package = publication.prepare(store,job)
    (live/'other.py').write_text('concurrent_feature = True\n',encoding='utf-8')
    git(live,'add','.')
    git(live,'commit','-m','Concurrent feature')
    check = publication.validate(store,job,package['file_id'])
    assert check['base_changed'] and not check['ready_for_approval']
    integration,target = feature(store,'Integrate approved scope')
    file = store.one('SELECT * FROM files WHERE id=?',(package['file_id'],))
    copied = Engine(store).handoff_file(integration['id'],file,'inputs')
    result = publication.apply_package(store,integration,copied['file_id'])
    assert result['applied'] and not result['conflicts']
    assert (target/'module.py').read_text()=='value = 2\n'
    assert (target/'other.py').is_file()
    assert not publication.prepare(store,integration)['validated']
    validate_tests(store,integration,target)
    fresh = publication.prepare(store,integration)
    assert publication.validate(store,integration,fresh['file_id'])['ready_for_approval']
    assert (live/'module.py').read_text()=='value = 1\n'


def test_conflicts_are_retained_in_integration_worktree(env):
    live,store = env
    job,root = feature(store)
    patch(store,job,root,2)
    package = publication.prepare(store,job)
    (live/'module.py').write_text('value = 3\n',encoding='utf-8')
    git(live,'add','.')
    git(live,'commit','-m','Overlapping feature')
    integration,target = feature(store,'Integrate')
    file = store.one('SELECT * FROM files WHERE id=?',(package['file_id'],))
    copied = Engine(store).handoff_file(integration['id'],file,'inputs')
    result = publication.apply_package(store,integration,copied['file_id'])
    assert not result['applied'] and result['conflicts']==['module.py']
    assert (live/'module.py').read_text()=='value = 3\n'
    assert (root/'module.py').read_text()=='value = 2\n'
    with pytest.raises(ActionNotExecuted,match='fresh, clean'):
        publication.apply_package(store,integration,copied['file_id'])
    with pytest.raises(ValueError,match='Resolve integration conflicts'):
        publication.prepare(store,integration)
    patch(store,integration,target,4)
    assert git(target,'diff','--name-only','--diff-filter=U')==''
    validate_tests(store,integration,target)
    fresh=publication.prepare(store,integration)
    assert publication.validate(store,integration,fresh['file_id'])['ready_for_approval']


def test_repository_write_is_bound_to_tested_bytes_at_execution(env):
    _,store = env
    job,root = feature(store)
    patch(store,job,root,2)
    validate_tests(store,job,root)
    package = publication.prepare(store,job)
    args = {'command':['git','commit','-am','Feature'],'cwd':str(root),'publication_file_id':package['file_id']}
    assert classify('run_command',args,'Self-Improvement').approval
    publication.guard_command(store,job,args)
    patch(store,job,root,3)
    with pytest.raises(ActionNotExecuted,match='differs'):
        publication.guard_command(store,job,args)
    with pytest.raises(ActionNotExecuted,match='publication_file_id'):
        publication.guard_command(store,job,{**args,'publication_file_id':None})


def test_retained_inputs_recover_original_bytes_without_cross_task_access(env):
    _,store = env
    parent = store.create_job('Owner task','Self-Improvement')
    source = store.create_job('Make source','Engineering',parent_id=parent['id'])
    path = tools.workspace(source['id'])/'source.txt'
    path.write_bytes(b'original evidence')
    fid = tools.file_record(store,source['id'],path)['file_id']
    target = store.create_job('Publish /api/files/'+fid,'Engineering',parent_id=parent['id'])
    path.write_bytes(b'changed later')
    result = tools.Tools(store).execute(target,'recover_inputs',{'file_ids':[fid]})
    local = result['recovered'][0]['file_id']
    assert tools.Tools(store).execute(target,'read_file',{'path':local})['text']=='original evidence'
    unrelated = store.create_job('Read /api/files/'+fid,'Engineering')
    with pytest.raises(ValueError,match='Unrelated jobs'):
        tools.Tools(store).execute(unrelated,'recover_inputs',{'file_ids':[fid]})
    path.unlink()
    returned = Engine(store).handoff_file(parent['id'],store.one('SELECT * FROM files WHERE id=?',(fid,)),'delegated')
    assert Path(returned['path']).read_bytes()==b'original evidence'


def test_recovery_can_select_an_older_registered_artifact_version(env):
    _,store = env
    parent=store.create_job('Owner task','Self-Improvement')
    path=tools.workspace(parent['id'])/'source.txt'
    path.write_bytes(b'approved snapshot')
    original=tools.file_record(store,parent['id'],path)
    expected=hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_bytes(b'newer snapshot')
    assert tools.file_record(store,parent['id'],path)['file_id']==original['file_id']
    child=store.create_job('Read /api/files/'+original['file_id'],'Engineering',parent_id=parent['id'])
    result=tools.Tools(store).execute(child,'recover_inputs',{'file_ids':[original['file_id']],
                        'expected_sha256':{original['file_id']:expected}})
    assert tools.Tools(store).execute(child,'read_file',{'path':result['recovered'][0]['file_id']})['text']=='approved snapshot'


def test_lost_artifact_bytes_do_not_silently_substitute_new_content(env):
    _,store=env
    job=store.create_job('Owner task','Self-Improvement')
    path=tools.workspace(job['id'])/'source.txt'
    path.write_bytes(b'original')
    fid=tools.file_record(store,job['id'],path)['file_id']
    sha=hashlib.sha256(b'original').hexdigest()
    (tools.workspace(job['id']).parent/'artifacts'/sha).unlink()
    path.write_bytes(b'changed')
    child=store.create_job('Read /api/files/'+fid,'Engineering',parent_id=job['id'])
    with pytest.raises(ValueError,match='rebuild and test|reconstruct and validate'):
        tools.Tools(store).execute(child,'recover_inputs',{'file_ids':[fid]})


def test_question_recovers_accessible_cos_inputs_before_contacting_owner(env):
    _,store=env
    parent=store.create_job('Owner task','Self-Improvement')
    path=tools.workspace(parent['id'])/'source.txt'
    path.write_bytes(b'original evidence')
    fid=tools.file_record(store,parent['id'],path)['file_id']
    child=store.create_job('Publish /api/files/'+fid,'Engineering',parent_id=parent['id'])
    path.unlink()
    engine=Engine(store,Scripted(
        perform('ask_user',{'question':'Could you reattach the original source files?'}),
        perform('finish',{'summary':'Recovered source available.'})))
    try:
        engine.run(child['id'])
        assert store.job(child['id'])['status']=='completed'
        assert tools.local_artifact(store,child['id'],fid)
        assert not store.rows("SELECT * FROM events WHERE job_id=? AND kind='question'",(child['id'],))
    finally:
        engine.stop()


def test_recovery_repairs_missing_local_attachment_as_a_verified_copy(env):
    _,store=env
    job=store.create_job('Owner task','Self-Improvement')
    path=tools.workspace(job['id'])/'source.txt'
    path.write_bytes(b'original')
    fid=tools.file_record(store,job['id'],path)['file_id']
    path.unlink()
    result=tools.Tools(store).execute(job,'recover_inputs',{'file_ids':[fid]})
    assert tools.Tools(store).execute(job,'read_file',{'path':result['recovered'][0]['file_id']})['text']=='original'
