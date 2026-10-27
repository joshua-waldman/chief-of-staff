"""Owner-configured project workflow, independent of project names and vendors."""
from .processes import background_options
import re
import subprocess
from pathlib import Path
from .config import settings,ROOT


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


def project_root(job):
    if job.get('org')=='Self-Improvement':
        return ROOT.resolve()
    root=settings()['projects'].get(job.get('project'))
    if not root: raise ValueError('Choose a registered project before changing branches.')
    return Path(root).resolve()


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
    return rule.get('branch_prefix','codex/')+(ticket+'-' if ticket else '')+slug


def create_branch(job,a):
    name=branch_name(job,a); root=project_root(job)
    if a.get('branch')!=name: raise ValueError('The branch preview changed; request fresh approval.')
    git(root,'switch','-c',name)
    return {'created':True,'branch':git(root,'branch','--show-current'),'project':job['project']}


def verify_branch(job):
    rule=workflow(job)
    if job.get('org')=='Self-Improvement':
        rule={**rule,'require_feature_branch':True}
    if not rule.get('require_feature_branch') and not rule.get('require_ticket'): return
    name=git(project_root(job),'branch','--show-current')
    prefix=rule.get('branch_prefix','codex/')
    if not name.startswith(prefix) or (rule.get('require_ticket') and not re.match(r'[A-Za-z][A-Za-z0-9_]*-[0-9]+-',name[len(prefix):])):
        raise ValueError('Create or select a feature branch matching the configured workflow before applying changes.')
