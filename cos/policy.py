"""Policy is enforced by the server, never delegated to model-supplied risk labels."""
from dataclasses import dataclass
import re

@dataclass
class Decision:
    risk: str
    approval: bool
    reason: str

ALWAYS = {'send_email','reply_email','send_teams','confluence_write','merge_code','deploy_code','delete_file','publish','run_command','install_dependencies','browser_action','apply_changes'}
ALWAYS |= {'calendar_rsvp','delete_email','mark_email_read','mark_teams_read','create_feature_branch'}
LOW = {'search_knowledge','suggest_knowledge','update_brief','list_files','read_file','write_file','create_document','create_presentation','create_skill','create_organization','delegate','ask_user','finish','fetch_url','search_mail','read_mail','list_chats','read_teams','meeting_artifacts','meeting_transcripts','save_meeting_summary','jira_read','jira_write','list_skills','read_skill','mcp_list_tools','run_sandbox','run_python','mail_attachments','teams_attachments','browser_read'}
LOW |= {'calendar_events','calendar_event'}

TRUSTED_PACKAGES={'fastapi','uvicorn','httpx','pytest','python-docx','python-pptx','pypdf','mcp','requests','pandas','numpy','openpyxl','rich','pydantic','keyring','playwright'}

def trusted_packages(packages):
    return bool(packages) and all(isinstance(p,str) and re.fullmatch(r'[A-Za-z0-9_.-]+(?:[<>=!~]+[A-Za-z0-9.*+-]+(?:,[<>=!~]+[A-Za-z0-9.*+-]+)*)?',p) and re.split(r'[<>=!~]',p)[0].lower().replace('_','-') in TRUSTED_PACKAGES for p in packages)

def classify(tool,args,org=None):
    if tool=='delegate' and org=='Communications':
        return Decision('high',True,'Review the requested work, destination organizations and deliverables before Communications starts delegated action.')
    if tool=='install_dependencies' and trusted_packages(args.get('packages',[])):
        return Decision('low',False,'Known registry packages are installed as binary wheels into the job-local dependencies directory.')
    if tool in ALWAYS:
        return Decision('high',True,'Approval is required for sends, Confluence, publishing, merging, deployment, deletion, and unrestricted host commands.')
    if tool == 'mcp_call':
        return Decision('high',True,'Imported MCP operations require review until their behavior is verified.')
    if tool == 'jira_write':
        if args.get('operation') not in {'comment','edit','create'}:
            return Decision('high',True,'Non-routine Jira operation.')
        if args.get('operation') == 'edit' and set(args.get('fields',{}))-{'summary','description','labels'}:
            return Decision('high',True,'This Jira edit changes fields outside the low-risk set.')
    if tool in LOW:
        return Decision('low',False,'Routine work authorized by the user.')
    raise ValueError('Unknown tool: '+tool)
