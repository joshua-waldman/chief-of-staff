"""Read-only top-level task discovery and progress for the owner Teams inbox."""
import json
import re
from collections import Counter

from .config import settings
from .job_names import add_display_names

PAGE_SIZE = 15
TERMINAL = {'completed', 'cancelled'}
ATTENTION = {'waiting_input', 'awaiting_approval', 'blocked', 'failed', 'capacity_paused'}
LABELS = {'waiting_children': 'Waiting for delegated work', 'waiting_input': 'Your input needed',
          'awaiting_approval': 'Approval needed', 'waiting_capacity': 'Waiting for model capacity',
          'capacity_paused': 'Model capacity needs attention'}


def label(status):
    return LABELS.get(status, status.replace('_', ' ').capitalize())


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
            return detail(store, matches[0], reviews)
        heading = f'Top-level task matches for “{query}” · {len(matches)}\nChoose a task by its ID.'
        jobs = matches
    else:
        active = sum(n for status, n in counts.items() if status not in TERMINAL)
        config = settings()
        monitoring = store.state('monitor_status', {}).get('state', 'starting') if config['monitor_enabled'] else 'paused'
        heading = (f'Top-level task status · {len(jobs)} total\nActive: {active} · Completed: {counts["completed"]}'
                   f' · Cancelled: {counts["cancelled"]} · Ready for review: {len(reviews)}\n'
                   f'Queue: {"paused" if config["queue_paused"] else "enabled"} · Monitoring: {monitoring}')
        if counts:
            heading += '\n'+ ' · '.join(label(status)+': '+str(counts[status]) for status in sorted(counts))
    lines = [heading]
    for job in jobs[offset:offset+PAGE_SIZE]:
        lines.append('job:'+job['id']+' · '+job['display_name']+'\n'+label(job['status'])+
                     (' · Ready for review' if job['id'] in reviews else '')+' · Updated '+job['updated'])
    if offset >= len(jobs):
        lines.append('No tasks on this page.')
    if offset+PAGE_SIZE < len(jobs):
        suffix = (' find '+query) if query else ''
        lines.append(f'Next: {prefix} status page {offset+PAGE_SIZE}{suffix}')
    lines.append(f'Task details: {prefix} status job:<id>\nFind a task: {prefix} status <name or unique ID prefix>')
    return '\n\n'.join(lines)


def detail(store, task, reviews):
    prefix = settings()['command_prefix']
    # Re-read live state after discovery. A result's review status is independent
    # of whether its task has resumed for newer activity.
    job = store.one('''SELECT id,parent_id,title,org,project,status,created,updated,request,result,question
        FROM jobs WHERE id=?''', (task['id'],))
    if not job or job['parent_id']:
        raise ValueError('This top-level task is unavailable. Request a fresh status list.')
    lines = [task['display_name'], 'Task: job:'+job['id'], 'Status: '+label(job['status']),
             'Organization: '+job['org'], 'Updated: '+job['updated'], 'Request: '+job['request'][:1200]]
    if job['project']:
        lines.append('Project: '+job['project'])
    if job['question']:
        lines.append('Current question or blocker: '+job['question'])
    saved = store.one('SELECT body,updated FROM job_briefs WHERE job_id=?', (job['id'],))
    if saved:
        brief = json.loads(saved['body'])
        for key, title in (('status_summary', 'Latest saved progress'), ('next_step', 'Saved next step')):
            if brief.get(key):
                lines.append(title+' (recorded '+saved['updated']+'): '+str(brief[key]))
    if job['status'] == 'completed' and job['result']:
        lines.append('Result summary: '+job['result'][:1200])
    descendants = store.rows('''WITH RECURSIVE tree(id) AS (
        SELECT id FROM jobs WHERE parent_id=? UNION SELECT j.id FROM jobs j JOIN tree t ON j.parent_id=t.id)
        SELECT j.id,j.title,j.org,j.status,j.question,j.project,j.parent_id FROM jobs j JOIN tree t ON t.id=j.id
        WHERE j.id<>? ORDER BY j.updated DESC,j.id''', (job['id'], job['id']))
    if descendants:
        counts = Counter(child['status'] for child in descendants)
        lines.append('Delegated work: '+str(len(descendants))+' task(s) · '+
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
    lines.append(f'Refresh: {prefix} status job:'+job['id'])
    return '\n\n'.join(lines)
