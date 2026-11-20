"""Provider routing shared by source readers and engineering workflows."""
from urllib.parse import urlparse

from . import github, stash


def is_github(value):
    return (isinstance(value,str) and
            (value.startswith('git@github.com:') or
             (not '://' in value and not value.startswith('git@')) or
             urlparse(value).hostname=='github.com'))


def provider(value):
    return github if is_github(value) else stash


def connection_status():
    return {'github':github.connection_status(),'stash':stash.connection_status()}


def read_repository(repository, path=None, ref=None):
    return provider(repository).read_repository(repository,path,ref)


def repository_tree(repository, ref=None, path='', offset=0, limit=200):
    return provider(repository).repository_tree(repository,ref,path,offset,limit)


def search_repositories(name, owner=None, server=None, offset=0):
    if server == 'github' or (server is None and not stash.servers()):
        if offset:
            raise ValueError('GitHub search returns its first 20 candidates; narrow the repository name.')
        return github.search_repositories(name,owner)
    if server is not None:
        return stash.search_repositories(name,owner,server,offset)
    results, errors = [], []
    if offset==0 and github.routes() and (owner is None or any(o.lower()==owner.lower() for o in github.routes())):
        try:
            results.extend(github.search_repositories(name,owner)['results'])
        except ValueError as error:
            errors.append({'provider':'github','error':str(error)})
    found = stash.search_repositories(name,owner,offset=offset)
    results.extend(found['results'])
    errors.extend(found.get('errors',[]))
    return {'results':results,'errors':errors,
            'instruction':'Connection errors mean discovery coverage is incomplete, not that repositories are absent.'}


def git_credentials(repository):
    """Return a canonical HTTPS transport and its ephemeral auth header."""
    if not is_github(repository):
        repo = stash.Repository(repository)
        return repo.clone_url, 'Bearer '+stash.token_for(repo.server,repo.login)
    import base64
    if repository.startswith('git@github.com:'):
        repository = 'https://github.com/'+repository.split(':',1)[1]
    repository, _ = github.repository_target(repository)
    login = github._account(repository.split('/')[0])
    token = github._token(login)
    return 'https://github.com/'+repository+'.git', 'Basic '+base64.b64encode(('x-access-token:'+token).encode()).decode()
