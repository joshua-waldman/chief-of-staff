"""Owner-configured project workflow, independent of project names and vendors."""
from .processes import background_options
import re
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


def create_branch(job,a):
    from .tools import workspace
    name=branch_name(job,a); root=source_root(job)
    if a.get('branch')!=name: raise ValueError('The branch preview changed; request fresh approval.')
    with _worktree_lock:
        saved = checkout(job)
        if saved:
            if saved['branch'] != name:
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
            if saved['branch']!=name or saved['source']!=str(root) or saved['path']!=str(target) or saved['job_id']!=job['id']:
                raise ValueError('A different feature checkout is reserved for this job.')
        else:
            saved = {'job_id':job['id'], 'project':job.get('project',''), 'source':str(root),
                     'path':str(target), 'branch':name, 'base_commit':git(root,'rev-parse','--verify','HEAD'),
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
