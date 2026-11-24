"""Read-only top-level task discovery and progress for the owner Teams inbox."""
import json
import re
from collections import Counter
from datetime import datetime,timezone

from .config import settings
from .job_names import add_display_names,ellipsize
from .teams_formatting import status_table,render,MAX_HTML_BYTES

PAGE_SIZE = 6
TERMINAL = {'completed', 'cancelled'}
ATTENTION = {'waiting_input', 'awaiting_approval', 'blocked', 'failed', 'capacity_paused'}
LABELS = {'waiting_children': 'Waiting for delegated work', 'waiting_input': 'Your input needed',
          'awaiting_approval': 'Approval needed', 'waiting_capacity': 'Waiting for model capacity',
          'capacity_paused': 'Model capacity needs attention'}


def label(status):
    return LABELS.get(status, status.replace('_', ' ').capitalize())


def timestamp(value):
    try:
        stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
        if stamp.tzinfo is None:
            return stamp.strftime('%b %d, %Y · %H:%M')+' (timezone unknown)'
        return stamp.astimezone(timezone.utc).strftime('%b %d, %Y · %H:%M UTC')
    except (ValueError,TypeError,AttributeError):
        return 'Not recorded'


def short_reference(identity, identities):
    for size in range(8,len(identity)+1):
        prefix=identity[:size]
        if sum(other.startswith(prefix) for other in identities)==1:
            return prefix
    return identity


def title(job):
    return ellipsize(job.get('task_name') or job.get('user_name') or job['display_name'],120)


def table_row(store,job,identities,reviews):
    record=store.one('SELECT question,substr(request,1,1000) AS request FROM jobs WHERE id=?',(job['id'],))
    if not record:
        raise ValueError('This task changed. Request a fresh status list.')
    saved=store.one('SELECT body FROM job_briefs WHERE job_id=?',(job['id'],))
    brief=json.loads(saved['body']) if saved else {}
    name=title(job)
    original=record['request'] or ''
    if original.startswith(('Triage this incoming','Execute confirmed commitment:')):
        original=''
    context=''
    for candidate in (brief.get('context'),brief.get('requested_action'),brief.get('email_excerpt'),original):
        candidate=' '.join((candidate or '').split())
        if candidate and candidate.casefold()!=name.removeprefix('Execute: ').casefold():
            context=ellipsize(candidate,160)
            break
    if not context:
        context=('Confirmed commitment · ' if (record['request'] or '').startswith('Execute confirmed commitment:') else '')+job['org']
        if job['project']:
            context+=' · '+job['project']
    state=job['status']
    if state in {'waiting_input','blocked','failed'}:
        next_step=record['question'] or 'Open the task to provide the missing information or review the error.'
    elif state=='awaiting_approval':
        next_step='Open the exact action preview and approve or reject it.'
    elif state=='completed':
        next_step='Optionally review the saved deliverable.' if job['id'] in reviews else 'No further steps recorded.'
    elif state=='cancelled':
        next_step='No further work is scheduled.'
    elif state=='capacity_paused':
        next_step='Open the task to retry when model capacity is available.'
    elif state=='waiting_capacity':
        next_step='COS will retry when model capacity is available.'
    elif brief.get('next_step'):
        next_step='Saved plan: '+brief['next_step']
    else:
        next_step={'queued':'COS will start when a worker is available.',
                   'running':'COS is working on this task.',
                   'waiting_children':'Waiting for delegated work; open details to check any blockers.'}.get(state,'Open the task for its latest progress.')
    return {'job':name,'context':context,'status':label(state)+('\nReady for review' if job['id'] in reviews else ''),
            'next_step':ellipsize(next_step,220),
            'command':settings()['command_prefix']+' status '+short_reference(job['id'],identities),
            'updated':timestamp(job['updated'])}


def respond(store, query=''):
    """Resolve IDs or names without inference, job mutation, or reading agent history."""
    query = query.strip()
    if len(query) > 1000:
        raise ValueError('Use a task name or ID under 1,000 characters.')
    prefix = settings()['command_prefix']
    offset = 0
    paged = re.fullmatch(r'page\s+(\d+)(?:\s+(.+))?', query, re.I | re.S)
    if paged:
        offset, query = int(paged[1]), (paged[2] or '').strip()
        if offset > 1000000:
            raise ValueError('Use a task list offset up to 1000000.')
    literal = query.casefold().startswith('find ')
    if literal:
        query = query[5:].strip()
        if not query:
            raise ValueError('Enter a task name after find.')
    if len(query) >= 2 and query[0] == query[-1] and query[0] in {'"', "'"}:
        query = query[1:-1].strip()
    jobs = store.rows('''SELECT id,parent_id,title,org,project,status,created,updated FROM jobs
        WHERE parent_id IS NULL
        ORDER BY CASE WHEN status IN ('completed','cancelled') THEN 1 ELSE 0 END,updated DESC,id''')
    add_display_names(store, jobs)
    identities=[j['id'] for j in jobs]
    reviews = {row['job_id'] for row in store.rows('''SELECT r.job_id FROM result_reviews r JOIN jobs j ON j.id=r.job_id
        WHERE j.parent_id IS NULL AND j.status<>'cancelled' AND r.eligible=1 AND r.reviewed IS NULL''')}
    counts = Counter(j['status'] for j in jobs)
    if query:
        identity = re.fullmatch(r'(?:(?:task|job):)?([a-f0-9]{6,32})', query, re.I)
        if identity and not literal:
            matches = [j for j in jobs if j['id'].startswith(identity[1].lower())]
        elif not literal and re.match(r'^(?:task|job):', query, re.I):
            matches = []
        else:
            terms = query.casefold().split()
            matches = [j for j in jobs if all(term in ' '.join(
                (j['display_name'], j['title'], j['project'] or '')).casefold() for term in terms)]
        if not matches:
            return f'No top-level task matches “{query}”.\nList tasks: {prefix} status\nSearch by name: {prefix} status find <name>'
        if len(matches) == 1 and not paged:
            return detail(store, matches[0], reviews,identities)
        heading = f'## Task matches\n\n**{len(matches):,} matches** for “{query}”\nOpen a task with its command below.'
        jobs = matches
    else:
        active = sum(n for status, n in counts.items() if status not in TERMINAL)
        config = settings()
        monitoring = store.state('monitor_status', {}).get('state', 'starting') if config['monitor_enabled'] else 'paused'
        heading = (f'## Task status\n\n**{active:,} active** · **{len(reviews):,} ready for review**\n'
                   f'{len(jobs):,} total · {counts["completed"]:,} completed · {counts["cancelled"]:,} cancelled\n\n'
                   f'Queue: {"paused" if config["queue_paused"] else "enabled"} · Monitoring: {monitoring.replace("_"," ")}')
        active_counts=[label(status)+': '+f'{counts[status]:,}' for status in sorted(counts) if status not in TERMINAL]
        if active_counts:
            heading += '\n'+' · '.join(active_counts)
        heading += '\n\n### Recent tasks' if not active else '\n\n### Active tasks first'
    def message(rows):
        lines=[heading,status_table(rows) if rows else 'No tasks on this page.']
        if rows and offset+len(rows)<len(jobs):
            suffix=(' find '+query) if query else ''
            lines.append(f'**More tasks**\n`{prefix} status page {offset+len(rows)}{suffix}`')
        lines.append(f'**Find a task by name**\n`{prefix} status <task name>`')
        return '\n\n'.join(lines)
    rows=[]
    for job in jobs[offset:offset+PAGE_SIZE]:
        candidate=rows+[table_row(store,job,identities,reviews)]
        body=message(candidate)
        # A table is atomic: reduce rows instead of splitting markup/JSON mid-row.
        if len(body)>5500 or len(render('COS inbox\n\n'+body).encode('utf-8'))>MAX_HTML_BYTES:
            if not rows:
                raise ValueError('This status row is too large. Request the task by its exact ID.')
            break
        rows=candidate
    return message(rows)


def detail(store, task, reviews,identities=None):
    prefix = settings()['command_prefix']
    # Re-read live state after discovery. A result's review status is independent
    # of whether its task has resumed for newer activity.
    job = store.one('''SELECT id,parent_id,title,org,project,status,created,updated,request,result,question
        FROM jobs WHERE id=?''', (task['id'],))
    if not job or job['parent_id']:
        raise ValueError('This top-level task is unavailable. Request a fresh status list.')
    row=table_row(store,{**task,**job},identities or [job['id']],reviews)
    lines = ['## Task details',status_table([row]),
             'Organization: '+job['org'], '### Original request\n'+job['request'][:1200]]
    if job['project']:
        lines.append('Project: '+job['project'])
    if job['question']:
        lines.append('### Needs your attention\n'+job['question'])
    saved = store.one('SELECT body,updated FROM job_briefs WHERE job_id=?', (job['id'],))
    if saved:
        brief = json.loads(saved['body'])
        for key, heading in (('status_summary', 'Latest saved progress'), ('next_step', 'Saved next step')):
            if brief.get(key):
                lines.append('### '+heading+'\nRecorded '+timestamp(saved['updated'])+'\n'+str(brief[key]))
    if job['status'] == 'completed' and job['result']:
        lines.append('### Result summary\n'+job['result'][:1200])
    descendants = store.rows('''WITH RECURSIVE tree(id) AS (
        SELECT id FROM jobs WHERE parent_id=? UNION SELECT j.id FROM jobs j JOIN tree t ON j.parent_id=t.id)
        SELECT j.id,j.title,j.org,j.status,j.question,j.project,j.parent_id FROM jobs j JOIN tree t ON t.id=j.id
        WHERE j.id<>? ORDER BY j.updated DESC,j.id''', (job['id'], job['id']))
    if descendants:
        counts = Counter(child['status'] for child in descendants)
        lines.append('### Delegated work\n'+str(len(descendants))+' task(s) · '+
                     ' · '.join(label(status)+': '+str(counts[status]) for status in sorted(counts)))
        blocked = [child for child in descendants if child['status'] in ATTENTION]
        add_display_names(store, blocked[:PAGE_SIZE])
        for child in blocked[:PAGE_SIZE]:
            lines.append('Delegated item needing attention: job:'+child['id']+' · '+child['display_name']+
                         '\n'+label(child['status'])+('\n'+child['question'] if child['question'] else '')+
                         f'\nOpen: {prefix} show job:'+child['id'])
        if len(blocked) > PAGE_SIZE:
            lines.append(f'{len(blocked)-PAGE_SIZE} more delegated items need attention. Use {prefix} attention for the full queue.')
    if job['id'] in reviews and job['status'] != 'cancelled':
        lines.append(f'A saved result is ready for review. Open: {prefix} show result:'+job['id'])
    if job['status'] != 'cancelled':
        lines.append(f'Open task details and available actions: {prefix} show job:'+job['id'])
    lines.append('Task: job:'+job['id']+f'\nRefresh: {prefix} status job:'+job['id'])
    return '\n\n'.join(lines)
