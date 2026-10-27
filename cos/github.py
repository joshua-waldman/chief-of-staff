"""Read GitHub evidence with an explicitly routed account, never global auth switches."""
import base64
import hashlib
import os
import re
import shutil
import subprocess
from urllib.parse import quote, unquote, urlparse

import httpx

from .config import settings
from .processes import background_options


def validate_accounts(accounts):
    if not isinstance(accounts, dict) or any(
        not isinstance(owner, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,99}', owner)
        or not isinstance(login, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}', login)
        for owner, login in accounts.items()
    ):
        raise ValueError('GitHub accounts must map repository owners to GitHub login names.')
    if len({owner.casefold() for owner in accounts}) != len(accounts):
        raise ValueError('GitHub owner routes must be unique regardless of case.')


def routes():
    accounts = settings().get('github_accounts', {})
    validate_accounts(accounts)
    return accounts


def connection_status():
    return {'available': bool(shutil.which('gh')), 'accounts': routes(),
            'setup': 'Sign in each account with gh auth login. Map each repository owner to its login here. Reads never switch the active GitHub account.'}


def _token(login):
    executable = shutil.which('gh')
    if not executable:
        raise ValueError('Install GitHub CLI and sign in the repository account with gh auth login.')
    env = dict(os.environ)
    for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN', 'GH_HOST', 'GH_DEBUG'):
        env.pop(key, None)
    env['GH_PROMPT_DISABLED'] = '1'
    try:
        result = subprocess.run([executable, 'auth', 'token', '--hostname', 'github.com', '--user', login],
                                capture_output=True, text=True, timeout=15, env=env, **background_options())
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError('Could not read the selected GitHub account credential; check GitHub CLI sign-in.') from None
    if result.returncode or not result.stdout.strip():
        raise ValueError('Sign in GitHub account '+login+' with gh auth login; no other account was substituted.')
    return result.stdout.strip()


def _get(endpoint, login, token, params=None):
    # Fixed API host, GET only, no redirects or model-controlled headers.
    try:
        response = httpx.get('https://api.github.com/'+endpoint,
                            headers={'Authorization': 'Bearer '+token, 'Accept': 'application/vnd.github+json'},
                            params=params, timeout=30, follow_redirects=False)
    except httpx.HTTPError:
        raise ValueError('GitHub read failed for account '+login+'; check the connection.') from None
    if response.status_code != 200:
        raise ValueError('GitHub HTTP '+str(response.status_code)+' for account '+login+
                         '. Check repository access, organization SSO and the owner/account route in Connections. No fallback account was used.')
    return response.json()


def repository_target(value, ref=None):
    if not isinstance(value, str):
        raise ValueError('Provide owner/repository or an exact GitHub repository URL from source evidence.')
    if value.startswith('https://'):
        url = urlparse(value)
        if url.hostname != 'github.com' or url.username or url.password or url.port or url.query or url.fragment:
            raise ValueError('Use an exact github.com repository URL without credentials or query parameters.')
        parts = url.path.strip('/').split('/')
        if len(parts) > 2:
            if len(parts) < 4 or parts[2] not in {'tree', 'commits'}:
                raise ValueError('Use the repository root or a tree/commits branch URL; pass file path separately.')
            linked_ref = unquote('/'.join(parts[3:]))
            if ref and ref != linked_ref:
                raise ValueError('The explicit ref conflicts with the branch in the source URL.')
            ref = linked_ref
        value = '/'.join(parts[:2])
    value = value.removesuffix('.git')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,99}/[A-Za-z0-9_.-]{1,100}', value) or value.split('/')[1] in {'.', '..'}:
        raise ValueError('Provide an exact owner/repository from source evidence.')
    if ref is not None and (not isinstance(ref, str) or not ref or len(ref) > 250 or any(ord(c) < 32 for c in ref)):
        raise ValueError('Provide a valid repository ref.')
    return value, ref


def _account(owner):
    login = next((login for name, login in routes().items() if name.casefold() == owner.casefold()), None)
    if not login:
        raise ValueError('Configure a GitHub account for repository owner '+owner+' in Connections. The active COS repository account is not assumed.')
    return login


def search_repositories(name, owner=None):
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', name):
        raise ValueError('Search with a repository name from source evidence, not message text.')
    owners = [owner] if owner else list(routes())
    if not owners:
        raise ValueError('Configure repository owner/account routes in Connections before GitHub discovery.')
    results = []
    for organization in owners:
        login = _account(organization)
        token = _token(login)
        data = _get('search/repositories', login, token,
                    {'q': name+' in:name user:'+organization, 'per_page': 20})
        results.append({'owner': organization, 'account': login, 'total_count': data['total_count'],
                        'repositories': [{'repository': item['full_name'], 'url': item['html_url'],
                                          'default_branch': item['default_branch']} for item in data['items']]})
    return {'results': results}


def read_repository(repository, path=None, ref=None):
    repository, ref = repository_target(repository, ref)
    if path is not None and (not isinstance(path, str) or not path or len(path)>500 or '\\' in path
                             or any(p in {'', '.', '..'} for p in path.split('/')) or any(ord(c)<32 for c in path)):
        raise ValueError('Use a repository-relative file path without traversal.')
    login = _account(repository.split('/')[0])
    token = _token(login)
    base = 'repos/'+repository
    if not ref:
        ref = _get(base, login, token)['default_branch']
    commit = _get(base+'/commits/'+quote(ref, safe=''), login, token)['sha']
    if not isinstance(commit, str) or not re.fullmatch('[0-9a-fA-F]{40,64}', commit):
        raise ValueError('GitHub did not return a valid commit identity.')
    endpoint = base+'/contents/'+quote(path, safe='/') if path else base+'/readme'
    data = _get(endpoint, login, token, {'ref': commit})
    if not isinstance(data, dict) or data.get('type') != 'file' or data.get('encoding') != 'base64' or data.get('size', 0)>1024*1024:
        raise ValueError('Repository evidence must be one text file of at most 1 MB.')
    raw = base64.b64decode(data['content'])
    if len(raw)>1024*1024 or b'\x00' in raw:
        raise ValueError('Repository evidence must be one text file of at most 1 MB.')
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError('Repository evidence must be UTF-8 text.') from None
    source_path = data['path']
    return {'repository': repository, 'account': login, 'ref': ref, 'commit': commit,
            'path': source_path, 'source': 'https://github.com/'+repository+'/blob/'+commit+'/'+quote(source_path, safe='/'),
            'sha256': hashlib.sha256(raw).hexdigest(), 'text': text}
