"""Immutable publication packages, bound to a Git base and actual test receipts."""
import hashlib
import json
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

from .db import uid, now
from .engineering import checkout, git
from .processes import background_options


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def git_bytes(root, *args, env=None):
    result = subprocess.run(['git','-C',str(root),*args],capture_output=True,timeout=60,env=env,**background_options())
    if result.returncode:
        raise ValueError('Cannot capture repository snapshot: '+result.stderr.decode('utf-8',errors='replace')[:1000])
    return result.stdout


def snapshot(job):
    from .tools import BLOCKED_PARTS, SECRET_NAMES
    saved = checkout(job)
    if not saved:
        raise ValueError('Create an isolated feature branch before capturing or testing source changes.')
    root = Path(saved['path'])
    listed = git_bytes(root,'ls-files','-z','--cached','--others','--exclude-standard')
    listed += git_bytes(root,'ls-tree','-r','--name-only','-z',saved['base_commit'])
    paths = sorted(set(listed.decode('utf-8').rstrip('\0').split('\0'))-{''})
    modes = {}
    for entry in git_bytes(root,'ls-files','--stage','-z').decode('utf-8').rstrip('\0').split('\0'):
        if entry:
            identity,name=entry.split('\t',1)
            mode,_,stage=identity.split()
            if stage!='0':
                raise ValueError('Resolve integration conflicts with staged apply_changes before testing or packaging: '+name)
            modes[name]=mode
    files = {}
    total = 0
    for name in paths:
        relative = PurePosixPath(name)
        path = root.joinpath(*relative.parts)
        if (relative.is_absolute() or '..' in relative.parts or not path.resolve().is_relative_to(root)
                or path.is_symlink() or set(relative.parts)&BLOCKED_PARTS
                or path.name.lower() in SECRET_NAMES or path.name.startswith('.env')):
            raise ValueError('Repository snapshot contains a private or unsupported path: '+name)
        if path.exists() and not path.is_file():
            raise ValueError('Submodules and non-file snapshot entries require a separate publication workflow: '+name)
        raw = path.read_bytes() if path.exists() else None
        total += len(raw) if raw is not None else 0
        if total>64*1024*1024:
            raise ValueError('Repository snapshot exceeds 64 MB.')
        executable=bool(path.stat().st_mode&0o111) if raw is not None else False
        mode=modes.get(name,'100755' if executable else '100644') if os.name=='nt' else ('100755' if executable else '100644')
        files[name] = {'sha256':digest(raw),'mode':mode} if raw is not None else None
    identity = {'head':git(root,'rev-parse','HEAD'),'files':files}
    return {**saved, 'tree_sha256':digest(json.dumps(files,sort_keys=True).encode()), **identity}


def prepare(store, job):
    from .tools import workspace, file_record
    before = snapshot(job)
    root = Path(before['path'])
    # An alternate index captures new/deleted/binary files without touching the
    # agent's real index, branch or working files.
    with tempfile.TemporaryDirectory(dir=workspace(job['id']).parent) as temporary:
        env = {**os.environ,'GIT_INDEX_FILE':str(Path(temporary)/'index')}
        git_bytes(root,'read-tree',before['base_commit'],env=env)
        git_bytes(root,'add','-A','--','.',env=env)
        patch = git_bytes(root,'diff','--cached','--binary','--full-index',before['base_commit'],'--',env=env)
        names = git_bytes(root,'diff','--cached','--name-only','-z',before['base_commit'],'--',env=env).decode('utf-8').rstrip('\0').split('\0')
    if not patch:
        raise ValueError('No changes to package against this job base.')
    validations = {}
    for receipt in store.rows("SELECT id,args,result FROM approvals WHERE job_id=? AND tool='run_command' AND status='executed' ORDER BY created,id",(job['id'],)):
        result = json.loads(receipt['result'])
        validation = result.get('validation',{})
        if validation.get('tree_sha256')==before['tree_sha256']:
            args = json.loads(receipt['args'])
            validations[json.dumps(args.get('command'))] = {'receipt_id':receipt['id'],'command':args.get('command'), 'result':result}
    tests = list(validations.values())
    manifest = {'version':1,'created':now(),'job_id':job['id'],
                'source':before['source'],'branch':before['branch'],'base_commit':before['base_commit'],
                'head':before['head'],'tree_sha256':before['tree_sha256'],
                'patch_sha256':digest(patch),'files':[], 'tests':tests,
                'validated':bool(tests) and all(t['result']['exit_code']==0 and t['result']['validation']['unchanged'] for t in tests)}
    target = workspace(job['id'])/'publication'/('publication-'+uid()+'.zip')
    target.parent.mkdir(parents=True,exist_ok=True)
    try:
        with zipfile.ZipFile(target,'w',compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('changes.patch',patch)
            for name in names:
                path = root/name
                exists = git_bytes(root,'ls-tree','-z',before['base_commit'],'--',name)
                old = git_bytes(root,'show',before['base_commit']+':'+name) if exists else None
                new = path.read_bytes() if path.exists() else None
                entry = {'path':name, 'before_sha256':digest(old) if old is not None else None,
                         'after_sha256':digest(new) if new is not None else None}
                manifest['files'].append(entry)
                if old is not None: archive.writestr('original/'+name,old)
                if new is not None: archive.writestr('proposed/'+name,new)
            archive.writestr('manifest.json',json.dumps(manifest,indent=2))
        if snapshot(job)['tree_sha256']!=before['tree_sha256']:
            raise ValueError('Source changed while packaging; capture and validate again.')
        raw = target.read_bytes()
        result = file_record(store,job['id'],target)
        store.set_state('publication:'+digest(raw),manifest)
        return {**result, 'base_commit':before['base_commit'],'tree_sha256':before['tree_sha256'],
                'validated':manifest['validated'],'test_receipt_ids':[t['receipt_id'] for t in tests],
                'instruction':'Transfer this entire package as an explicit file input. Use validate_publication before proposing any repository write. Publication and integration require fresh exact-action approval.'}
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def validate(store, job, file_id):
    from .tools import local_artifact
    from .artifact_inputs import verified_source
    file = local_artifact(store,job['id'],file_id)
    if not file:
        raise ValueError('Publication package must be attached to this job.')
    raw = verified_source(store,file).read_bytes()
    manifest = store.state('publication:'+digest(raw),None)
    if not manifest:
        raise ValueError('No server-generated publication record matches this package. Rebuild a fresh proposal.')
    # The immutable ZIP digest binds every archived byte and test result. Never
    # trust a worker-authored manifest or an old approval as validation.
    current_base = git(Path(manifest['source']),'rev-parse','HEAD')
    base_changed = current_base!=manifest['base_commit']
    return {'package_sha256':digest(raw),'base_commit':manifest['base_commit'],
            'current_base':current_base,'base_changed':base_changed,
            'validated':manifest['validated'],
            'ready_for_approval':manifest['validated'] and not base_changed,
            'instruction':('The integration base changed. Create a fresh integration job/worktree on the current base, apply this saved patch, resolve conflicts, rerun tests, and package a new proposal. Do not overwrite other features or reuse earlier publication approvals.' if base_changed else
                           'Prepare exact repository-write approval for these packaged bytes; no publication permission is granted by this check.' if manifest['validated'] else
                           'No passing, unchanged source validation is recorded. Run the required tests against the isolated checkout and prepare a new package.')}


def for_handoff(store,job,file_ids):
    """Never let a completed implementation return only historical links."""
    from .tools import local_artifact
    from .artifact_inputs import verified_source
    saved=checkout(job)
    if not saved:
        return None
    root=Path(saved['path'])
    changed=git_bytes(root,'diff','--name-only','-z',saved['base_commit'],'--')
    untracked=git_bytes(root,'ls-files','--others','--exclude-standard','-z')
    if not changed and not untracked:
        return None
    current=snapshot(job)
    for fid in file_ids:
        file=local_artifact(store,job['id'],fid)
        if not file or Path(file['path']).suffix.lower()!='.zip':
            continue
        manifest=store.state('publication:'+digest(verified_source(store,file).read_bytes()),{})
        if manifest.get('job_id')==job['id'] and manifest.get('tree_sha256')==current['tree_sha256']:
            return {'file_id':fid,'url':'/api/files/'+fid,'validated':manifest['validated']}
    if len(file_ids)>=16:
        raise ValueError('Leave one deliverable slot for the required publication package.')
    return prepare(store,job)


def apply_package(store, job, file_id):
    """Reapply saved work only in a clean owned checkout, never the live tree."""
    from .tools import local_artifact
    from .artifact_inputs import verified_source
    from .local_checks import ActionNotExecuted
    try:
        file = local_artifact(store,job['id'],file_id)
        if not file:
            raise ValueError('Attach the complete publication package first.')
        path = verified_source(store,file)
        manifest = store.state('publication:'+digest(path.read_bytes()),None)
        saved = checkout(job)
        if not manifest or not saved:
            raise ValueError('A server-generated package and isolated feature checkout are required.')
        if Path(manifest['source']).resolve()!=Path(saved['source']).resolve():
            raise ValueError('Publication package belongs to a different repository.')
        root = Path(saved['path'])
        if git(root,'status','--porcelain') or git(root,'rev-parse','HEAD')!=saved['base_commit']:
            raise ValueError('Integrate into a fresh, clean job checkout; preserve existing work.')
        with zipfile.ZipFile(path) as archive:
            patch = archive.read('changes.patch')
        if digest(patch)!=manifest['patch_sha256']:
            raise ValueError('Publication patch checksum mismatch.')
    except (ValueError,KeyError,OSError) as error:
        raise ActionNotExecuted(str(error)) from error
    try:
        result = subprocess.run(['git','-C',str(root),'apply','--3way','--index','-'],input=patch,
                                capture_output=True,timeout=60,**background_options())
    except OSError as error:
        raise ActionNotExecuted(str(error)) from error
    conflicts = [name for name in git_bytes(root,'diff','--name-only','--diff-filter=U','-z').decode('utf-8').split('\0') if name]
    return {'applied':result.returncode==0,'conflicts':conflicts,'base_commit':saved['base_commit'],
            'output':result.stderr.decode('utf-8',errors='replace')[-4000:],
            'instruction':'Resolve conflicts in this isolated checkout with staged apply_changes; run tests and prepare a new publication package. Earlier validation and approval do not cover this integration.'}


def guard_command(store, job, args):
    """Bind native Git publication commands at review AND execution time.

    Opaque scripts retain independent effects review and exact-action approval;
    they are never auto-authorized by a publication package.
    """
    from .tools import resolve_path
    from .local_checks import ActionNotExecuted
    command = args.get('command',[])
    writes = {'add','commit','push','merge','rebase','cherry-pick','reset','checkout',
              'switch','apply','am','tag','update-ref','branch','restore','clean'}
    if (not command or PureWindowsPath(command[0]).name.lower() not in {'git','git.exe'}
            or not writes.intersection(command[1:])):
        return
    if len(command)>2 and command[1]=='branch' and all(x in {'--show-current','--list','-a','-r','-v','-vv'} for x in command[2:]):
        return
    try:
        fid = args.get('publication_file_id')
        if not fid:
            raise ValueError('Prepare and validate a publication package, then supply publication_file_id for repository-write approval.')
        result = validate(store,job,fid)
        if not result['ready_for_approval']:
            raise ValueError(result['instruction'])
        current = snapshot(job)
        manifest = store.state('publication:'+result['package_sha256'],{})
        if current['tree_sha256']!=manifest['tree_sha256'] or current['base_commit']!=manifest['base_commit']:
            raise ValueError('Checkout differs from the tested publication package. Reapply, test and prepare a fresh proposal.')
        if resolve_path(job['id'],args.get('cwd','.'))!=Path(current['path']):
            raise ValueError('Repository writes must run in this job isolated checkout.')
        if any(x.startswith(('-C','--git-dir','--work-tree','--namespace','-c','--config-env')) for x in command[1:]):
            raise ValueError('Repository-write commands cannot override the reviewed Git directory or configuration.')
    except (ValueError,KeyError,OSError) as error:
        raise ActionNotExecuted(str(error)) from error
