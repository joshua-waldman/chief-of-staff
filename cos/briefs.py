"""Validated structured job summaries, separate from verbose agent logs."""
import json
import re
from .db import now
FIELDS={'action_needed','action_reason','requested_action','action_plan','context','status_summary','next_step','email_from','email_subject','email_received','email_excerpt','email_id','response_needed','response_reason','proposed_to','proposed_subject','proposed_body','uncertainties'}

def unresolved_action(store, job):
    row=store.one('SELECT body FROM job_briefs WHERE job_id=?',(job['id'],))
    brief=json.loads(row['body']) if row else {}
    if brief.get('action_needed') in {'yes','unknown'}:
        return brief.get('requested_action') or brief.get('action_reason') or 'Review the unresolved follow-up.'
    return ''
def update_brief(store,job,a):
    current=store.one('SELECT body FROM job_briefs WHERE job_id=?',(job['id'],))
    body=json.loads(current['body']) if current else {}
    if set(a)-FIELDS:
        raise ValueError('Unknown job summary field.')
    for key,value in a.items():
        if not isinstance(value,str) or len(value)>(12000 if key=='proposed_body' else 2000):
            raise ValueError('Invalid summary field length.')
        if key in {'response_needed','action_needed'} and value not in {'yes','no','unknown'}:
            raise ValueError(key+' must be yes, no, or unknown.')
        body[key]=value.strip()
    store.write('INSERT OR REPLACE INTO job_briefs VALUES(?,?,?)',(job['id'],json.dumps(body),now()))
    if body.get('email_subject') or body.get('context'):
        store.update(job['id'],title=(body.get('email_subject') or body['context'])[:100])
    return body

def detail_brief(store,job):
    row=store.one('SELECT body FROM job_briefs WHERE job_id=?',(job['id'],))
    b=json.loads(row['body']) if row else {}
    request=job['request']
    for key,pattern in [('email_from',r'(?m)^From: (.+)$'),('email_subject',r'(?m)^Subject: (.+)$'),('email_id',r'Email ID: (\S+)'),('email_excerpt',r'(?m)^Preview: (.+)$')]:
        match=re.search(pattern,request)
        if match and not b.get(key):
            b[key]=match.group(1)
    b.setdefault('context',b.get('email_subject') or request.split('\n')[0][:300])
    b.setdefault('status_summary',{'waiting_input':'Waiting for your answer','awaiting_approval':'Ready for your approval','running':'Agent is working','waiting_children':'Waiting for delegated work','queued':'Queued for an agent','completed':'Work completed','blocked':'Needs information or setup','failed':'Stopped after an error','cancelled':'Cancelled'}.get(job['status'],job['status']))
    default_next={'waiting_input':'Answer the question below to continue.','awaiting_approval':'Review the exact action below, then approve or reject.','running':'The agent will return a result or a decision for you.','waiting_children':'The director will resume when sub-agents return.','queued':'An agent will start when capacity is available.','completed':'Review the result and any deliverables.','blocked':'Provide the missing setup or information below.','failed':'Review the error and provide instructions to retry.','cancelled':'No further steps are scheduled.'}
    b['next_step']=default_next.get(job['status'],'Review the latest activity.') if job['status'] in {'awaiting_approval','waiting_input','blocked','failed','cancelled'} else b.get('next_step',default_next.get(job['status'],''))
    for a in reversed(job.get('approvals',[])):
        if a['tool'] in {'send_email','reply_email','send_teams'} and a['status']=='pending':
            args=json.loads(a['args'])
            b.update(proposed_to=args.get('to',args.get('chat_id','Reply to original thread')),proposed_subject=args.get('subject',b.get('email_subject','')),proposed_body=args.get('body',''),response_needed='yes')
            break
    return b
