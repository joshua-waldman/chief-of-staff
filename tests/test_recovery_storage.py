import hashlib
import json
import zipfile
from pathlib import Path
import pytest
from cos import config,tools,server
from cos.context import save_text
from cos.db import Store
from cos.engine import Engine
from cos.recovery_storage import prune_recovery,archived_bytes,archive_key,BUNDLE


@pytest.fixture
def env(tmp_path,monkeypatch):
    monkeypatch.setattr(tools,'DATA',tmp_path/'data')
    monkeypatch.setattr(config,'DATA',tmp_path/'data')
    return Store(tmp_path/'test.db')


def snapshots(store,count=6):
    job=store.create_job('Research','Technology')
    originals={}
    last=None
    for index in range(count):
        history=save_text(store,job['id'],json.dumps([{'role':'tool','content':str(index)+' evidence '*1000}]),'history')
        checkpoint=save_text(store,job['id'],json.dumps({'history_path':history,'previous':last,'step':index}),'checkpoint')
        last=checkpoint
        originals[history]=Path(history).read_bytes()
        originals[checkpoint]=Path(checkpoint).read_bytes()
    content=save_text(store,job['id'],'source '*3000+'final detail','content')
    originals[content]=Path(content).read_bytes()
    image=b'\x89PNG synthetic image bytes'
    path=tools.workspace(job['id'])/'context'/('image-'+hashlib.sha256(image).hexdigest()[:20]+'.png')
    path.write_bytes(image)
    tools.file_record(store,job['id'],path)
    originals[str(path)]=image
    store.update(job['id'],status='completed',messages=json.dumps({'checkpoint_path':last,'messages':[]}))
    return job,originals


def test_pruning_is_lossless_and_old_paths_still_support_text_and_images(env):
    job,originals=snapshots(env)
    files_before=env.rows('SELECT * FROM files WHERE job_id=?',(job['id'],))
    preview=prune_recovery(env,job['id'])
    assert preview['files']==10 and not preview['applied']
    assert all(Path(path).exists() for path in originals)
    result=prune_recovery(env,job['id'],apply=True)
    assert result['removed_files']==10 and result['archived_files']==10
    assert result['archive_bytes']<result['loose_bytes']
    manifest=env.state(archive_key(job['id']))['members']
    for path,raw in originals.items():
        if Path(path).exists():
            assert Path(path).read_bytes()==raw
        else:
            assert archived_bytes(env,job['id'],path)==raw
    assert len(env.detail(job['id'])['files'])==5 # two histories, two checkpoints, one bundle
    assert env.detail(job['id'])['archived_recovery_count']==10
    assert len(env.rows('SELECT * FROM files WHERE job_id=?',(job['id'],)))==len(files_before)+1
    content=next(path for path in originals if Path(path).name.startswith('content-'))
    response=tools.Tools(env).execute(job,'read_file',{'path':content,'offset':20,'limit':100})
    assert response['text']==originals[content].decode()[20:120]
    assert response['next_offset']==120
    assert response['source']==content
    assert response['sha256']==hashlib.sha256(originals[content]).hexdigest()
    image=next(path for path in originals if Path(path).suffix=='.png')
    import base64
    assert base64.b64decode(tools.Tools(env).execute(job,'read_file',{'path':image})['image_data'])==originals[image]
    assert not Path(content).exists() and not Path(image).exists()
    assert manifest


def test_original_download_links_survive_and_auth_is_required(env,monkeypatch):
    from fastapi.testclient import TestClient
    job,originals=snapshots(env)
    records=env.rows('SELECT * FROM files WHERE job_id=?',(job['id'],))
    prune_recovery(env,job['id'],apply=True)
    monkeypatch.setattr(server,'store',env)
    client=TestClient(server.app)
    url='/api/files/'+records[0]['id']
    assert client.get(url).status_code==401
    client.get('/')
    for row in records:
        response=client.get('/api/files/'+row['id'])
        assert response.status_code==200
        assert response.content==originals[row['path']]
        assert 'attachment' in response.headers['content-disposition']
    bundle=next(row for row in env.detail(job['id'])['files'] if row['name']==BUNDLE)
    assert client.get('/api/files/'+bundle['id']).content==Path(bundle['path']).read_bytes()


def test_repeated_pruning_preserves_previous_members_and_stable_file_id(env):
    job,originals=snapshots(env)
    prune_recovery(env,job['id'],apply=True)
    bundle_id=next(row['id'] for row in env.detail(job['id'])['files'] if row['name']==BUNDLE)
    for index in range(4):
        path=save_text(env,job['id'],'new evidence '+str(index),'history')
        originals[path]=Path(path).read_bytes()
    assert prune_recovery(env,job['id'],apply=True)['removed_files']>0
    assert next(row['id'] for row in env.detail(job['id'])['files'] if row['name']==BUNDLE)==bundle_id
    for path,raw in originals.items():
        assert (Path(path).read_bytes() if Path(path).exists() else archived_bytes(env,job['id'],path))==raw
    assert prune_recovery(env,job['id'],apply=True)['files']==0


@pytest.mark.parametrize('status',['running','queued'])
def test_active_jobs_are_skipped(env,status):
    job,originals=snapshots(env)
    env.update(job['id'],status=status)
    assert prune_recovery(env,job['id'],apply=True)['skipped']
    assert all(Path(path).exists() for path in originals)


def test_deliverables_unregistered_files_and_other_jobs_are_untouched(env):
    job,originals=snapshots(env)
    root=tools.workspace(job['id'])
    untouched=[]
    for name in ['history-report.txt','context/notes.txt','context/history-'+'a'*20+'.txt']:
        path=root/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('Deliverable',encoding='utf-8')
        if 'notes' in name or 'report' in name:
            tools.file_record(env,job['id'],path)
        untouched.append(path)
    other,other_originals=snapshots(env)
    prune_recovery(env,job['id'],apply=True)
    assert all(path.exists() for path in untouched)
    assert all(Path(path).exists() for path in other_originals)
    path=next(path for path in originals if not Path(path).exists())
    assert archived_bytes(env,other['id'],path) is None
    with pytest.raises(ValueError,match='private'):
        tools.Tools(env).execute(other,'read_file',{'path':path})


def test_failed_publication_keeps_every_loose_original(env,monkeypatch):
    job,originals=snapshots(env)
    original_replace=Path.replace
    def fail_replace(path,target):
        if Path(target).name==BUNDLE:
            raise OSError('Disk unavailable')
        return original_replace(path,target)
    monkeypatch.setattr(Path,'replace',fail_replace)
    with pytest.raises(OSError):
        prune_recovery(env,job['id'],apply=True)
    assert all(Path(path).read_bytes()==raw for path,raw in originals.items())
    assert not env.state(archive_key(job['id']))
    assert not list((tools.workspace(job['id'])/'context').glob('*.tmp'))


def test_corrupt_existing_bundle_stops_cleanup(env):
    job,originals=snapshots(env)
    prune_recovery(env,job['id'],apply=True)
    new=save_text(env,job['id'],'new source','content')
    bundle=tools.workspace(job['id'])/'context'/BUNDLE
    bundle.write_bytes(b'corrupt zip')
    with pytest.raises(zipfile.BadZipFile):
        prune_recovery(env,job['id'],apply=True)
    assert Path(new).read_text()=='new source'
    path=next(path for path in originals if not Path(path).exists())
    with pytest.raises(zipfile.BadZipFile):
        archived_bytes(env,job['id'],path)


def test_valid_zip_with_changed_bytes_is_rejected(env):
    job,originals=snapshots(env)
    prune_recovery(env,job['id'],apply=True)
    members=env.state(archive_key(job['id']))['members']
    name=next(iter(members))
    bundle=tools.workspace(job['id'])/'context'/BUNDLE
    with zipfile.ZipFile(bundle,'w') as output:
        output.writestr(name,b'x'*members[name]['size'])
    with pytest.raises(ValueError,match='checksum'):
        archived_bytes(env,job['id'],bundle.parent/name)


def test_interrupted_deletion_is_recoverable_and_retried(env,monkeypatch):
    job,originals=snapshots(env)
    unlink=Path.unlink
    def fail_unlink(path,*args,**kwargs):
        if str(path) in originals:
            raise OSError('File in use')
        return unlink(path,*args,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path,'unlink',fail_unlink)
        with pytest.raises(OSError):
            prune_recovery(env,job['id'],apply=True)
    assert env.state(archive_key(job['id']))['members']
    assert all(Path(path).exists() for path in originals)
    assert prune_recovery(env,job['id'],apply=True)['removed_files']==10
    for path,raw in originals.items():
        assert (Path(path).read_bytes() if Path(path).exists() else archived_bytes(env,job['id'],path))==raw


def test_automatic_cleanup_and_maintenance_failure_never_replay_actions(env,monkeypatch):
    job,originals=snapshots(env,count=10)
    class Finish:
        def complete(self,*args,**kwargs):
            return {'role':'assistant','content':None,'tool_calls':[{'id':'done','type':'function',
              'function':{'name':'perform','arguments':json.dumps({'action':'finish','arguments':{'summary':'Done'}})}}]},{}
    env.update(job['id'],status='queued')
    Engine(env,Finish()).run(job['id'])
    assert env.job(job['id'])['status']=='completed'
    assert env.state(archive_key(job['id']))['members']
    from cos import recovery_storage
    monkeypatch.setattr(recovery_storage,'prune_recovery',lambda *args,**kwargs:(_ for _ in ()).throw(OSError('Disk unavailable')))
    env.update(job['id'],status='queued')
    Engine(env,Finish()).run(job['id'])
    assert env.job(job['id'])['status']=='completed'
    assert env.job(job['id'])['result']=='Done'


def test_archived_reads_keep_input_size_limit(env):
    job,originals=snapshots(env)
    prune_recovery(env,job['id'],apply=True)
    content=next(path for path in originals if Path(path).name.startswith('content-'))
    with pytest.raises(ValueError,match='25 MB'):
        archived_bytes(env,job['id'],content,max_size=5)


def test_database_commit_failure_leaves_loose_originals_and_retry_works(env,monkeypatch):
    import sqlite3
    job,originals=snapshots(env)
    class CommitFailure(sqlite3.Connection):
        def commit(self):
            raise sqlite3.OperationalError('Simulated index commit failure')
    def broken_connect():
        connection=sqlite3.connect(env.path,factory=CommitFailure)
        connection.row_factory=sqlite3.Row
        return connection
    with monkeypatch.context() as patch:
        patch.setattr(env,'connect',broken_connect)
        with pytest.raises(sqlite3.OperationalError):
            prune_recovery(env,job['id'],apply=True)
    assert all(Path(path).read_bytes()==raw for path,raw in originals.items())
    assert not env.state(archive_key(job['id']))
    assert prune_recovery(env,job['id'],apply=True)['removed_files']==10


def test_nested_recovery_files_still_download(env,monkeypatch):
    from fastapi.testclient import TestClient
    job=env.create_job('Research','Technology')
    path=tools.workspace(job['id'])/'context'/'nested'/'notes.txt'
    path.parent.mkdir(parents=True)
    path.write_text('Nested evidence',encoding='utf-8')
    file=tools.file_record(env,job['id'],path)
    monkeypatch.setattr(server,'store',env)
    client=TestClient(server.app)
    client.get('/')
    response=client.get(file['url'])
    assert response.status_code==200 and response.text=='Nested evidence'


def test_unrelated_bundle_is_never_overwritten(env):
    job,originals=snapshots(env)
    path=tools.workspace(job['id'])/'context'/BUNDLE
    with zipfile.ZipFile(path,'w') as output:
        output.writestr('report.txt','Owner report')
    before=path.read_bytes()
    with pytest.raises(ValueError,match='unrelated'):
        prune_recovery(env,job['id'],apply=True)
    assert path.read_bytes()==before
    assert all(Path(path).exists() for path in originals)


def test_preview_does_not_create_workspace_for_jobs_without_files(env):
    job=env.create_job('No artifacts','Technology')
    env.update(job['id'],status='completed')
    root=tools.DATA/'jobs'/job['id']/'workspace'
    assert not root.exists()
    assert prune_recovery(env,job['id'])['files']==0
    assert not root.exists()


def test_file_edited_after_archiving_is_not_deleted_or_hidden_on_read(env):
    job,originals=snapshots(env)
    prune_recovery(env,job['id'],apply=True)
    content=next(path for path in originals if Path(path).name.startswith('content-'))
    Path(content).write_text('Edited evidence',encoding='utf-8')
    assert tools.Tools(env).execute(job,'read_file',{'path':content})['text']=='Edited evidence'
    with pytest.raises(ValueError,match='changed'):
        prune_recovery(env,job['id'],apply=True)
    assert Path(content).read_text()=='Edited evidence'


def test_compression_does_not_hold_write_lock_and_resumed_job_defers_cleanup(env,monkeypatch):
    job,originals=snapshots(env)
    original_write=zipfile.ZipFile.write
    changed=False
    def write_then_resume(bundle,*args,**kwargs):
        nonlocal changed
        if not changed:
            # Another Store has its own lock/connection, as another process would.
            other=Store(env.path)
            other.update(job['id'],status='queued')
            changed=True
        return original_write(bundle,*args,**kwargs)
    monkeypatch.setattr(zipfile.ZipFile,'write',write_then_resume)
    result=prune_recovery(env,job['id'],apply=True)
    assert result['skipped'] and not result['applied']
    assert all(Path(path).read_bytes()==raw for path,raw in originals.items())
    assert not env.state(archive_key(job['id']))


def test_changed_index_during_compression_is_not_overwritten(env,monkeypatch):
    job,originals=snapshots(env)
    original_write=zipfile.ZipFile.write
    changed=False
    def write_then_change(bundle,*args,**kwargs):
        nonlocal changed
        if not changed:
            env.set_state(archive_key(job['id']),{'members':{},'revision':'concurrent cleanup'})
            changed=True
        return original_write(bundle,*args,**kwargs)
    monkeypatch.setattr(zipfile.ZipFile,'write',write_then_change)
    result=prune_recovery(env,job['id'],apply=True)
    assert result['skipped'] and not result['applied']
    assert env.state(archive_key(job['id']))['revision']=='concurrent cleanup'
    assert all(Path(path).exists() for path in originals)
