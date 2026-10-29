"""Validated structured job summaries, separate from verbose agent logs."""
import json
import re
from .db import now
FIELDS={'action_needed','action_reason','requested_action','action_plan','context','status_summary','next_step','email_from','email_subject','email_received','email_excerpt','email_id','response_needed','response_reason','proposed_to','proposed_subject','proposed_body','uncertainties'}
FIELDS |= {'triage_category','triage_reason','job_name'}
FIELDS |= {'time_sensitive','timing_reason','deadline_at'}
FIELDS |= {'owner_obligation_source_id','owner_obligation_evidence'}

OPTIONAL_REVIEW = {'review the result and any deliverables', 'review the result',
                   'review the unresolved follow-up',
                   'answer the question below to continue'}


def validate_question(question):
    if (not isinstance(question, str) or not question.strip() or
            question.strip().lower().rstrip('.!?') in OPTIONAL_REVIEW):
        raise ValueError('Identify the specific missing information or owner decision. '
                         'Optional result review is not a question; finish when no obligation remains.')


def validate_historical_attention(store, job, brief):
    state = json.loads(job['messages'])
    if not isinstance(state, dict) or not state.get('historical_import'):
        return
    if not any(brief.get(k) == 'yes' for k in ('action_needed', 'response_needed')):
        if any(brief.get(k) == 'unknown' for k in ('action_needed', 'response_needed')):
            validate_question(brief.get('uncertainties'))
        return
    source_id = brief.get('owner_obligation_source_id', '')
    excerpt = brief.get('owner_obligation_evidence', '').strip()
    source = store.one('''SELECT m.payload FROM teams_topic_messages m
        JOIN communication_threads t ON t.thread_id=m.topic_id AND t.source='teams'
        WHERE t.job_id=? AND m.message_id=?''', (job['id'], source_id))
    evidence = json.loads(source['payload']) if source else {}
    if (evidence.get('record_kind') not in {'message', 'owner_feedback'} or
            not excerpt or excerpt not in evidence.get('text', '')):
        raise ValueError('Historical owner attention requires owner_obligation_source_id '
                         'and owner_obligation_evidence quoting an original message or owner feedback '
                         'in this topic that establishes an outstanding obligation. A model brief, '
                         'generic result-review guidance or unverified tests is insufficient. '
                         'If only optional review remains, set action_needed and response_needed to no '
                         'and finish; use unknown with specific uncertainties for a real evidence gap.')

TIMING_POLICY = """Assess time sensitivity with LLM reasoning for every request requiring
owner attention, even when no explicit timeline is stated. Before asking for
input, requesting approval, or finishing triage, use update_brief to record
time_sensitive (yes/no/unknown), timing_reason, and deadline_at (empty unless an
actual deadline and timezone are established). Evaluate consequences of delay,
blocked people or work, dependencies, active incidents, expiring opportunities,
and the surrounding conversation. A credible need for prompt attention can be
inferred without any date or the word urgent. Explain the contextual evidence
and likely impact of delay in timing_reason; label inferred urgency as an
assessment rather than a stated deadline. Use no for routine work that can wait;
unknown when evidence is insufficient. Do not assume every approval, question,
senior sender or urgent subject is time critical. Assess the owner's outstanding
obligation, not work assigned to others or already resolved. Never invent a
deadline to express urgency. Use deadline_at only for a supported ISO timestamp
with timezone. Reassess timing as new evidence arrives and clear obsolete timing
fields when an obligation is resolved or superseded.
"""


def unresolved_action(store, job):
    row=store.one('SELECT body FROM job_briefs WHERE job_id=?',(job['id'],))
    brief=json.loads(row['body']) if row else {}
    validate_historical_attention(store, job, brief)
    if brief.get('action_needed') in {'yes','unknown'}:
        requested=brief.get('requested_action','').strip()
        if requested.lower().rstrip('.') in {'none','none outstanding','no follow-up','no further action','n/a'}:
            requested=''
        if brief['action_needed']=='unknown':
            followup = requested or brief.get('uncertainties') or brief.get('triage_reason') or brief.get('action_reason')
        else:
            followup = requested or brief.get('action_reason')
        validate_question(followup)
        return followup
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
        if key == 'time_sensitive' and value not in {'yes','no','unknown'}:
            raise ValueError('time_sensitive must be yes, no, or unknown.')
        if key == 'deadline_at' and value:
            from datetime import datetime
            try:
                parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                if parsed.tzinfo is None:
                    raise ValueError()
            except ValueError:
                raise ValueError('deadline_at must be an ISO timestamp with a timezone, or empty.')
        if key == 'triage_category':
            from .message_triage import LABELS
            if value not in LABELS:
                raise ValueError('Unknown triage category.')
        if key == 'job_name':
            from .job_names import compact_name
            value = compact_name(value)
        body[key]=value.strip()
    if body.get('time_sensitive') == 'yes' and not body.get('timing_reason'):
        raise ValueError('Explain the evidence for time-sensitive attention in timing_reason.')
    validate_historical_attention(store, job, body)
    if job['org'] == 'Communications':
        from .message_triage import category
        if body.get('triage_category') == 'review' and 'triage_category' not in a and {'response_needed','action_needed'}.intersection(a):
            body.pop('triage_category',None)
            body.pop('triage_reason',None)
        body['triage_category'] = category(body)
        incomplete = store.one('SELECT 1 FROM communication_sources WHERE job_id=? AND complete=0', (job['id'],))
        if incomplete and body['triage_category'] in {'reading','ignore'}:
            body.update(triage_category='review', response_needed='unknown', action_needed='unknown',
                        triage_reason='Full message text is needed before dismissing or summarizing this conversation.',
                        next_step='Read the remaining source bodies, then reassess reply and action needs.')
        if body['triage_category'] in {'reading','ignore'} and body.get('response_needed') == body.get('action_needed') == 'no':
            for field in ('requested_action','action_plan','proposed_to','proposed_subject','proposed_body'):
                body.pop(field,None)
            body['next_step'] = 'No further action required. You can optionally mark the conversation as read.'
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
    default_next={'waiting_input':'Answer the question below to continue.','awaiting_approval':'Review the exact action below, then approve or reject.','running':'The agent will return a result or a decision for you.','waiting_children':'The director will resume when sub-agents return.','queued':'An agent will start when capacity is available.','completed':'The result and any deliverables are available for optional review.','blocked':'Provide the missing setup or information below.','failed':'Review the error and provide instructions to retry.','cancelled':'No further steps are scheduled.'}
    b['next_step']=default_next.get(job['status'],'Review the latest activity.') if job['status'] in {'awaiting_approval','waiting_input','blocked','failed','cancelled'} else b.get('next_step',default_next.get(job['status'],''))
    if job['status'] in {'waiting_capacity','capacity_paused'}:
        b['status_summary']='Progress saved; waiting for model capacity.'
        b['next_step']='Review gateway capacity or retry when ready.' if job['status']=='capacity_paused' else 'The job will resume automatically.'
    for a in reversed(job.get('approvals',[])):
        if a['tool'] in {'send_email','reply_email','send_teams'} and a['status']=='pending':
            args=json.loads(a['args'])
            b.update(proposed_to=args.get('to',args.get('chat_id','Reply to original thread')),proposed_subject=args.get('subject',b.get('email_subject','')),proposed_body=args.get('body',''),response_needed='yes')
            break
    if job['org'] == 'Communications':
        from .message_triage import category
        b['triage_category'] = category(b)
    return b
