"""Test-package-bound Stash writes through the existing exact approval flow."""
from pathlib import Path

from . import engineering, publication, stash
from .local_checks import ActionNotExecuted


def prepare(store, job, args):
    operation = args.get('operation')
    if operation not in {'push','create_pr','merge'}:
        raise ValueError('Choose push, create_pr or merge.')
    file_id = args.get('file_id')
    if not file_id:
        raise ValueError('Supply the tested publication package file_id.')
    validated = publication.validate(store,job,file_id)
    if not validated['ready_for_approval']:
        raise ValueError(validated['instruction'])
    manifest = store.state('publication:'+validated['package_sha256'])
    saved = engineering.checkout(job)
    if (not saved or manifest['job_id']!=job['id']
            or manifest.get('base_kind')!='remote_default'):
        raise ValueError('Publish from this job own tested worktree created with base:remote_default.')
    current = publication.snapshot(job)
    if current['tree_sha256']!=manifest['tree_sha256'] or current['base_commit']!=manifest['base_commit']:
        raise ValueError('Source changed since testing; prepare a new tested package.')
    root = Path(saved['path'])
    head = engineering.git(root,'rev-parse','HEAD')
    if head==manifest['base_commit'] or engineering.git(root,'status','--porcelain'):
        raise ValueError('Commit the exact tested package before publishing.')
    repo = stash.Repository(engineering.git(root,'remote','get-url','origin'))
    if repo.url != saved['remote_repository']:
        raise ValueError('The origin no longer matches this worktree reserved repository.')
    target, base = repo.default()
    if base!=manifest['base_commit']:
        raise ValueError('The remote default branch changed; integrate and retest on a fresh base.')
    preview = {'operation':operation,'repository':repo.url,'account':repo.login,
               'branch':saved['branch'],'head':head,'target':target,'base_commit':base,
               'package_sha256':validated['package_sha256']}
    if operation in {'create_pr','merge'}:
        remote_head = repo.resolve('refs/heads/'+saved['branch'])
        if remote_head!=head:
            raise ValueError('Push the exact tested feature commit before creating or merging its PR.')
    if operation=='create_pr':
        title, description = args.get('title'), args.get('description','')
        if (not isinstance(title,str) or not title.strip() or len(title)>255
                or not isinstance(description,str) or len(description)>32000):
            raise ValueError('Supply a PR title of 1–255 characters and a description of at most 32000 characters.')
        preview.update(title=title,description=description)
    if operation=='merge':
        number = args.get('pr_number')
        if type(number) is not int or number<1:
            raise ValueError('Supply a positive PR number.')
        pr = repo.request('GET','/pull-requests/'+str(number))
        stash.check_pr(repo,pr,saved['branch'],head,target,'OPEN')
        version = pr.get('version')
        if type(version) is not int or version<0:
            raise ValueError('Stash did not return a valid PR version.')
        preview.update(pr_number=number,version=version)
    # Replace every model-authored preview with server-derived identity.
    return {k:v for k,v in {**args,'_repository_preview':preview}.items()
            if k in {'operation','file_id','title','description','pr_number','_repository_preview'}}


def execute(store, job, args):
    try:
        current = prepare(store,job,args)
        if current['_repository_preview']!=args.get('_repository_preview'):
            raise ValueError('Repository, source or PR version changed since approval; request a fresh action review.')
        saved = engineering.checkout(job)
        preview = current['_repository_preview']
        repo = stash.Repository(preview['repository'])
        # Resolve credentials before entering the possible-external-write boundary.
        token = stash.token_for(repo.server,repo.login)
    except (ValueError,KeyError,OSError) as error:
        raise ActionNotExecuted(str(error)) from error
    operation = preview['operation']
    if operation=='push':
        engineering.authenticated_git(Path(saved['path']),repo.clone_url,'Bearer '+token,
            ['push','--porcelain','--',repo.clone_url,preview['head']+':refs/heads/'+preview['branch']])
        remote = repo.resolve('refs/heads/'+preview['branch'])
        if remote!=preview['head']:
            raise ValueError('Stash has not confirmed the exact pushed commit; checking the outcome is required.')
        return {'operation':'push','repository':repo.url,'branch':preview['branch'],'commit':preview['head']}
    if operation=='create_pr':
        ref = {'slug':repo.slug,'project':{'key':repo.project}}
        result = repo.request('POST','/pull-requests',body={
            'title':preview['title'],'description':preview['description'],
            'fromRef':{'id':'refs/heads/'+preview['branch'],'repository':ref},
            'toRef':{'id':preview['target'],'repository':ref}})
    else:
        # Server version check preserves Stash's concurrency and merge controls.
        result = repo.request('POST','/pull-requests/'+str(preview['pr_number'])+'/merge',
                              params={'version':preview['version']})
    number = result.get('id')
    if type(number) is not int or number<1:
        raise ValueError('Stash write response lacked a PR identity; reconcile before retrying.')
    return {'operation':operation,'repository':repo.url,'pr_number':number,
            'url':repo.url+'/pull-requests/'+str(number),'state':result.get('state'),
            'version':result.get('version')}


def verify_action(args):
    """Read-only recovery after a timeout or interruption, never replay writes."""
    preview = args.get('_repository_preview',{})
    repo = stash.Repository(preview['repository'])
    operation = preview['operation']
    if operation=='push':
        commit = repo.resolve('refs/heads/'+preview['branch'])
        if commit==preview['head']:
            return {'outcome':'executed','evidence':{'repository':repo.url,'commit':commit}}
    elif operation=='merge':
        pr = repo.request('GET','/pull-requests/'+str(preview['pr_number']))
        stash.check_pr(repo,pr,preview['branch'],preview['head'],preview['target'],'MERGED')
        return {'outcome':'executed','evidence':{'pr_number':pr['id'],'state':'MERGED'}}
    else:
        page = repo.request('GET','/pull-requests',{'state':'ALL','direction':'OUTGOING',
                    'at':'refs/heads/'+preview['branch'],'limit':100})
        matches = []
        for pr in page.get('values',[]):
            try:
                stash.check_pr(repo,pr,preview['branch'],preview['head'],preview['target'],pr.get('state'))
            except ValueError:
                continue
            if pr.get('title')==preview['title'] and (pr.get('description') or '')==preview['description']:
                matches.append(pr)
        if len(matches)==1 and page.get('isLastPage') is True:
            return {'outcome':'executed','evidence':{'pr_number':matches[0]['id'],'state':matches[0]['state']}}
    return {'outcome':'unknown','evidence':'Stash has not confirmed the exact operation. No write was repeated.'}
