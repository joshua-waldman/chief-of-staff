"""Lossless pruning of loose recovery snapshots into a verified ZIP bundle.

Original file IDs and logical paths remain valid. The bundle is published before
its index, and the index is committed before any loose file is removed.
"""
import hashlib
import json
import os
import re
import shutil
import zipfile
from pathlib import Path
from .db import uid,now
from . import tools

GENERATED=re.compile(r'(?:history|checkpoint|content)-[0-9a-f]{20}\.txt|image-[0-9a-f]{20}\.(?:png|jpg|webp)')
BUNDLE='recovery-archive.zip'
MARKER=b'Chief of Staff recovery archive v1'


def archive_key(job_id):
    return 'recovery_archive:'+job_id


def digest_stream(stream):
    digest=hashlib.sha256()
    size=0
    while chunk:=stream.read(1024*1024):
        digest.update(chunk)
        size+=len(chunk)
    return digest.hexdigest(),size


def verified_member(bundle,name,meta):
    info=bundle.getinfo(name)
    if info.file_size!=meta['size']:
        raise ValueError('Recovery archive size mismatch.')
    with bundle.open(name) as stream:
        if digest_stream(stream)!=(meta['sha256'],meta['size']):
            raise ValueError('Recovery archive checksum mismatch.')


def recovery_root(job_id):
    base=(tools.DATA/'jobs'/job_id/'workspace').resolve()
    root=base/'context'
    if root.is_symlink() or root.resolve().parent!=base:
        raise ValueError('Recovery directory must stay inside the job workspace.')
    return root


def archived_bytes(store,job_id,path,max_size=None):
    """Return exact archived bytes for a missing logical path, or None.

    The caller must authorize the logical path before calling this function.
    """
    with store.lock:
        root=recovery_root(job_id)
        path=Path(path).resolve()
        if path.parent!=root or not GENERATED.fullmatch(path.name) or path.exists():
            return None
        meta=store.state(archive_key(job_id),{}).get('members',{}).get(path.name)
        if not meta:
            return None
        if max_size is not None and meta['size']>max_size:
            raise ValueError('File exceeds 25 MB input limit.')
        with zipfile.ZipFile(root/BUNDLE) as bundle:
            verified_member(bundle,path.name,meta)
            return bundle.read(path.name)


def recovery_download(store,job_id,path):
    """Open a stable stream while holding the same lock as pruning.

    An open ZIP handle remains on the old inode after replacement on Unix.
    Windows defers replacement while a download holds the file open.
    """
    with store.lock:
        root=recovery_root(job_id)
        path=Path(path).resolve()
        if not path.is_relative_to(root):
            raise ValueError('Unexpected recovery download path.')
        bundle=None
        try:
            stream=path.open('rb')
        except FileNotFoundError:
            meta=store.state(archive_key(job_id),{}).get('members',{}).get(path.name) if path.parent==root else None
            if not meta:
                raise
            bundle=zipfile.ZipFile(root/BUNDLE)
            try:
                verified_member(bundle,path.name,meta)
                stream=bundle.open(path.name)
            except BaseException:
                bundle.close()
                raise
        def close():
            stream.close()
            if bundle:
                bundle.close()
        def chunks():
            try:
                while chunk:=stream.read(1024*1024):
                    yield chunk
            finally:
                close()
        return chunks(),close


def prune_recovery(store,job_id,*,apply=False,min_files=1):
    """Compress outside write locks, then publish and prune with status checks.

    The saved index acts as an optimistic version: concurrent cleanup or worker
    activity causes publication/deletion to defer, preserving loose originals.
    """
    with store.lock,store.connect() as connection:
        connection.execute('BEGIN')
        job=connection.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        if not job:
            raise ValueError('Job not found.')
        if job['status'] in {'running','queued'}:
            return {'job_id':job_id,'skipped':'Job is active or queued.','files':0}
        root=recovery_root(job_id)
        records=[dict(row) for row in connection.execute(
            'SELECT * FROM files WHERE job_id=? ORDER BY created DESC,id DESC',(job_id,))]
        saved=connection.execute('SELECT value FROM state WHERE key=?',(archive_key(job_id),)).fetchone()
        version=saved['value'] if saved else None
    generated=[row for row in records if GENERATED.fullmatch(Path(row['path']).name)
               and Path(row['path']).resolve().parent==root]
    keep=set()
    for label in ('checkpoint-','history-'):
        recent=[Path(row['path']).name for row in generated if Path(row['path']).name.startswith(label)]
        keep.update(recent[:2])
    session=json.loads(job['messages'])
    if isinstance(session,dict) and session.get('checkpoint_path'):
        keep.add(Path(session['checkpoint_path']).name)
    candidates=[row for row in generated if Path(row['path']).name not in keep
                and Path(row['path']).is_file() and not Path(row['path']).is_symlink()]
    result={'job_id':job_id,'files':len(candidates),
            'loose_bytes':sum(Path(row['path']).stat().st_size for row in candidates),'applied':False}
    if not apply or len(candidates)<min_files:
        return result
    members=json.loads(version).get('members',{}) if version else {}
    target=root/BUNDLE
    if target.is_symlink():
        raise ValueError('Recovery archive cannot be a symbolic link.')
    if target.exists():
        with zipfile.ZipFile(target) as previous:
            if previous.comment!=MARKER:
                raise ValueError('Refusing to replace an unrelated recovery bundle.')
    temp=root/('recovery-archive-'+uid()+'.tmp')
    try:
        with zipfile.ZipFile(temp,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as output:
            output.comment=MARKER
            if members:
                with zipfile.ZipFile(target) as previous:
                    for name,meta in members.items():
                        if not GENERATED.fullmatch(name):
                            raise ValueError('Unexpected recovery archive member.')
                        verified_member(previous,name,meta)
                        with previous.open(name) as source,output.open(name,'w',force_zip64=True) as dest:
                            shutil.copyfileobj(source,dest)
            for row in candidates:
                path=Path(row['path'])
                with path.open('rb') as source:
                    digest,size=digest_stream(source)
                meta={'sha256':digest,'size':size,'file_id':row['id']}
                if path.name in members:
                    if members[path.name]!=meta:
                        raise ValueError('An archived recovery file has changed.')
                else:
                    output.write(path,path.name)
                    members[path.name]=meta
        with zipfile.ZipFile(temp) as check:
            for name,meta in members.items():
                verified_member(check,name,meta)
        with temp.open('r+b') as source:
            os.fsync(source.fileno())
            source.seek(0)
            checksum,size=digest_stream(source)
        with store.lock,store.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            current=connection.execute('SELECT value FROM state WHERE key=?',(archive_key(job_id),)).fetchone()
            status=connection.execute('SELECT status FROM jobs WHERE id=?',(job_id,)).fetchone()['status']
            if (current['value'] if current else None)!=version or status in {'running','queued'}:
                return {**result,'skipped':'Job or recovery index changed during compression; retry later.'}
            if target.is_symlink() or root.is_symlink() or root.resolve().parent!=root.parent:
                raise ValueError('Recovery directory changed during compression.')
            temp.replace(target)
            if os.name!='nt':
                directory=os.open(root,os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            archive=connection.execute('SELECT id FROM files WHERE job_id=? AND path=?',(job_id,str(target))).fetchone()
            archive_id=archive['id'] if archive else uid()
            stamp=now()
            connection.execute('INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?)',
                (archive_id,job_id,BUNDLE,str(target),'application/zip',stamp))
            connection.execute('INSERT OR REPLACE INTO artifact_provenance VALUES(?,?,?,?)',
                (archive_id,checksum,size,stamp))
            connection.execute('INSERT OR REPLACE INTO state VALUES(?,?)',
                (archive_key(job_id),json.dumps({'members':members})))
            connection.execute('INSERT INTO events(job_id,kind,body,created) VALUES(?,?,?,?)',
                (job_id,'maintenance',f'Preserved {len(members)} recovery files in a verified compressed bundle.',stamp))
            connection.commit()
        # The index is durable before deletion. Each short transaction excludes
        # worker claims; if work resumes, remaining loose copies are left alone.
        removed=0
        for row in candidates:
            with store.lock,store.connect() as connection:
                connection.execute('BEGIN IMMEDIATE')
                status=connection.execute('SELECT status FROM jobs WHERE id=?',(job_id,)).fetchone()['status']
                if status in {'running','queued'}:
                    break
                path=Path(row['path'])
                if path.is_symlink() or path.resolve().parent!=root:
                    continue
                try:
                    with path.open('rb') as source:
                        unchanged=digest_stream(source)==(members[path.name]['sha256'],members[path.name]['size'])
                except FileNotFoundError:
                    continue
                if unchanged:
                    path.unlink(missing_ok=True)
                    removed+=1
        return {**result,'applied':True,'removed_files':removed,'archive_bytes':size,
                'archived_files':len(members)}
    finally:
        temp.unlink(missing_ok=True)
