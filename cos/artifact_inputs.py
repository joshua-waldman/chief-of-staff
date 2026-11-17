"""Verified retained artifact bytes and scoped recovery of historical handoffs."""
import hashlib
import shutil
from pathlib import Path

from .db import uid


def retain(job_id, path):
    from .tools import workspace
    directory = workspace(job_id).parent/'artifacts'
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory/(uid()+'.tmp')
    sha = hashlib.sha256()
    size = 0
    try:
        with path.open('rb') as source, temporary.open('wb') as retained:
            while chunk:=source.read(1024*1024):
                sha.update(chunk)
                size+=len(chunk)
                retained.write(chunk)
        digest = sha.hexdigest()
        target = directory/digest
        temporary.replace(target)
        return digest,size
    finally:
        temporary.unlink(missing_ok=True)


def verified_source(store, file, expected_sha256=None):
    from .tools import workspace, is_recovery_file
    if is_recovery_file(file['job_id'],file['path']):
        raise ValueError('Recovery archives cannot be handed to another agent.')
    meta = (store.one('SELECT sha256,size FROM artifact_versions WHERE file_id=? AND sha256=?',(file['id'],expected_sha256))
            if expected_sha256 else store.one('SELECT sha256,size FROM artifact_provenance WHERE file_id=?',(file['id'],)))
    if not meta:
        raise ValueError('Artifact has no recorded checksum; reconstruct and validate it before publication.')
    retained = workspace(file['job_id']).parent/'artifacts'/meta['sha256']
    for path in (retained, Path(file['path'])):
        if path.is_file():
            with path.open('rb') as stream:
                if path.stat().st_size==meta['size'] and hashlib.file_digest(stream,'sha256').hexdigest()==meta['sha256']:
                    return path
    raise ValueError('Original artifact bytes are unavailable or changed; rebuild and test a fresh proposal. Never substitute changed files under an old approval.')


def copy_verified(store,file,target,expected_sha256=None):
    source=verified_source(store,file,expected_sha256)
    expected=expected_sha256 or store.one('SELECT sha256 FROM artifact_provenance WHERE file_id=?',(file['id'],))['sha256']
    target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_name(uid()+'.tmp')
    try:
        shutil.copy2(source,temporary)
        with temporary.open('rb') as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest()!=expected:
                raise ValueError('Source artifact changed during transfer; no attachment was replaced.')
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def root_job(store, job):
    seen = set()
    while job.get('parent_id'):
        if job['id'] in seen or len(seen)>32:
            raise ValueError('Invalid job ancestry.')
        seen.add(job['id'])
        job = store.one('SELECT id,parent_id FROM jobs WHERE id=?',(job['parent_id'],))
        if not job:
            raise ValueError('Source job ancestry is unavailable.')
    return job['id']


def local_ready(store,job_id,fid):
    from .tools import local_artifact
    file=local_artifact(store,job_id,fid)
    if not file:
        return False
    path=Path(file['path'])
    meta=store.one('SELECT sha256,size FROM artifact_provenance WHERE file_id=?',(file['id'],))
    if not meta or not path.is_file() or path.stat().st_size!=meta['size']:
        return False
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()==meta['sha256']


def recover(store, job, file_ids, expected_sha256=None):
    """A cited link is recoverable only within the same owner task tree.

    Links in unrelated documents do not confer cross-job access. The controlled
    transfer verifies saved hashes and creates a normal local attachment.
    """
    from .tools import ARTIFACT_LINK, file_record, workspace, local_artifact
    if expected_sha256 is None:
        expected_sha256 = {}
    if not isinstance(file_ids,list) or len(file_ids)>16 or any(not isinstance(i,str) for i in file_ids):
        raise ValueError('Select at most 16 referenced input file IDs.')
    if not isinstance(expected_sha256,dict) or any(k not in file_ids or not isinstance(v,str) or len(v)!=64 or any(c not in '0123456789abcdef' for c in v) for k,v in expected_sha256.items()):
        raise ValueError('Expected checksums must be SHA-256 values for the selected input IDs.')
    references = set(ARTIFACT_LINK.findall(job.get('request','')))
    for local in store.rows('SELECT path FROM files WHERE job_id=?',(job['id'],)):
        path = Path(local['path'])
        if path.is_file() and path.stat().st_size<=2*1024*1024:
            try:
                references.update(ARTIFACT_LINK.findall(path.read_text(encoding='utf-8')))
            except (UnicodeError,OSError):
                pass
    candidates = []
    for fid in sorted(references):
        file = store.one('SELECT * FROM files WHERE id=?',(fid,))
        if not file:
            continue
        owner = store.one('SELECT id,parent_id FROM jobs WHERE id=?',(file['job_id'],))
        if owner and root_job(store,owner)==root_job(store,job):
            candidates.append(file)
    available = {f['id']:f for f in candidates}
    if any(fid not in available and not local_artifact(store,job['id'],fid) for fid in file_ids):
        raise ValueError('Input is not an attached or cited artifact from this owner task. Unrelated jobs require an explicit attachment.')
    sources=[]
    recovered={}
    for fid in file_ids:
        local=local_artifact(store,job['id'],fid)
        expected=expected_sha256.get(fid)
        meta=store.one('SELECT sha256 FROM artifact_provenance WHERE file_id=?',(local['id'],)) if local else None
        if local and local_ready(store,job['id'],fid) and (not expected or meta and meta['sha256']==expected):
            recovered[fid]=local['id']
            continue
        file=available.get(fid) or local
        sources.append((fid,file,verified_source(store,file,expected)))
    for fid,file,path in sources:
        target = workspace(job['id'])/'inputs'/(fid+'-'+path.name[:12]+Path(file['name']).suffix)
        target.parent.mkdir(parents=True,exist_ok=True)
        copy_verified(store,file,target,expected_sha256.get(fid))
        recovered[fid]=file_record(store,job['id'],target,file['mime'],name=file['name'])['file_id']
        store.event(job['id'],'input_recovered','Recovered verified input '+fid+' from the same owner task.')
    return {'available':[{'file_id':f['id'],'name':f['name']} for f in candidates],
            'recovered':[{'original_file_id':fid,'file_id':recovered[fid]} for fid in file_ids],
            'instruction':'If exact bytes cannot be recovered, rebuild from the current base, test, and prepare fresh publication approval. Never ask the owner to recreate COS intermediates.'}
