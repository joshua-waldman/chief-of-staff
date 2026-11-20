"""Owner-configured project workflow, independent of project names and vendors."""
from .processes import background_options
import re
import os
import subprocess
import json
import threading
from pathlib import Path
from .config import settings,ROOT

_worktree_lock = threading.RLock()


def workspace_metadata(job):
    from .tools import workspace
    return workspace(job['id']).parent / 'engineering.json'


def checkout(job):
    """Server-owned identity; never infer ownership from a branch prefix."""
    from .tools import workspace
    if not job.get('id'):
        return None
    path = workspace_metadata(job)
    pending = path.with_suffix('.pending.json')
    # Recover a process stop after Git finished but before registration. The
    # reservation was written by the server before any repository mutation.
    if not path.exists() and pending.exists() and (workspace(job['id'])/'checkout'/'.git').is_file():
        with _worktree_lock:
            if not path.exists() and pending.exists():
                _verify_checkout(job,json.loads(pending.read_text(encoding='utf-8')))
                pending.replace(path)
    if not path.exists():
        return None
    saved = json.loads(path.read_text(encoding='utf-8'))
    _verify_checkout(job,saved)
    return saved


def _verify_checkout(job,saved):
    from .tools import workspace
    root = (workspace(job['id']) / 'checkout').resolve()
    if saved['job_id'] != job['id'] or Path(saved['path']).resolve() != root:
        raise ValueError('Job checkout identity is invalid.')
    pointer=(root/'.git').read_text(encoding='utf-8').strip()
    if not pointer.startswith('gitdir: '):
        raise ValueError('Job checkout Git identity is invalid.')
    git_dir=(root/pointer[8:]).resolve()
    common=(git_dir/(git_dir/'commondir').read_text(encoding='utf-8').strip()).resolve()
    if common!=Path(saved['common_dir']).resolve():
        raise ValueError('Job checkout repository changed.')
    if (git_dir/'HEAD').read_text(encoding='utf-8').strip() != 'ref: refs/heads/'+saved['branch']:
        raise ValueError('Job checkout branch changed; recover its owned feature branch before continuing.')


def validate_workflows(value, projects):
    if not isinstance(value,dict): raise ValueError('Engineering workflows must be an object.')
    for project,rule in value.items():
        if project not in {'*',*projects} or not isinstance(rule,dict) or set(rule)-{'branch_prefix','require_ticket','require_feature_branch','jira_parent','jira_issue_type'}:
            raise ValueError('Choose registered projects (or *) and supported workflow fields.')
        for key in ('require_ticket','require_feature_branch'):
            if key in rule and type(rule[key]) is not bool: raise ValueError(key+' must be boolean.')
        for key in ('branch_prefix','jira_parent','jira_issue_type'):
            if key in rule and (not isinstance(rule[key],str) or len(rule[key])>160): raise ValueError('Workflow text exceeds the allowed length.')
        prefix=rule.get('branch_prefix','codex/')
        if not re.fullmatch(r'[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*/',prefix): raise ValueError('Use a branch prefix such as codex/ or feature/.')
        if rule.get('jira_parent') and not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*-[0-9]+',rule['jira_parent']): raise ValueError('Jira parent must be an issue key.')
    return value


def workflow(job):
    config=settings(); rules=config.get('engineering_workflows',{})
    return {**rules.get('*',{}),**rules.get(job.get('project'),{})}


def source_root(job):
    if job.get('org')=='Self-Improvement':
        return ROOT.resolve()
    root=settings()['projects'].get(job.get('project'))
    if not root: raise ValueError('Choose a registered project before changing branches.')
    return Path(root).resolve()


def project_root(job):
    saved = checkout(job)
    return Path(saved['path']) if saved else source_root(job)


def git(root,*args):
    result=subprocess.run(['git','-C',str(root),*args],capture_output=True,text=True,timeout=30,**background_options())
    if result.returncode: raise ValueError(result.stderr.strip() or 'Git operation failed.')
    return result.stdout.strip()


def branch_name(job,a):
    rule=workflow(job); ticket=a.get('ticket','')
    if ticket and (not isinstance(ticket,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*-[0-9]+',ticket)): raise ValueError('Provide a valid ticket key.')
    if rule.get('require_ticket') and not ticket: raise ValueError('This workflow requires a ticket key.')
    slug=re.sub(r'[^a-z0-9]+','-',str(a.get('name','')).lower()).strip('-')[:80]
    if not slug: raise ValueError('Provide a descriptive feature name.')
    suffix = '-'+job['id'][:8] if job.get('id') else ''
    return rule.get('branch_prefix','codex/')+(ticket+'-' if ticket else '')+slug+suffix


def remote_default_commit(root, repository=None):
    """Resolve the configured origin's default branch via its routed account.

    The API pins the commit independently of local tracking refs.
    """
    from . import github
    from .repositories import is_github
    remote = git(root, 'remote', 'get-url', 'origin')
    if not is_github(remote):
        from .stash import Repository
        repo = Repository(remote)
        if repository is not None and repo.url != repository:
            raise ValueError('The job origin no longer matches its reserved Stash repository.')
        return repo.url, repo.default()[1]
    if remote.startswith('git@github.com:'):
        remote = 'https://github.com/'+remote.split(':', 1)[1]
    found, _ = github.repository_target(remote)
    if repository is not None and repository != found:
        raise ValueError('The job origin no longer matches its reserved GitHub repository.')
    login = github._account(found.split('/')[0])
    token = github._token(login)
    base = 'repos/'+found
    default = github._get(base, login, token)['default_branch']
    if not isinstance(default, str) or not default or any(ord(c)<32 for c in default):
        raise ValueError('GitHub did not return a valid default branch.')
    sha = github._get(base+'/commits/'+github.quote(default, safe=''), login, token)['sha']
    if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise ValueError('GitHub did not return a valid default commit.')
    return found, sha


def acquire_remote_commit(root, repository, sha):
    """Acquire a pinned commit with the routed account, without updating refs."""
    try:
        if git(root, 'rev-parse', '--verify', sha+'^{commit}') == sha:
            return
    except ValueError:
        pass
    from .repositories import git_credentials
    url, authorization = git_credentials(repository)
    authenticated_git(root, url, authorization, ['fetch', '--no-tags',
        '--no-recurse-submodules', '--no-write-fetch-head', url, sha])
    if git(root, 'rev-parse', '--verify', sha+'^{commit}') != sha:
        raise ValueError('Acquired remote commit did not match the pinned identity.')


def authenticated_git(root, url, authorization, command):
    """Use a routed HTTPS credential only in the child environment."""
    # Credentials live only in the child environment, never argv, Git config,
    # URLs or error output. Disable helpers, redirects and recursive fetches.
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(('GIT_CONFIG_', 'GIT_TRACE')) or key in {
                'GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_OBJECT_DIRECTORY',
                'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_ASKPASS'}:
            env.pop(key, None)
    options = [('credential.helper', ''), ('http.extraHeader', ''),
               ('gc.auto', '0'), ('maintenance.auto', 'false'),
               ('http.'+url+'.extraHeader', 'Authorization: '+authorization),
               ('http.followRedirects', 'false')]
    env.update(GIT_CONFIG_COUNT=str(len(options)), GIT_TERMINAL_PROMPT='0')
    for i, (key, value) in enumerate(options):
        env['GIT_CONFIG_KEY_'+str(i)] = key
        env['GIT_CONFIG_VALUE_'+str(i)] = value
    try:
        result = subprocess.run(['git', '-C', str(root), *command],
            capture_output=True, timeout=120, env=env, **background_options())
        if result.returncode:
            if command[0]=='fetch':
                raise ValueError('Authenticated remote commit acquisition failed; check repository access and retry.')
            raise ValueError('Authenticated Git operation failed; check repository permissions and connection.')
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError('Authenticated Git operation did not complete; verify remote state before retrying.') from None


def current_base(saved):
    if saved.get('base_kind') != 'remote_default':
        return git(Path(saved['source']), 'rev-parse', 'HEAD')
    _, sha = remote_default_commit(Path(saved['source']), saved['remote_repository'])
    return sha


def create_branch(job,a):
    from .tools import workspace
    name=branch_name(job,a); root=source_root(job)
    base_kind=a.get('base','local')
    if base_kind not in {'local','remote_default'}:
        raise ValueError('Choose local or remote_default as the branch base.')
    if a.get('branch')!=name: raise ValueError('The branch preview changed; request fresh approval.')
    with _worktree_lock:
        saved = checkout(job)
        if saved:
            if saved['branch'] != name or saved.get('base_kind','local') != base_kind:
                raise ValueError('This job already owns another feature; delegate a new job for a new feature.')
            return {**saved, 'created':False, 'resumed':True}
        target = (workspace(job['id'])/'checkout').resolve()
        if target.exists():
            raise ValueError('Unregistered checkout exists; retain it for recovery instead of overwriting it.')
        path = workspace_metadata(job)
        pending = path.with_suffix('.pending.json')
        resuming=pending.exists()
        if pending.exists():
            saved=json.loads(pending.read_text(encoding='utf-8'))
            if (saved['branch']!=name or saved['source']!=str(root) or saved['path']!=str(target)
                    or saved['job_id']!=job['id'] or saved.get('base_kind','local')!=base_kind):
                raise ValueError('A different feature checkout is reserved for this job.')
        else:
            repository, base = remote_default_commit(root) if base_kind == 'remote_default' else (None, git(root,'rev-parse','--verify','HEAD'))
            if base_kind == 'remote_default':
                acquire_remote_commit(root, repository, base)
            saved = {'job_id':job['id'], 'project':job.get('project',''), 'source':str(root),
                     'path':str(target), 'branch':name, 'base_commit':base,
                     'base_kind':base_kind, 'remote_repository':repository,
                     'common_dir':git(root,'rev-parse','--path-format=absolute','--git-common-dir')}
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(saved),encoding='utf-8')
            temporary.replace(pending)
        # A failed add may have created the reserved branch before stopping.
        # Resume it only at its exact reserved base; never reset another ref.
        exists=subprocess.run(['git','-C',str(root),'show-ref','--verify','--quiet','refs/heads/'+name],
                              capture_output=True,timeout=30,**background_options()).returncode
        if exists==0:
            if not resuming:
                pending.unlink()
                raise ValueError('Feature branch already exists; never reuse an unowned branch.')
            if git(root,'rev-parse','refs/heads/'+name)!=saved['base_commit']:
                raise ValueError('Reserved feature branch moved; retain it for recovery.')
            git(root,'worktree','add',str(target),name)
        elif exists==1:
            git(root,'worktree','add','-b',name,str(target),saved['base_commit'])
        else:
            raise ValueError('Cannot verify the reserved feature branch.')
        _verify_checkout(job,saved)
        pending.replace(path)
        return {**saved, 'created':True}


def verify_branch(job):
    saved = checkout(job)
    if job.get('org')=='Self-Improvement' and not saved:
        raise ValueError('Create an isolated feature branch with create_feature_branch before applying or testing changes.')
    rule=workflow(job)
    if job.get('org')=='Self-Improvement':
        rule={**rule,'require_feature_branch':True}
    if not rule.get('require_feature_branch') and not rule.get('require_ticket'): return
    name=saved['branch'] if saved else git(project_root(job),'branch','--show-current')
    prefix=rule.get('branch_prefix','codex/')
    if not name.startswith(prefix) or (rule.get('require_ticket') and not re.match(r'[A-Za-z][A-Za-z0-9_]*-[0-9]+-',name[len(prefix):])):
        raise ValueError('Create or select a feature branch matching the configured workflow before applying changes.')
