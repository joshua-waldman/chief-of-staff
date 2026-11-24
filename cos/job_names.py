"""Compact list labels; full source requests stay in job details."""
import re
import json
from html import unescape


NAMING_POLICY = """Include job_name when calling update_brief: a clear, descriptive
name for the job's main topic or outcome (for example, 'Release readiness review'
or 'Quarterly budget approval'). Use a short noun phrase, not a detailed request,
status report or list of steps. Never include message, chat, conversation or
meeting IDs, URLs, timestamps, or source metadata. Preserve the action and distinguishing
deliverable or context; do not cut names to a fixed word count. Keep it within 120
characters, using an ellipsis when shortening is necessary.
"""


TRAILING_WORDS={'a','an','the','and','or','for','of','to','in','on','at','by','with','from','into'}


def ellipsize(value,limit=120):
    value=' '.join((value or '').split())
    if len(value)<=limit:
        return value
    shortened=value[:limit-1]
    if shortened.rfind(' ')>=limit//2:
        shortened=shortened.rsplit(' ',1)[0]
    while shortened.rsplit(' ',1)[-1].casefold() in TRAILING_WORDS and ' ' in shortened:
        shortened=shortened.rsplit(' ',1)[0]
    return shortened.rstrip(' ,;:-')+'…'


def compact_name(value):
    value = re.sub(r'https?://\S+|\b\S+@\S+\b', '', value or '', flags=re.I)
    value = re.sub(r'\b(?:teams\s+)?(?:message|meeting|chat|conversation|thread|email)[ _-]*(?:id\b\s*[:=#-]?|#)\s*\S+', '', value, flags=re.I)
    value = re.sub(r'\b[0-9a-f]{8}-[0-9a-f-]{27,}\b|\b\d{10,}\b|\b[0-9a-f]{32,}\b', '', value, flags=re.I)
    value = re.sub(r'^(?:(?:re|fw|fwd)\s*:\s*)+', '', value.strip(), flags=re.I)
    value = re.sub(r'^(?:please\s+|can you\s+|could you\s+)', '', value, flags=re.I)
    pieces=re.split(r'\n|[.!?](?=\s|$)',value,maxsplit=1)
    value=re.sub(r'^#{1,6}\s+','',' '.join(pieces[0].split())).strip(' :,-')
    omitted=len(pieces)>1 and bool(pieces[1].strip())
    while value.rsplit(' ',1)[-1].casefold() in TRAILING_WORDS and ' ' in value:
        value=value.rsplit(' ',1)[0]
        omitted=True
    return ellipsize(value+('…' if omitted and not value.endswith('…') else ''))


def usable_name(value):
    name = compact_name(value)
    if not name or not re.search(r'[a-zA-Z]', name):
        return ''
    if re.match(r'^(?:Teams conversation|Triage this incoming|Message ID|Chat ID|Conversation review|Task review)\b', name, re.I):
        return ''
    return name


def source_topic(sources, meeting=False):
    # Prefer explicit topics over message prose. Some older Teams system events
    # were cached as flattened text, with a timestamp and sender MRI before the topic.
    topics, messages = [], []
    for source in sources:
        if source.get('subject'):
            topics.append(source['subject'])
        body = unescape(source.get('body') or '').strip()
        tags = re.findall(r'<(?:topic|subject|title|meetingTitle)\b[^>]*>([^<]+)</(?:topic|subject|title|meetingTitle)>', body, re.I)
        topics.extend(tags)
        match = re.fullmatch(r'(?:\d{10,})?8:orgid:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}(.+)', body, re.I)
        if match:
            topics.append(match[1])
        if meeting and body.endswith('Play'):
            topics.append(body[:-4])
        # Metadata blobs and system events do not describe the job's purpose.
        if not re.search(r'orgid:|callStarted|callEnded|ChatEnabled|Recurring|https?://|[{}]', body, re.I):
            messages.append(re.sub(r'<[^>]+>', ' ', body))
    for value in topics + messages:
        if re.search(r'orgid:|callStarted|callEnded|ChatEnabled|Recurring|https?://|[{}]', value, re.I):
            continue
        name = usable_name(value)
        if name and name.lower() not in {'play', 'true', 'false', 'thanks', 'thank you', 'ok', 'okay'}:
            return name
    return ''


def source_label(brief, sources):
    """Use original source metadata without compacting people's names or subjects."""
    emails = [s for s in sources if s.get('source') == 'email']
    if emails:
        first = emails[0]
        return ' · '.join(filter(None, (first.get('sender') or brief.get('email_from'),
                                       first.get('subject') or brief.get('email_subject'))))
    if brief.get('email_from') and brief.get('email_subject'):
        return brief['email_from'] + ' · ' + brief['email_subject']
    return ', '.join(dict.fromkeys(s['sender'] for s in sources
        if s.get('source') == 'teams' and s.get('sender') not in (None, '', 'You', 'other participant')
        and not re.search(r'orgid:|https?://', s['sender'], re.I)))


def primary_name(job, brief, sources=()):
    if job.get('user_name'):
        return job['user_name']
    if job.get('original_task_name'):
        recovered=usable_name(job['original_task_name'])
        if recovered:
            return recovered
    name = ''
    for candidate in (brief.get('job_name'), brief.get('email_subject'), job.get('title'), brief.get('requested_action'), brief.get('context'), brief.get('email_excerpt')):
        name = usable_name(candidate)
        if name:
            break
    # Older briefs stored the seven-word prefix. Expand only when a retained
    # title/context actually begins with that same prefix; never invent a name.
    old=(brief.get('job_name') or '').strip().rstrip('…')
    if old and (len(old.split())>=7 or len(old)>=50 or old.rsplit(' ',1)[-1].casefold() in TRAILING_WORDS):
        for candidate in (brief.get('requested_action'),job.get('title'),brief.get('context')):
            candidate=compact_name(candidate)
            if candidate.casefold().startswith(old.casefold()+' '):
                name=compact_name(candidate)
                break
    name = name or source_topic(sources, meeting='19:meeting_' in (job.get('title') or ''))
    return name


def display_name(job, brief, sources=(), label=''):
    if job.get('user_name'):
        return job['user_name']
    name=primary_name(job,brief,sources)
    label = ' '.join((label or source_label(brief, sources)).split())
    if label:
        return name + ' — ' + label if name and name.casefold() not in label.casefold() else label
    return name or ('Conversation review' if job.get('org') == 'Communications' else 'Task review')


def add_display_names(store, jobs, briefs=None):
    """Share names between lists and details, using cached evidence in one batch.

    Naming is read-only: never trigger inference or fetch Teams messages from GETs.
    Source bodies are used locally and are not attached to the list response.
    """
    if not jobs:
        return
    ids = list(dict.fromkeys(job['id'] for job in jobs))
    marks = ','.join('?' for _ in ids)
    retained_requests={row['job_id']:row for row in store.rows('''SELECT j.id AS job_id,e.commitment_id,
        substr(j.request,1,1400) AS request FROM jobs j LEFT JOIN commitment_execution e ON j.id=e.job_id
        WHERE j.id IN ('''+marks+') AND (e.job_id IS NOT NULL OR length(j.title)>=90)',ids)}
    for job in jobs:
        execution=retained_requests.get(job['id'])
        if execution and execution['commitment_id'] and execution['request'].startswith('Execute confirmed commitment: '):
            original,separator,_=execution['request'].partition('\nCommitment ID: '+execution['commitment_id']+'\n')
            if separator:
                job['original_task_name']='Execute: '+original.removeprefix('Execute confirmed commitment: ')
    names = {row['job_id']: row['name'] for row in store.rows(
        'SELECT job_id,name FROM job_names WHERE job_id IN ('+marks+')', ids)}
    for job in jobs:
        job['user_name'] = names.get(job['id'], '')
    if briefs is None:
        marks = ','.join('?' for _ in ids)
        briefs = {row['job_id']: json.loads(row['body']) for row in store.rows(
            'SELECT job_id,body FROM job_briefs WHERE job_id IN ('+marks+')', ids)}
    for job in jobs:
        original=retained_requests.get(job['id'])
        if original and not original['commitment_id'] and original['request'].startswith(job.get('title','')):
            full=original['request'].split('\n',1)[0]
            previous=(briefs.get(job['id'],{}).get('job_name') or '').rstrip('…')
            if len(full)>len(job.get('title','')) and (not previous or compact_name(full).casefold().startswith(previous.casefold()+' ')):
                job['original_task_name']=full
    ancestry = {}
    for row in store.rows('''WITH RECURSIVE lineage(job_id,ancestor_id,parent_id,depth) AS (
            SELECT id,id,parent_id,0 FROM jobs WHERE id IN ('''+marks+''')
            UNION ALL SELECT l.job_id,j.id,j.parent_id,l.depth+1 FROM lineage l
            JOIN jobs j ON j.id=l.parent_id WHERE l.depth<64)
            SELECT job_id,ancestor_id FROM lineage ORDER BY depth''', ids):
        ancestry.setdefault(row['job_id'], []).append(row['ancestor_id'])
    ids = list(dict.fromkeys([*ids, *(ancestor for parents in ancestry.values() for ancestor in parents)]))
    marks = ','.join('?' for _ in ids)
    # Labels are shared by every topic in a chat. Fetch metadata for all jobs,
    # even those with an inferred name, so the source stays visible after triage.
    labels = {}
    for row in store.rows('''SELECT c.job_id,l.source,l.name,l.subject
            FROM communication_threads c LEFT JOIN teams_topics t ON t.topic_id=c.thread_id AND c.source='teams'
            JOIN communication_labels l ON l.source=c.source AND l.thread_id=COALESCE(t.chat_id,c.thread_id)
            WHERE c.job_id IN ('''+marks+')', ids):
        label = (row['name'] or row['subject']) if row['source'] == 'teams' else ' · '.join(filter(None, (row['name'], row['subject'])))
        labels.setdefault(row['job_id'], []).append(label)
    sources = {}
    if ids:
        rows = store.rows('''SELECT job_id,source,sender,subject,substr(body,1,2000) AS body FROM (
            SELECT job_id,source,sender,subject,body,row_number() OVER (
                PARTITION BY job_id ORDER BY received,message_id) AS position
            FROM communication_sources WHERE job_id IN ('''+marks+')) WHERE position<=32', ids)
        for row in rows:
            sources.setdefault(row['job_id'], []).append(row)
    for job in jobs:
        label = ''
        for ancestor in ancestry.get(job['id'], [job['id']]):
            label = ' / '.join(labels.get(ancestor, ())) or source_label({}, sources.get(ancestor, ()))
            if label:
                break
        job['task_name']=primary_name(job,briefs.get(job['id'],{}),sources.get(job['id'],()))
        job['display_name'] = display_name(job, briefs.get(job['id'], {}), sources.get(job['id'], ()),
                                           label)
        job.pop('original_task_name',None)
