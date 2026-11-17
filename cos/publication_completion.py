"""Keep self-improvement delivery open until GitHub confirms the packaged change merged."""
import hashlib
import json
from pathlib import Path

from .db import now


def identity(manifest):
    # Reintegrating unchanged source on a newer base preserves the obligation.
    return hashlib.sha256(json.dumps([manifest['source'], sorted(
        (f['path'], f['after_sha256']) for f in manifest['files'])], sort_keys=True).encode()).hexdigest()


def validate_finish(connection, job):
    if job['org'] != 'Self-Improvement':
        return
    tree = connection.execute('''WITH RECURSIVE tree AS (
        SELECT * FROM jobs WHERE id=? UNION ALL
        SELECT j.* FROM jobs j JOIN tree t ON j.parent_id=t.id WHERE j.status<>'cancelled'
        ) SELECT * FROM tree''', (job['id'],)).fetchall()
    pending = []
    for row in tree:
        state = json.loads(row['messages'])
        source_only = isinstance(state,dict) and state.get('source_only')
        if row['id'] != job['id'] and row['status'] != 'completed' and not source_only:
            pending.append(row['id'])
    if pending:
        raise ValueError('Self-improvement is unfinished: delegated work is still open. Wait for its result; do not close the publication task. Jobs: '+', '.join(pending[:8]))
    ids = {r['id'] for r in tree}
    latest = {}
    for row in connection.execute("SELECT key,value FROM state WHERE key LIKE 'publication:%'"):
        manifest = json.loads(row['value'])
        worker = manifest.get('job_id')
        if worker in ids and manifest.get('created','') >= latest.get(worker,{}).get('created',''):
            latest[worker] = manifest
    # A delegated leaf can return preparation to an OPEN self-improvement parent.
    # Coordinators and owner tasks retain the obligation across that handoff.
    parent = connection.execute('SELECT org,status FROM jobs WHERE id=?', (job['parent_id'],)).fetchone() if job['parent_id'] else None
    if len(tree) == 1 and parent and parent['org'] == 'Self-Improvement' and parent['status'] not in {'completed','cancelled'}:
        return
    missing = [m for m in latest.values() if not connection.execute(
        'SELECT 1 FROM state WHERE key=?', ('publication_merged:'+identity(m),)).fetchone()]
    if missing:
        raise ValueError('Self-improvement publication is incomplete. Commit and push the tested package, create its PR, merge it into the default branch, then call verify_publication with file_id and pr_number. A report, package, approval, or completed child is not merge evidence. Preserve the work and request the next exact action approval instead of finishing.')


def verify(store, job, file_id, pr_number):
    """GET-only evidence bound to an attached server package and its actual commit."""
    from . import github
    from .artifact_inputs import verified_source
    from .engineering import checkout, git
    from .publication import digest, snapshot
    from .tools import local_artifact

    if type(pr_number) is not int or pr_number < 1:
        raise ValueError('Provide a positive PR number.')
    file = local_artifact(store, job['id'], file_id)
    if not file:
        raise ValueError('Attach the publication package to this job first.')
    manifest = store.state('publication:'+digest(verified_source(store,file).read_bytes()), None)
    if not manifest or not manifest.get('validated'):
        raise ValueError('A server-generated tested publication package is required.')
    worker = {'id':manifest['job_id']}
    saved = checkout(worker)
    if not saved or snapshot(worker)['tree_sha256'] != manifest['tree_sha256']:
        raise ValueError('Source differs from the tested package; prepare a fresh package.')
    root = Path(saved['path'])
    head = git(root,'rev-parse','HEAD')
    if head == manifest['base_commit'] or git(root,'status','--porcelain'):
        raise ValueError('Commit the exact tested package before verifying publication.')
    remote = git(root,'remote','get-url','origin')
    if remote.startswith('git@github.com:'):
        remote = 'https://github.com/'+remote.split(':',1)[1]
    repository,_ = github.repository_target(remote)
    login = github._account(repository.split('/')[0])
    token = github._token(login)
    base = 'repos/'+repository
    target = github._get(base,login,token)['default_branch']
    pr = github._get(base+'/pulls/'+str(pr_number),login,token)
    if (pr.get('merged') is not True or not pr.get('merged_at') or pr.get('state') != 'closed'
            or pr.get('base',{}).get('ref') != target
            or (pr.get('base',{}).get('repo') or {}).get('full_name') != repository
            or (pr.get('head',{}).get('repo') or {}).get('full_name') != repository
            or pr.get('head',{}).get('ref') != manifest['branch']
            or pr.get('head',{}).get('sha') != head):
        raise ValueError('GitHub has not verified this exact feature commit as pushed and merged by this PR into the default branch.')
    merged = pr.get('merge_commit_sha')
    if not isinstance(merged,str) or len(merged) != 40 or any(c not in '0123456789abcdef' for c in merged):
        raise ValueError('GitHub did not return a merge commit identity.')
    tree = github._get(base+'/git/trees/'+merged,login,token,{'recursive':'1'})
    if tree.get('truncated'):
        raise ValueError('GitHub returned incomplete merge evidence; do not mark publication completed.')
    blobs = {entry['path']:entry for entry in tree['tree']}
    for entry in manifest['files']:
        name = entry['path']
        local = git(root,'ls-tree','HEAD','--',name)
        expected = local.split()[2] if local else None
        actual = blobs.get(name)
        if ((entry['after_sha256'] is None and actual is not None)
                or (entry['after_sha256'] is not None and
                    (not expected or not actual or actual.get('type') != 'blob' or actual.get('sha') != expected))):
            raise ValueError('Merged source does not match the tested package: '+name)
    receipt = {'repository':repository,'pr_number':pr_number,'url':pr['html_url'],
               'head':head,'merge_commit':merged,'merged_at':pr['merged_at'],'verified_at':now()}
    for key in {identity(manifest), *manifest.get('supersedes',[])}:
        store.set_state('publication_merged:'+key,receipt)
    store.event(job['id'],'publication_verified',json.dumps(receipt))
    return receipt


def resurface(store, job_id, instruction):
    """Restore saved work to Attention without replaying any repository actions."""
    from .progress import reset
    with store.lock, store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        job = c.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        if not job or job['status'] != 'completed':
            raise ValueError('Only a completed job can be resurfaced.')
        if c.execute("SELECT 1 FROM approvals WHERE job_id=? AND status IN ('pending','executing','uncertain')",(job_id,)).fetchone():
            raise ValueError('Resolve the existing action outcome before resurfacing this job.')
        state = json.loads(job['messages'])
        if isinstance(state,list):
            state = {'messages':state}
        for key in ('final_output','final_review','waiting_call','waiting_action','delivered_versions'):
            state.pop(key,None)
        reset(state)
        state['delivery_pending'] = True
        state.setdefault('messages',[]).append({'role':'system','content':instruction})
        stamp = now()
        c.execute("UPDATE jobs SET status='waiting_input',question=?,messages=?,step=0,updated=? WHERE id=?",
                  (instruction,json.dumps(state),stamp,job_id))
        c.execute('UPDATE result_reviews SET eligible=0 WHERE job_id=?',(job_id,))
        row = c.execute('SELECT body FROM job_briefs WHERE job_id=?',(job_id,)).fetchone()
        brief = json.loads(row['body']) if row else {}
        brief.update(status_summary=instruction,next_step='Resume publication from the saved integration package; verify_publication must confirm the merged PR before finish.')
        c.execute('INSERT OR REPLACE INTO job_briefs VALUES(?,?,?)',(job_id,json.dumps(brief),stamp))
        c.execute('INSERT INTO events(job_id,kind,body,created) VALUES(?,?,?,?)',(job_id,'publication_reopened',instruction,stamp))
