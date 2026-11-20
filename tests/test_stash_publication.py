import copy
import hashlib
import json
import subprocess
from pathlib import Path

import httpx
import pytest

from cos import engineering, publication, publication_completion, repository_publication, stash, tools
from cos.action_verification import target as action_target
from cos.engine import Engine
from cos.local_checks import ActionNotExecuted
from cos.policy import classify
from tests.test_concurrent_improvement import env, git, patch, validate_tests
from tests.test_workflows import Scripted, perform, run

URL = 'https://stash.amd.com/projects/ATE/repos/app'
CLONE = 'https://stash.amd.com/scm/ATE/app.git'


@pytest.fixture
def prepared(env,monkeypatch,tmp_path):
    live,store = env
    remote = tmp_path/'upstream.git'
    subprocess.run(['git','clone','--bare',str(live),str(remote)],check=True,capture_output=True)
    git(live,'remote','add','origin',CLONE)
    base = git(live,'rev-parse','HEAD')
    monkeypatch.setattr(stash,'settings',lambda:{'stash_servers':{
        'amd':{'url':'https://stash.amd.com','projects':{'ATE':'work-login'}}}})
    monkeypatch.setattr(stash,'token_for',lambda *args:'private-stash-token')
    state = {'base':base,'remote_head':None,'pr':None,'posts':[],'git_writes':[],'files':{}}
    def api(method,url,**kwargs):
        assert kwargs['headers']['Authorization']=='Bearer private-stash-token'
        assert kwargs['follow_redirects'] is False
        if method=='POST':
            state['posts'].append((url,copy.deepcopy(kwargs)))
            if url.endswith('/merge'):
                assert kwargs['params']['version']==state['pr']['version']
                state['pr']['state']='MERGED'
                state['pr']['closedDate']=123456789
                state['pr']['properties']={'mergeCommit':{'id':state['remote_head']}}
                data = state['pr']
            else:
                body = kwargs['json']
                state['pr'] = {**copy.deepcopy(body),'id':12,'version':0,'state':'OPEN'}
                state['pr']['fromRef']['latestCommit']=state['remote_head']
                data = state['pr']
        elif url.endswith('/branches/default'):
            data = {'id':'refs/heads/main','latestCommit':state['base']}
        elif url.endswith('/commits'):
            assert kwargs['params']['until'].startswith('refs/heads/')
            data = {'values':[{'id':state['remote_head']}]}
        elif '/raw/' in url:
            name = url.split('/raw/',1)[1]
            if name not in state['files']:
                return httpx.Response(404)
            return httpx.Response(200,content=state['files'][name])
        elif url.endswith('/pull-requests'):
            data = {'isLastPage':True,'values':[state['pr']] if state['pr'] else []}
        else:
            assert url.endswith('/pull-requests/12')
            data = state['pr']
        return httpx.Response(200,json=copy.deepcopy(data))
    monkeypatch.setattr(stash.httpx,'request',api)
    original = subprocess.run
    def transport(command,**kwargs):
        if 'push' in command or 'fetch' in command:
            assert 'private-stash-token' not in str(command)
            assert kwargs['env']['GIT_TERMINAL_PROMPT']=='0'
            assert kwargs['env']['GIT_CONFIG_VALUE_4']=='Authorization: Bearer private-stash-token'
            assert kwargs['env']['GIT_CONFIG_KEY_4']=='http.'+CLONE+'.extraHeader'
            state['git_writes'].append(command[:])
            command = [str(remote) if part==CLONE else part for part in command]
        result = original(command,**kwargs)
        if 'push' in command and result.returncode==0:
            state['remote_head']=git(Path(command[2]),'rev-parse','HEAD')
        return result
    monkeypatch.setattr(engineering.subprocess,'run',transport)
    job = store.create_job('Update ATE source','Self-Improvement')
    args = {'name':'ATE feature','base':'remote_default'}
    args['branch']=engineering.branch_name(job,args)
    saved = engineering.create_branch(job,args)
    root = Path(saved['path'])
    assert saved['remote_repository']==URL and saved['base_commit']==base
    assert git(live,'branch','--show-current')=='main'
    patch(store,job,root,2)
    validate_tests(store,job,root)
    package = publication.prepare(store,job)
    assert package['validated']
    git(root,'add','-A')
    git(root,'commit','-m','Tested ATE change')
    state['files']['module.py']=(root/'module.py').read_bytes()
    return store,job,root,package,state,live,remote


def publish(prepared,operation,**kwargs):
    store,job,_,package,*_ = prepared
    args = repository_publication.prepare(store,job,{'file_id':package['file_id'],'operation':operation,**kwargs})
    return tools.Tools(store).execute(job,'repository_publish',args)


def test_real_git_push_pr_merge_and_verified_tested_source(prepared):
    store,job,root,package,state,live,remote = prepared
    before = git(live,'show-ref')
    pushed = publish(prepared,'push')
    assert pushed['commit']==git(root,'rev-parse','HEAD')
    assert git(remote,'rev-parse','refs/heads/'+pushed['branch'])==pushed['commit']
    assert git(live,'show-ref')==before and git(live,'branch','--show-current')=='main'
    created = publish(prepared,'create_pr',title='Update ATE source',description='Validated source changes.')
    assert created['pr_number']==12 and created['state']=='OPEN'
    assert state['posts'][0][1]['json']['toRef']['id']=='refs/heads/main'
    assert publish(prepared,'merge',pr_number=12)['state']=='MERGED'
    verified = publication_completion.verify(store,job,package['file_id'],12)
    assert verified['merge_commit']==pushed['commit'] and verified['provider']=='stash'
    assert 'private-stash-token' not in json.dumps([pushed,created,verified])
    assert len(state['git_writes'])==1 and len(state['posts'])==2
    store.complete_result(job['id'],{},'Published and merged',1)
    assert store.job(job['id'])['status']=='completed'


@pytest.mark.parametrize('change',['default','source','origin','pr_version'])
def test_stale_approval_fails_before_any_write(prepared,change):
    store,job,root,package,state,*_ = prepared
    publish(prepared,'push')
    publish(prepared,'create_pr',title='ATE change')
    args = repository_publication.prepare(store,job,{
        'file_id':package['file_id'],'operation':'merge','pr_number':12})
    if change=='default': state['base']='f'*40
    if change=='source': (root/'module.py').write_text('value = 999\n')
    if change=='origin': git(root,'remote','set-url','origin',CLONE.replace('/app.git','/other.git'))
    if change=='pr_version': state['pr']['version']+=1
    posts = len(state['posts'])
    with pytest.raises(ActionNotExecuted):
        tools.Tools(store).execute(job,'repository_publish',args)
    assert len(state['posts'])==posts


def test_merge_rejects_unrelated_pr_and_failed_content(prepared):
    store,job,root,package,state,*_ = prepared
    publish(prepared,'push')
    publish(prepared,'create_pr',title='ATE change')
    state['pr']['fromRef']['latestCommit']='b'*40
    with pytest.raises(ValueError,match='exact tested'):
        publish(prepared,'merge',pr_number=12)
    state['pr']['fromRef']['latestCommit']=git(root,'rev-parse','HEAD')
    publish(prepared,'merge',pr_number=12)
    state['files']['module.py']=b'wrong bytes'
    with pytest.raises(ValueError,match='Merged source'):
        publication_completion.verify(store,job,package['file_id'],12)
    assert not store.state('publication_merged:nonexistent',None)
    with pytest.raises(ValueError,match='publication is incomplete'):
        store.complete_result(job['id'],{},'Done',1)


def test_unknown_outcomes_use_read_only_reconciliation(prepared):
    store,job,_,package,state,*_ = prepared
    push = repository_publication.prepare(store,job,{'file_id':package['file_id'],'operation':'push'})
    tools.Tools(store).execute(job,'repository_publish',push)
    assert repository_publication.verify_action(push)['outcome']=='executed'
    create = repository_publication.prepare(store,job,{
        'file_id':package['file_id'],'operation':'create_pr','title':'ATE change'})
    tools.Tools(store).execute(job,'repository_publish',create)
    before = len(state['posts'])
    assert repository_publication.verify_action(create)['evidence']['pr_number']==12
    assert len(state['posts'])==before
    changed = copy.deepcopy(create)
    changed['_repository_preview']['head']='f'*40
    assert action_target('repository_publish',changed)==action_target('repository_publish',create)


def test_engine_prepares_exact_approval_without_publishing(prepared):
    store,job,root,package,state,*_ = prepared
    engine = Engine(store,Scripted(perform('repository_publish',{
        'file_id':package['file_id'],'operation':'push',
        '_repository_preview':{'repository':'https://evil.test'}})))
    assert run(engine,job)['status']=='awaiting_approval'
    approval = store.one("SELECT * FROM approvals WHERE job_id=? AND tool='repository_publish'",(job['id'],))
    args = json.loads(approval['args'])
    assert args['_repository_preview']['repository']==URL
    assert args['_repository_preview']['head']==git(root,'rev-parse','HEAD')
    assert not state['git_writes'] and not state['posts']
    engine.decide(approval['id'],True)
    assert store.one('SELECT status FROM approvals WHERE id=?',(approval['id'],))['status']=='executed'
    assert len(state['git_writes'])==1


def test_publish_privileges_are_never_granted_to_source_readers():
    for org in ('Engineering','Self-Improvement'):
        assert classify('repository_publish',{},org=org).approval
        job = {'org':org,'messages':json.dumps({'source_only':True})}
        assert not tools.allowed_job_action(job,'repository_publish')
    assert not tools.allowed_job_action({'org':'Technology','messages':'{}'},'repository_publish')


def test_stash_fetch_acquires_missing_pinned_commit_without_moving_shared_refs(env,monkeypatch,tmp_path):
    live,_ = env
    upstream = tmp_path/'remote-source'
    subprocess.run(['git','clone',str(live),str(upstream)],check=True,capture_output=True)
    git(upstream,'config','user.name','Test')
    git(upstream,'config','user.email','test@example.invalid')
    (upstream/'remote-only.py').write_text('value = 3\n')
    git(upstream,'add','.')
    git(upstream,'commit','-m','Remote default advanced')
    sha = git(upstream,'rev-parse','HEAD')
    git(live,'remote','add','origin','ssh://git@stash.amd.com:7999/ATE/app.git')
    before = git(live,'show-ref')
    monkeypatch.setattr(stash,'settings',lambda:{'stash_servers':{
        'amd':{'url':'https://stash.amd.com','projects':{'ATE':'work-login'}}}})
    monkeypatch.setattr(stash,'token_for',lambda *args:'private-stash-token')
    monkeypatch.setattr(stash.httpx,'request',lambda *args,**kwargs:
        httpx.Response(200,json={'id':'refs/heads/main','latestCommit':sha}))
    original = subprocess.run
    calls = []
    def fetch(command,**kwargs):
        if 'fetch' in command:
            calls.append(command[:])
            assert '--no-write-fetch-head' in command and 'private-stash-token' not in str(command)
            assert kwargs['env']['GIT_CONFIG_VALUE_4']=='Authorization: Bearer private-stash-token'
            command = [str(upstream) if part==CLONE else part for part in command]
        return original(command,**kwargs)
    monkeypatch.setattr(engineering.subprocess,'run',fetch)
    repository,base = engineering.remote_default_commit(live)
    assert repository==URL and base==sha
    engineering.acquire_remote_commit(live,repository,base)
    assert git(live,'rev-parse',sha+'^{commit}')==sha
    assert git(live,'show-ref')==before and not (live/'.git/FETCH_HEAD').exists()
    engineering.acquire_remote_commit(live,repository,base)
    assert len(calls)==1
