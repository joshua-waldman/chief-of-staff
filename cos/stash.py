"""Bitbucket Server/Data Center (Stash) with explicit server/project routing.

Tokens are held in the OS credential store, never settings or action arguments.
Only the configured HTTPS server receives credentials; redirects are forbidden.
"""
import hashlib
import re
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx

from .config import settings

SERVICE = 'AMD-ChiefOfStaff-Stash'


def validate_servers(servers):
    if not isinstance(servers, dict):
        raise ValueError('Stash servers must be an object keyed by server name.')
    urls = set()
    for name, server in servers.items():
        if (not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', name)
                or name.lower()=='github'
                or not isinstance(server, dict) or set(server) != {'url', 'projects'}):
            raise ValueError('Each Stash server needs a name other than github, url and projects mapping.')
        url = server['url']
        if not isinstance(url, str):
            raise ValueError('Stash URL must be HTTPS.')
        parsed = urlparse(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or url != url.rstrip('/')
                or any(c.isspace() or ord(c)<32 for c in url)
                or not re.fullmatch(r'(?:/[A-Za-z0-9_-]+)*', parsed.path)):
            raise ValueError('Use an exact HTTPS Stash base URL, with optional context path and no trailing slash.')
        try:
            parsed.port
        except ValueError:
            raise ValueError('Invalid Stash server port.') from None
        if url.lower() in urls:
            raise ValueError('Stash server URLs must be unique.')
        urls.add(url.lower())
        projects = server['projects']
        if (not isinstance(projects, dict) or len({p.lower() for p in projects if isinstance(p,str)}) != len(projects)
                or any(not isinstance(p,str) or not re.fullmatch(r'~?[A-Za-z0-9][A-Za-z0-9_.-]{0,99}',p)
                       or not isinstance(login,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,159}',login)
                       for p,login in projects.items())):
            raise ValueError('Stash projects must map unique project keys to login names, never tokens.')


def servers():
    result = settings().get('stash_servers', {})
    validate_servers(result)
    return result


def route(name, project):
    if not isinstance(name,str) or not isinstance(project,str):
        raise ValueError('Choose a configured Stash server name and project key.')
    server = servers().get(name)
    if server:
        login = next((v for k,v in server['projects'].items() if k.lower()==project.lower()), None)
        if login:
            return server, login
    raise ValueError('Configure the Stash server/project account route in Connections.')


def credential_id(server, login):
    return server['url']+'|'+login


def store_token(name, project, token):
    import keyring
    server, login = route(name, project)
    if not isinstance(token,str) or not token.strip() or any(c.isspace() for c in token.strip()):
        raise ValueError('Provide a nonempty Stash personal access token.')
    if 'plaintext' in type(keyring.get_keyring()).__name__.lower():
        raise ValueError('A secure operating-system credential store is required.')
    try:
        keyring.set_password(SERVICE, credential_id(server, login), token.strip())
    except Exception:
        raise ValueError('Could not save the Stash token in the OS credential store.') from None


def token_for(server, login):
    import keyring
    try:
        token = keyring.get_password(SERVICE, credential_id(server, login))
    except Exception:
        token = None
    if not token:
        raise ValueError('Save the Stash personal access token for '+login+' in Connections. No fallback account was used.')
    return token


def connection_status():
    return {'servers': servers(), 'setup': 'Configure a server URL and project → login routes, then save a personal access token securely. Register a local clone under Project roots for engineering work.'}


def target(value, ref=None):
    """Return (server alias, project, slug, linked ref) from browser or Git URLs."""
    if not isinstance(value,str):
        raise ValueError('Use an exact configured Stash repository URL.')
    ssh = re.fullmatch(r'git@([^/:]+):(.+)', value)
    parsed = urlparse('ssh://git@'+ssh[1]+'/'+ssh[2] if ssh else value)
    is_ssh = parsed.scheme == 'ssh'
    if (parsed.scheme not in {'https','ssh'} or not parsed.hostname or parsed.password
            or (parsed.username is not None and (not is_ssh or parsed.username!='git'))
            or parsed.fragment or any(c.isspace() or ord(c)<32 for c in value)):
        raise ValueError('Use a configured HTTPS Stash URL or an SSH Git remote without credentials.')
    candidates = []
    for name, server in servers().items():
        base = urlparse(server['url'])
        if is_ssh:
            if parsed.hostname == base.hostname:
                candidates.append((name, parsed.path.strip('/')))
        elif parsed.netloc.lower()==base.netloc.lower() and parsed.path.startswith(base.path+'/'):
            candidates.append((name, parsed.path[len(base.path):].strip('/')))
    if len(candidates)!=1:
        raise ValueError('Stash repository must match exactly one configured server; check its URL in Connections.')
    name, path = candidates[0]
    parts = path.split('/')
    linked = None
    if len(parts)>=4 and parts[0]=='projects' and parts[2]=='repos':
        project, slug = parts[1], parts[3]
        if len(parts)>4:
            if parts[4]=='commits' and len(parts)>5:
                linked = unquote('/'.join(parts[5:]))
            elif parts[4]!='browse' or len(parts)>5:
                raise ValueError('Use the repository root or browse URL; pass file paths separately.')
    elif len(parts)==3 and parts[0]=='scm':
        project, slug = parts[1:]
    elif is_ssh and len(parts)==2:
        project, slug = parts
    else:
        raise ValueError('Use a Stash /projects/KEY/repos/slug or /scm/KEY/slug.git URL.')
    query = parse_qs(parsed.query, keep_blank_values=True)
    if query:
        if set(query)!={'at'} or len(query['at'])!=1 or not query['at'][0] or linked:
            raise ValueError('Only a single at branch/revision query parameter is supported.')
        linked = query['at'][0]
    slug = slug.removesuffix('.git')
    if (not re.fullmatch(r'~?[A-Za-z0-9][A-Za-z0-9_.-]{0,99}',project)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}',slug)):
        raise ValueError('Invalid Stash project or repository slug.')
    if linked and ref and linked!=ref:
        raise ValueError('The explicit ref conflicts with the repository URL.')
    ref = ref or linked
    if ref is not None and (not isinstance(ref,str) or not ref or len(ref)>250 or any(ord(c)<32 for c in ref)):
        raise ValueError('Provide a valid repository ref.')
    route(name, project)
    return name, project, slug, ref


class Repository:
    def __init__(self, value, ref=None):
        self.name, self.project, self.slug, self.ref = target(value, ref)
        self.server, self.login = route(self.name, self.project)
        self.base = 'projects/'+quote(self.project,safe='')+'/repos/'+quote(self.slug,safe='')
        self.url = self.server['url']+'/'+self.base
        self.clone_url = self.server['url']+'/scm/'+quote(self.project,safe='')+'/'+quote(self.slug,safe='')+'.git'

    def request(self, method, suffix='', params=None, body=None, raw=False, missing=False):
        return request(self.server,self.login,method,self.base+suffix,params,body,raw,missing)

    def default(self):
        data = self.request('GET','/branches/default')
        branch = data.get('id')
        if not isinstance(branch,str) or not branch.startswith('refs/heads/') or len(branch)<=11:
            raise ValueError('Stash did not return a valid default branch.')
        return branch, commit_id(data.get('latestCommit'))

    def commit(self):
        if not self.ref:
            return self.default()[1]
        return self.resolve(self.ref)

    def resolve(self, ref):
        # Refs in query parameters survive reverse proxies that reject encoded
        # slashes in URL paths (common for feature/PROJECT-123 branch names).
        page = self.request('GET','/commits',{'until':ref,'limit':1})
        values = page.get('values') or []
        if not values:
            raise ValueError('Stash did not resolve the requested branch or revision.')
        return commit_id(values[0].get('id'))

    def file(self, path, commit, missing=False):
        validate_path(path)
        return self.request('GET','/raw/'+quote(path,safe='/'),{'at':commit},raw=True,missing=missing)


def request(server, login, method, endpoint, params=None, body=None, raw=False, missing=False):
    token = token_for(server, login)
    try:
        response = httpx.request(method, server['url']+'/rest/api/1.0/'+endpoint,
            headers={'Authorization':'Bearer '+token,'Accept':'application/octet-stream' if raw else 'application/json'},
            params=params,json=body,timeout=30,follow_redirects=False)
    except httpx.HTTPError:
        raise ValueError('Stash request failed; check network/VPN and server connection.') from None
    if missing and response.status_code==404:
        return None
    if response.status_code not in {200,201}:
        # Never echo response bodies, URLs containing credentials, or HTTP exceptions.
        raise ValueError('Stash HTTP '+str(response.status_code)+' for account '+login+
                         '. Check token permissions, project route, VPN and server merge checks. No fallback account was used.')
    if raw:
        if len(response.content)>1024*1024:
            raise ValueError('Repository evidence must be at most 1 MB.')
        return response.content
    try:
        return response.json()
    except ValueError:
        raise ValueError('Stash returned an invalid API response; check the server URL and authentication.') from None


def commit_id(value):
    if not isinstance(value,str) or not re.fullmatch(r'[0-9a-f]{40}',value):
        raise ValueError('Stash did not return a valid commit identity.')
    return value


def validate_path(path, directory=False):
    if (not isinstance(path,str) or (not path and not directory) or len(path)>500 or '\\' in path
            or path.startswith('/') or any(p in {'.','..'} for p in path.split('/'))
            or (path and '' in path.split('/')) or any(ord(c)<32 for c in path)):
        raise ValueError('Use a repository-relative path without traversal.')


def next_offset(page, offset):
    if page.get('isLastPage') is True:
        return None
    next_page = page.get('nextPageStart')
    if type(next_page) is not int or next_page<=offset:
        raise ValueError('Stash returned incomplete pagination metadata.')
    return next_page


def read_repository(repository, path=None, ref=None):
    repo = Repository(repository, ref)
    commit = repo.commit()
    if path is None:
        # Select only a root README from a complete, bounded directory page.
        page = repo.request('GET','/browse',{'at':commit,'limit':500})
        children = page.get('children',{})
        if children.get('isLastPage') is not True:
            raise ValueError('Root directory listing is incomplete; supply an explicit README path.')
        names = [r['path']['toString'] for r in children.get('values',[]) if r.get('type')=='FILE']
        path = next((n for candidate in ('readme.md','readme.rst','readme.txt','readme')
                     for n in names if n.lower()==candidate),None)
        if not path:
            raise ValueError('No root README found; use repository_tree and supply a file path.')
    raw = repo.file(path, commit)
    try:
        if b'\x00' in raw:
            raise ValueError('Repository evidence must be UTF-8 text.')
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError('Repository evidence must be UTF-8 text.') from None
    return {'repository':repo.url,'provider':'stash','account':repo.login,'ref':repo.ref,
            'commit':commit,'path':path,'source':repo.url+'/browse/'+quote(path,safe='/')+'?at='+commit,
            'sha256':hashlib.sha256(raw).hexdigest(),'text':text}


def repository_tree(repository, ref=None, path='', offset=0, limit=200):
    validate_path(path, directory=True)
    if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=500:
        raise ValueError('Use a nonnegative offset and limit between 1 and 500.')
    repo = Repository(repository,ref)
    commit = repo.commit()
    # Root-relative paths avoid version-dependent directory-relative responses.
    page = repo.request('GET','/files',{'at':commit,'start':offset,'limit':limit})
    prefix = path+'/' if path else ''
    return {'repository':repo.url,'provider':'stash','account':repo.login,'commit':commit,'ref':repo.ref,
            'source':repo.url+'/browse?at='+commit,
            'entries':[{'path':p,'type':'blob'} for p in page['values'] if p.startswith(prefix)],
            'next_offset':next_offset(page,offset),
            'instruction':'Use commit as ref on subsequent pages and reads; follow next_offset even if this filtered page is empty.'}


def search_repositories(name, owner=None, server=None, offset=0):
    if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',name):
        raise ValueError('Search with a repository name from source evidence, not message text.')
    if type(offset) is not int or offset<0:
        raise ValueError('Use a nonnegative search offset.')
    configured = servers()
    if server is not None and server not in configured:
        raise ValueError('Choose a configured Stash server name.')
    results, errors = [], []
    for alias, config in configured.items():
        if server is not None and alias!=server:
            continue
        for project, login in config['projects'].items():
            if owner and project.lower()!=owner.lower():
                continue
            endpoint = 'projects/'+quote(project,safe='')+'/repos'
            # Older Stash project-list endpoints ignore a name query. Filter the
            # scoped page locally and retain its cursor even for empty matches.
            try:
                page = request(config,login,'GET',endpoint,{'start':offset,'limit':20})
            except ValueError as error:
                errors.append({'provider':'stash','server':alias,'owner':project,'error':str(error)})
                continue
            results.append({'provider':'stash','server':alias,'owner':project,'account':login,
                'next_offset':next_offset(page,offset),
                'repositories':[{'repository':config['url']+'/'+endpoint+'/'+quote(r['slug'],safe=''),
                                 'url':config['url']+'/'+endpoint+'/'+quote(r['slug'],safe='')}
                                for r in page['values'] if name.lower() in r['slug'].lower()
                                or name.lower() in r.get('name','').lower()]})
    if not results and not errors and server is not None:
        raise ValueError('No matching configured Stash project route.')
    return {'results':results,'errors':errors,
            'instruction':'Follow next_offset even for empty matches. Connection errors mean incomplete discovery coverage.'}


def check_pr(repo, pr, branch, head, target, state):
    def correct(ref, name, commit=None):
        source = ref.get('repository') or {}
        return (ref.get('id')==name and source.get('slug')==repo.slug
                and (source.get('project') or {}).get('key','').lower()==repo.project.lower()
                and (commit is None or ref.get('latestCommit')==commit))
    if (pr.get('state')!=state or not correct(pr.get('fromRef',{}),'refs/heads/'+branch,head)
            or not correct(pr.get('toRef',{}),target)):
        raise ValueError('Stash PR does not match the exact tested feature commit, repository and default branch.')


def verify_merge(repository, pr_number, branch, head, files):
    """Verify exact PR identity and packaged bytes at an immutable target commit.

    Some Stash versions omit properties.mergeCommit. In that case the receipt
    explicitly records the current default-branch commit as content evidence,
    rather than claiming that it is the historical merge commit.
    """
    repo = Repository(repository)
    target, target_commit = repo.default()
    pr = repo.request('GET','/pull-requests/'+str(pr_number))
    check_pr(repo,pr,branch,head,target,'MERGED')
    if not pr.get('closedDate'):
        raise ValueError('Stash did not confirm the merge timestamp.')
    merged = (pr.get('properties',{}).get('mergeCommit') or {}).get('id')
    verified = commit_id(merged) if merged else target_commit
    for entry in files:
        raw = repo.file(entry['path'],verified,missing=True)
        actual = hashlib.sha256(raw).hexdigest() if raw is not None else None
        if actual != entry['after_sha256']:
            raise ValueError('Merged source does not match the tested package: '+entry['path'])
    return {'provider':'stash','repository':repo.url,'pr_number':pr_number,
            'url':repo.url+'/pull-requests/'+str(pr_number),'head':head,
            'merge_commit':merged,'verified_target_commit':verified,'merged_at':pr['closedDate']}
