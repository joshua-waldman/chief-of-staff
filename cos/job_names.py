"""Compact list labels; full source requests stay in job details."""
import re
import json
from html import unescape


NAMING_POLICY = """Include job_name when calling update_brief: a clear 3-7 word
name for the job's main topic or outcome (for example, 'Release readiness review'
or 'Quarterly budget approval'). Use a short noun phrase, not a detailed request,
status report or list of steps. Never include message, chat, conversation or
meeting IDs, URLs, timestamps, or source metadata. Keep it under 60 characters.
"""


def compact_name(value):
    value = re.sub(r'https?://\S+|\b\S+@\S+\b', '', value or '', flags=re.I)
    value = re.sub(r'\b(?:teams\s+)?(?:message|meeting|chat|conversation|thread|email)[ _-]*(?:id\b\s*[:=#-]?|#)\s*\S+', '', value, flags=re.I)
    value = re.sub(r'\b[0-9a-f]{8}-[0-9a-f-]{27,}\b|\b\d{10,}\b|\b\S{32,}\b', '', value, flags=re.I)
    value = re.sub(r'^(?:(?:re|fw|fwd)\s*:\s*)+', '', value.strip(), flags=re.I)
    value = re.sub(r'^(?:please\s+|can you\s+|could you\s+)', '', value, flags=re.I)
    value = re.split(r'[\n.!?;]|\s+[—–]\s+', value)[0]
    value = ' '.join(value.split()[:7]).strip(' :,-#')
    if len(value) > 60:
        value = value[:61].rsplit(' ', 1)[0].rstrip(' :,-#')
    return value


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


def display_name(job, brief, sources=()):
    for candidate in (brief.get('job_name'), brief.get('email_subject'), brief.get('context'), job.get('title'), brief.get('requested_action'), brief.get('email_excerpt')):
        name = usable_name(candidate)
        if name:
            return name
    return source_topic(sources, meeting='19:meeting_' in (job.get('title') or '')) or (
        'Conversation review' if job.get('org') == 'Communications' else 'Task review')


def add_display_names(store, jobs, briefs=None):
    """Share names between lists and details, using cached evidence in one batch.

    Naming is read-only: never trigger inference or fetch Teams messages from GETs.
    Source bodies are used locally and are not attached to the list response.
    """
    if not jobs:
        return
    ids = list(dict.fromkeys(job['id'] for job in jobs))
    if briefs is None:
        marks = ','.join('?' for _ in ids)
        briefs = {row['job_id']: json.loads(row['body']) for row in store.rows(
            'SELECT job_id,body FROM job_briefs WHERE job_id IN ('+marks+')', ids)}
    unresolved = []
    for job in jobs:
        job['display_name'] = display_name(job, briefs.get(job['id'], {}))
        if job['org'] == 'Communications' and job['display_name'] == 'Conversation review':
            unresolved.append(job['id'])
    sources = {}
    if unresolved:
        marks = ','.join('?' for _ in unresolved)
        rows = store.rows('''SELECT job_id,subject,substr(body,1,2000) AS body FROM (
            SELECT job_id,subject,body,row_number() OVER (
                PARTITION BY job_id ORDER BY received,message_id) AS position
            FROM communication_sources WHERE job_id IN ('''+marks+')) WHERE position<=32', unresolved)
        for row in rows:
            sources.setdefault(row['job_id'], []).append(row)
        for job in jobs:
            if job['id'] in sources:
                job['display_name'] = display_name(job, briefs.get(job['id'], {}), sources[job['id']])
