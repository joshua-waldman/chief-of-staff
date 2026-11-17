import copy
import json
from pathlib import Path

import pytest

from cos import github, publication, publication_completion as completion
from cos.db import now
from cos.engine import Engine
from tests.test_concurrent_improvement import env, feature, git, patch, validate_tests


def packaged(env):
    _,store = env
    job,root = feature(store)
    patch(store,job,root,2)
    validate_tests(store,job,root)
    package = publication.prepare(store,job)
    manifest = store.state('publication:'+publication.digest(
        Path(store.one('SELECT path FROM files WHERE id=?',(package['file_id'],))['path']).read_bytes()))
    return store,job,root,package,manifest


def merged_github(monkeypatch, root, manifest, mutate=None):
    git(root,'add','-A')
    git(root,'commit','-m','Tested change')
    git(root,'remote','add','origin','https://github.com/example/app.git')
    head = git(root,'rev-parse','HEAD')
    blob = git(root,'rev-parse','HEAD:module.py')
    pr = {'merged':True,'merged_at':now(),'state':'closed','merge_commit_sha':'a'*40,
          'html_url':'https://github.com/example/app/pull/12',
          'base':{'ref':'main','repo':{'full_name':'example/app'}},
          'head':{'ref':manifest['branch'],'sha':head,'repo':{'full_name':'example/app'}}}
    tree = {'tree':[{'path':'module.py','type':'blob','sha':blob}],'truncated':False}
    if mutate:
        mutate(pr,tree)
    monkeypatch.setattr(github,'_account',lambda owner:'test-account')
    monkeypatch.setattr(github,'_token',lambda login:'private')
    def get(endpoint, login, token, params=None):
        if '/pulls/' in endpoint:
            return copy.deepcopy(pr)
        if '/git/trees/' in endpoint:
            return copy.deepcopy(tree)
        return {'default_branch':'main'}
    monkeypatch.setattr(github,'_get',get)


def test_report_cannot_close_unpublished_change_even_at_store_boundary(env):
    store,job,_,_,_ = packaged(env)
    with pytest.raises(ValueError,match='publication is incomplete'):
        store.complete_result(job['id'],{'final_output':True},'Publication prepared, not pushed.',1)
    assert store.job(job['id'])['status'] != 'completed'
    assert not store.one('SELECT 1 FROM result_reviews WHERE job_id=?',(job['id'],))


def test_coordinator_waits_for_child_and_retains_handoff_obligation(env):
    store,worker,_,package,_ = packaged(env)
    owner = store.create_job('Improve meetings','Self-Improvement')
    publisher = store.create_job('Publish the change','Self-Improvement',parent_id=owner['id'])
    store.write('UPDATE jobs SET parent_id=? WHERE id=?',(publisher['id'],worker['id']))
    with pytest.raises(ValueError,match='delegated work is still open'):
        store.complete_result(publisher['id'],{},'Report ready',1)
    store.complete_result(worker['id'],{'deliverable_file_ids':[package['file_id']]},'Package ready',1)
    with pytest.raises(ValueError,match='publication is incomplete'):
        store.complete_result(publisher['id'],{},'Prepared publication report',1)
    with pytest.raises(ValueError,match='delegated work is still open'):
        store.complete_result(owner['id'],{},'Owner report ready',1)


def test_verified_exact_merge_unlocks_completion_and_survives_restart(env,monkeypatch):
    store,job,root,package,manifest = packaged(env)
    merged_github(monkeypatch,root,manifest)
    receipt = completion.verify(store,job,package['file_id'],12)
    assert receipt['head'] == git(root,'rev-parse','HEAD')
    from cos.db import Store
    restarted = Store(store.path)
    restarted.complete_result(job['id'],{},'Published and merged',1)
    assert restarted.job(job['id'])['status'] == 'completed'


@pytest.mark.parametrize('kind',['open','wrong_branch','wrong_repo','wrong_commit','wrong_target','wrong_source','truncated'])
def test_unrelated_or_incomplete_remote_evidence_never_closes_job(env,monkeypatch,kind):
    store,job,root,package,manifest = packaged(env)
    def mutate(pr,tree):
        if kind == 'open': pr['merged'] = False
        if kind == 'wrong_branch': pr['head']['ref'] = 'unrelated'
        if kind == 'wrong_repo': pr['head']['repo']['full_name'] = 'other/app'
        if kind == 'wrong_commit': pr['head']['sha'] = 'b'*40
        if kind == 'wrong_target': pr['base']['ref'] = 'release'
        if kind == 'wrong_source': tree['tree'][0]['sha'] = 'c'*40
        if kind == 'truncated': tree['truncated'] = True
    merged_github(monkeypatch,root,manifest,mutate)
    with pytest.raises(ValueError):
        completion.verify(store,job,package['file_id'],12)
    with pytest.raises(ValueError,match='publication is incomplete'):
        store.complete_result(job['id'],{},'Done',1)


def test_new_edits_and_failed_remote_reads_do_not_reuse_old_merge(env,monkeypatch):
    store,job,root,package,manifest = packaged(env)
    merged_github(monkeypatch,root,manifest)
    monkeypatch.setattr(github,'_get',lambda *a,**kw:(_ for _ in ()).throw(ValueError('GitHub unavailable')))
    with pytest.raises(ValueError,match='unavailable'):
        completion.verify(store,job,package['file_id'],12)
    patch(store,job,root,3)
    with pytest.raises(ValueError,match='Source differs'):
        completion.verify(store,job,package['file_id'],12)
    validate_tests(store,job,root)
    publication.prepare(store,job)
    store.set_state('publication_merged:'+completion.identity(manifest),{'url':'old merge'})
    with pytest.raises(ValueError,match='publication is incomplete'):
        store.complete_result(job['id'],{},'Done',1)


def test_resurface_retains_saved_work_without_executing_actions(env):
    _,store = env
    job = store.create_job('Meeting artifact publication','Self-Improvement')
    store.complete_result(job['id'],{'final_output':True},'Old report',1)
    completion.resurface(store,job['id'],'Publication incomplete. Resume the saved package.')
    saved = store.job(job['id'])
    assert saved['status'] == 'waiting_input' and saved['result'] == 'Old report'
    assert 'final_output' not in json.loads(saved['messages'])
    assert not store.one('SELECT 1 FROM approvals WHERE job_id=?',(job['id'],))
    assert not store.one('SELECT 1 FROM result_reviews WHERE job_id=? AND eligible=1',(job['id'],))
    Engine(store).answer(job['id'],'Continue publication')
    assert store.job(job['id'])['status'] == 'queued'


def test_nonimplementation_self_improvement_report_can_finish(env):
    _,store = env
    job = store.create_job('Review possible improvements','Self-Improvement')
    store.complete_result(job['id'],{},'Research complete; no source changes',1)
    assert store.job(job['id'])['status'] == 'completed'


def test_fresh_integration_merge_satisfies_original_package_after_conflict_resolution(env,monkeypatch):
    live,store = env
    store,worker,_,old_package,old_manifest = packaged(env)
    owner = store.create_job('Owner change','Self-Improvement')
    store.write('UPDATE jobs SET parent_id=? WHERE id=?',(owner['id'],worker['id']))
    store.complete_result(worker['id'],{},'Original preparation returned',1)
    (live/'module.py').write_text('value = 3\n',encoding='utf-8')
    git(live,'add','.')
    git(live,'commit','-m','Concurrent change')
    integration,root = feature(store,'Integrate change')
    store.write('UPDATE jobs SET parent_id=? WHERE id=?',(owner['id'],integration['id']))
    file = store.one('SELECT * FROM files WHERE id=?',(old_package['file_id'],))
    attached = Engine(store).handoff_file(integration['id'],file,'inputs')
    result = publication.apply_package(store,integration,attached['file_id'])
    assert result['conflicts'] == ['module.py']
    patch(store,integration,root,4)
    validate_tests(store,integration,root)
    fresh = publication.prepare(store,integration)
    manifest = store.state('publication:'+publication.digest(Path(
        store.one('SELECT path FROM files WHERE id=?',(fresh['file_id'],))['path']).read_bytes()))
    assert completion.identity(old_manifest) in manifest['supersedes']
    merged_github(monkeypatch,root,manifest)
    completion.verify(store,integration,fresh['file_id'],12)
    store.complete_result(integration['id'],{},'Integrated and merged',1)
    store.complete_result(owner['id'],{},'All changes merged',1)
    assert store.job(owner['id'])['status'] == 'completed'
