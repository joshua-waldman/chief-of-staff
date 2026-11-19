import io
import json
from pathlib import Path
import sqlite3
import subprocess
import os
import sys
import threading
import zipfile

import pytest
from fastapi.testclient import TestClient

from cos import updates, update_gate, update_worker, server


@pytest.fixture
def updater(monkeypatch, tmp_path):
    monkeypatch.setattr(updates, 'ROOT', tmp_path)
    monkeypatch.setattr(updates, 'DATA', tmp_path/'data')
    monkeypatch.setattr(updates, 'installed', lambda: {'tag':'v1.0.0','sha':'a'*40,'kind':'zip'})
    monkeypatch.setattr(update_gate, 'pending', False)
    return updates.Updates()


def response(tag='v1.1.0', **extra):
    return {'tag_name':tag,'body':'Release notes',**extra}


def test_stable_check_pins_commit_and_never_downgrades(updater, monkeypatch):
    calls = []
    def get(endpoint):
        calls.append(endpoint)
        return response() if endpoint == '/releases/latest' else {'sha':'b'*40}
    monkeypatch.setattr(updates, 'get_json', get)
    state = updater.check()
    assert state['state'] == 'available'
    assert state['release']['sha'] == 'b'*40
    assert calls == ['/releases/latest','/commits/v1.1.0']
    assert updater.remind()['reminded']
    monkeypatch.setattr(updates, 'get_json', lambda endpoint: response('v0.9.0') if endpoint == '/releases/latest' else {'sha':'c'*40})
    assert updater.check()['state'] == 'current'


@pytest.mark.parametrize('release', [response('v1.1.0-rc.1'),response(prerelease=True),response(draft=True)])
def test_preview_and_draft_releases_are_rejected(updater, monkeypatch, release):
    monkeypatch.setattr(updates, 'get_json', lambda _:release)
    assert updater.check()['state'] == 'error'


def test_network_failure_and_repeated_checks(updater, monkeypatch):
    def fail(_):
        raise ValueError('Offline')
    monkeypatch.setattr(updates,'get_json',fail)
    assert updater.check()['error'] == 'Offline'
    updater.state['state'] = 'installing'
    assert updater.check()['state'] == 'installing'


def test_release_archive_cannot_replace_runtime_or_escape(tmp_path):
    data=io.BytesIO()
    with zipfile.ZipFile(data,'w') as archive:
        for name in ('run.py','requirements.txt','cos/server.py','Start-ChiefOfStaff.ps1',
                     'data/chief.sqlite3','config.local.json','../outside.py','cos/../../outside.py','cos\\bad.py'):
            archive.writestr('source/'+name,'safe')
    updates.stage_archive(data.getvalue(),tmp_path/'stage')
    assert (tmp_path/'stage/cos/server.py').read_text() == 'safe'
    assert not (tmp_path/'stage/data').exists()
    assert not (tmp_path/'stage/config.local.json').exists()
    assert not (tmp_path/'outside.py').exists()


def test_duplicate_archive_paths_rejected(tmp_path):
    data=io.BytesIO()
    with zipfile.ZipFile(data,'w') as archive:
        archive.writestr('source/cos/server.py','a')
        archive.writestr('source/cos/SERVER.py','b')
    with pytest.raises(ValueError,match='duplicate'):
        updates.stage_archive(data.getvalue(),tmp_path)


def test_maintenance_barrier_drains_existing_work_and_blocks_dispatch(monkeypatch):
    monkeypatch.setattr(update_gate,'pending',False)
    monkeypatch.setattr(update_gate,'active',0)
    ran=[]
    @update_gate.background
    def work():
        ran.append(True)
        assert update_gate.active == 1
    work()
    monkeypatch.setattr(update_gate,'pending',True)
    work()
    assert ran == [True] and update_gate.active == 0
    with pytest.raises(RuntimeError):
        monkeypatch.setattr(update_gate,'pending',False)
        @update_gate.background
        def fail():
            raise RuntimeError()
        fail()
    assert update_gate.active == 0


def test_update_endpoints_require_session_token_and_exact_release(updater, monkeypatch):
    monkeypatch.setattr(server,'updates',updater)
    client=TestClient(server.app)
    assert client.get('/api/updates').status_code == 401
    client.get('/')
    assert client.post('/api/updates/install',json={}).status_code == 403
    headers={'x-cos-token':server.TOKEN}
    assert client.post('/api/updates/install',json={},headers=headers).status_code == 400
    assert client.post('/api/updates/install',json={'acknowledgment':'update_and_restart','tag':'v9.9.9','sha':'f'*40},headers=headers).status_code == 400
    monkeypatch.setattr(update_gate,'pending',True)
    assert client.post('/api/settings',json={'queue_paused':False},headers=headers).status_code == 503
    assert client.get('/api/updates').status_code == 200


@pytest.fixture
def installation(tmp_path, monkeypatch):
    root, stage, work=tmp_path/'app',tmp_path/'stage',tmp_path/'work'
    root.mkdir();stage.mkdir();work.mkdir()
    (root/'run.py').write_text('old')
    (stage/'run.py').write_text('new')
    (root/'requirements.txt').write_text('old-dependency')
    (stage/'requirements.txt').write_text('new-dependency')
    (root/'config.local.json').write_text('private configuration')
    database=root/'private.sqlite3'
    with sqlite3.connect(database) as conn:
        conn.execute('CREATE TABLE saved(value TEXT)')
        conn.execute("INSERT INTO saved VALUES('history')")
    import hashlib
    baseline={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in ('run.py','requirements.txt')}
    plan={'root':str(root),'stage':str(stage),'database':str(database),'python':'python',
          'port':8765,'baseline':baseline,'current':{'kind':'zip','tag':'v1.0.0','sha':'a'*40},
          'release':{'tag':'v1.1.0','sha':'b'*40}}
    actions=[]
    monkeypatch.setattr(update_worker,'task',lambda root,action:actions.append(action))
    monkeypatch.setattr(update_worker,'run',lambda *args,**kwargs:'')
    monkeypatch.setattr(update_worker,'health',lambda *args:None)
    import socket
    class Socket:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def connect_ex(self,*args): return 1
        def settimeout(self,*args): pass
    monkeypatch.setattr(socket,'socket',Socket)
    return root,work,plan,actions


def test_detached_update_preserves_data_and_tests_startup_before_activation(installation,monkeypatch):
    root,work,plan,actions=installation
    trials=[]
    def health(port,sha,trial):
        trials.append(trial)
        assert (root/'.cos-update-trial').exists() == trial
    monkeypatch.setattr(update_worker,'health',health)
    assert update_worker.install(plan,work) == 'complete'
    assert (root/'run.py').read_text() == 'new'
    assert (root/'config.local.json').read_text() == 'private configuration'
    assert (work/'database.sqlite3').exists()
    assert trials == [True,False]
    assert actions == ['inspect','stop','start','stop','start']


def test_failed_startup_rolls_back_code_and_database(installation,monkeypatch):
    root,work,plan,actions=installation
    def fail(*args):
        with sqlite3.connect(plan['database']) as conn:
            conn.execute('DELETE FROM saved')
        raise RuntimeError('Startup failed')
    monkeypatch.setattr(update_worker,'health',fail)
    with pytest.raises(RuntimeError,match='Startup failed'):
        update_worker.install(plan,work)
    assert (root/'run.py').read_text() == 'old'
    assert not (root/'.cos-update-trial').exists()
    with sqlite3.connect(plan['database']) as conn:
        assert conn.execute('SELECT value FROM saved').fetchone()[0] == 'history'
    assert actions[-1] == 'start'


def test_source_edit_aborts_before_stopping_app(installation):
    root,work,plan,actions=installation
    (root/'run.py').write_text('edited')
    with pytest.raises(RuntimeError,match='changed'):
        update_worker.install(plan,work)
    assert actions == ['inspect']


def test_failure_after_activation_never_rolls_back_live_history(installation,monkeypatch):
    root,work,plan,actions=installation
    def health(port,sha,trial):
        if not trial:
            raise RuntimeError('Restart failed')
    monkeypatch.setattr(update_worker,'health',health)
    with pytest.raises(RuntimeError,match='may have resumed'):
        update_worker.install(plan,work)
    assert (root/'run.py').read_text() == 'new'


def test_update_helper_failure_releases_barrier(updater,tmp_path,monkeypatch):
    updater.work=tmp_path
    updater.state['state']='installing'
    (tmp_path/'result.json').write_text(json.dumps({'state':'error','error':'Task preflight failed'}))
    monkeypatch.setattr(update_gate,'pending',True)
    assert updater.snapshot()['state']=='error'
    assert not update_gate.pending


@pytest.mark.parametrize('branch,dirty', [('codex/feature',''),('main',' M cos/server.py')])
def test_update_refuses_feature_branch_or_local_changes(updater,monkeypatch,branch,dirty):
    updater.state.update(state='available',release={'tag':'v1.1.0','sha':'b'*40})
    monkeypatch.setattr(updates,'installed',lambda:{'kind':'git','sha':'a'*40,'tag':'v1.0.0'})
    def git(*args):
        if args[0]=='remote':return 'https://github.com/'+updates.REPOSITORY+'.git'
        if args[0]=='branch':return branch
        return dirty
    monkeypatch.setattr(updates,'git',git)
    with pytest.raises(ValueError,match='clean main/release'):
        updater.request('v1.1.0','b'*40,None,8765)
    assert not update_gate.pending


def test_release_tag_move_is_rejected_before_stopping(installation,monkeypatch):
    root,work,plan,actions=installation
    plan['current']['kind']='git'
    def run(args,**kwargs):
        if args[0]!='git':return ''
        operation=args[3:]
        if operation==['rev-parse','HEAD']:return 'a'*40
        if operation==['branch','--show-current']:return 'main'
        if operation[0]=='rev-parse':return 'c'*40
        return ''
    monkeypatch.setattr(update_worker,'run',run)
    with pytest.raises(RuntimeError,match='tag changed'):
        update_worker.install(plan,work)
    assert actions==['inspect']


@pytest.mark.parametrize('startup_fails',[False,True])
@pytest.mark.parametrize('on_main',[False,True])
def test_real_git_checkout_and_rollback_preserve_branch_and_private_files(tmp_path,monkeypatch,startup_fails,on_main):
    root,work,stage=tmp_path/'repo',tmp_path/'attempt',tmp_path/'stage'
    root.mkdir();work.mkdir();stage.mkdir()
    def git(*args):
        return subprocess.check_output(['git','-C',str(root),*args],stderr=subprocess.DEVNULL).decode().strip()
    git('init','-b','main');git('config','user.email','owner@example.com');git('config','user.name','Test Owner')
    (root/'.gitignore').write_text('*\n!/.gitignore\n!/run.py\n!/requirements.txt\n')
    (root/'run.py').write_text('old');(root/'requirements.txt').write_text('')
    git('add','.gitignore','run.py','requirements.txt');git('commit','-m','Initial')
    old=git('rev-parse','HEAD')
    (root/'run.py').write_text('new');git('commit','-am','Release');git('tag','v1.1.0')
    new=git('rev-parse','HEAD');git('checkout','--detach',old)
    if on_main:
        git('branch','-f','main',old)
        git('checkout','main')
    (root/'config.local.json').write_text('private')
    import hashlib,socket
    baseline={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in ('run.py','requirements.txt','.gitignore')}
    plan={'root':str(root),'stage':str(stage),'database':str(root/'private.sqlite3'),'python':'python',
          'port':8765,'baseline':baseline,'current':{'kind':'git','sha':old,'tag':'v1.0.0'},'release':{'tag':'v1.1.0','sha':new}}
    monkeypatch.setattr(update_worker,'task',lambda *args:None)
    original_run=update_worker.run
    monkeypatch.setattr(update_worker,'run',lambda args,**kwargs:'' if args[0]=='python' or args[0]=='git' and args[3]=='fetch' else original_run(args,**kwargs))
    class Socket:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def connect_ex(self,*args):return 1
        def settimeout(self,*args):pass
    monkeypatch.setattr(socket,'socket',Socket)
    def health(*args):
        if startup_fails:raise RuntimeError('Startup failure')
    monkeypatch.setattr(update_worker,'health',health)
    if startup_fails:
        with pytest.raises(RuntimeError,match='Startup failure'):update_worker.install(plan,work)
    else:
        assert update_worker.install(plan,work)=='complete'
    assert git('rev-parse','HEAD')==(old if startup_fails else new)
    assert git('rev-parse','main')==(old if on_main else new)
    assert git('branch','--show-current')==('main' if startup_fails and on_main else '')
    assert (root/'config.local.json').read_text()=='private'


def test_reminder_survives_app_restart(updater):
    updater.remind()
    assert updates.Updates().snapshot()['reminded']


def test_preparation_waits_for_workers_and_actions_before_launching_helper(updater,tmp_path,monkeypatch):
    from types import SimpleNamespace
    (tmp_path/'cos').mkdir()
    (tmp_path/'cos/update_worker.py').write_text('# copied helper')
    archive=io.BytesIO()
    with zipfile.ZipFile(archive,'w') as z:
        for name in ('run.py','requirements.txt','cos/server.py','Start-ChiefOfStaff.ps1'):
            z.writestr('release/'+name,'new source')
    class Stream:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def raise_for_status(self):pass
        def iter_bytes(self):yield archive.getvalue()
    monkeypatch.setattr(updates.httpx,'stream',lambda *args,**kwargs:Stream())
    monkeypatch.setattr(updates,'headers',lambda:{})
    active={'worker'}
    store=SimpleNamespace(path=str(tmp_path/'db.sqlite3'),one=lambda *_:None)
    engine=SimpleNamespace(active=active,verifying=set(),lock=threading.RLock(),store=store)
    waits=[]
    class Event:
        def wait(self,seconds):
            assert update_gate.pending
            waits.append(seconds);active.clear();return False
    updater.stop_event=Event()
    launches=[]
    def launch(args,**kwargs):
        assert not active and update_gate.pending
        launches.append((args,kwargs))
        return SimpleNamespace(poll=lambda:None)
    monkeypatch.setattr(updates.subprocess,'Popen',launch)
    release={'tag':'v1.1.0','sha':'b'*40}
    updater.prepare(release,{'kind':'zip','sha':'a'*40},engine,8765)
    assert updater.snapshot()['state']=='installing'
    assert waits==[1] and len(launches)==1
    plan=json.loads((updater.work/'plan.json').read_text())
    assert plan['release']==release and plan['port']==8765
    assert (updater.work/'source/cos/server.py').read_text()=='new source'


def test_prepare_failure_never_leaves_maintenance_enabled(updater,monkeypatch):
    def fail():raise RuntimeError('No download access')
    monkeypatch.setattr(updates,'tree_digest',fail)
    updater.prepare({'tag':'v1.1.0','sha':'b'*40},{},None,8765)
    assert updater.snapshot()['state']=='error'
    assert not update_gate.pending


def test_fresh_release_zip_is_recognized_without_a_version_marker(updater,tmp_path,monkeypatch):
    import hashlib
    (tmp_path/'run.py').write_bytes(b'release source\n')
    blob=b'release source\n'
    oid=hashlib.sha1(b'blob '+str(len(blob)).encode()+b'\0'+blob).hexdigest()
    monkeypatch.setattr(updates,'installed',lambda:{'kind':'zip','tag':None,'sha':None})
    def get(endpoint):
        if endpoint=='/releases/latest':return response()
        if endpoint.startswith('/commits/'):return {'sha':'b'*40}
        return {'tree':[{'path':'run.py','sha':oid,'type':'blob'}]}
    monkeypatch.setattr(updates,'get_json',get)
    assert updater.check()['state']=='current'
    (tmp_path/'run.py').write_bytes(b'older source\n')
    assert updater.check()['state']=='available'


@pytest.mark.skipif(os.name!='nt',reason='Windows process identity API')
def test_windows_shutdown_verifies_creation_time_before_stopping_exact_child():
    child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        identity=update_worker.process_identity(child.pid)
        assert identity['created']
        with pytest.raises(RuntimeError,match='PID was reused'):
            update_worker.terminate_server({**identity,'created':identity['created']+1})
        assert child.poll() is None
        update_worker.terminate_server(identity)
        assert child.wait(timeout=10)==0
    finally:
        if child.poll() is None:child.terminate();child.wait(timeout=10)


def test_failed_trial_uses_boot_identity_when_health_never_succeeds(installation,monkeypatch):
    root,work,plan,actions=installation
    identity={'pid':12345,'created':67890}
    stopped=[]
    monkeypatch.setattr(update_worker,'terminate_server',lambda item:stopped.append(item))
    def fail(*args):
        (work/'trial-process.json').write_text(json.dumps(identity))
        raise RuntimeError('Trial startup failed')
    monkeypatch.setattr(update_worker,'health',fail)
    with pytest.raises(RuntimeError,match='Trial startup failed'):
        update_worker.install(plan,work)
    assert identity in stopped
    assert (root/'run.py').read_text()=='old'


@pytest.mark.parametrize('valid',[True,False])
def test_trial_boot_records_identity_only_inside_update_directory(tmp_path,monkeypatch,valid):
    import runpy
    from types import SimpleNamespace
    from cos import config
    monkeypatch.setattr(config,'DATA',tmp_path/'data')
    identity_file=tmp_path/('data/updates/attempt' if valid else 'elsewhere')/'trial-process.json'
    identity_file.parent.mkdir(parents=True)
    (tmp_path/'.cos-update-trial').write_text(json.dumps({'identity_file':str(identity_file)}))
    runs=[]
    monkeypatch.setitem(sys.modules,'uvicorn',SimpleNamespace(run=lambda *args,**kwargs:runs.append(kwargs)))
    monkeypatch.setattr(sys,'argv',['run.py'])
    script=Path(__file__).resolve().parents[1]/'run.py'
    if valid:
        runpy.run_path(str(script),run_name='__main__')
        assert json.loads(identity_file.read_text())['pid']==os.getpid()
        assert runs[0]['host']=='127.0.0.1'
    else:
        with pytest.raises(RuntimeError,match='Invalid update trial'):
            runpy.run_path(str(script),run_name='__main__')
        assert not identity_file.exists() and not runs
