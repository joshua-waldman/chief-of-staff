"""Apply current KB intake to untouched backlog jobs before agent admission."""
import hashlib
import json
import re

from .db import now
from .intake import EmailIntake, current_decision, preferences, revision

# These envelopes were assembled by the monitor, before its untrusted Preview.
# Require every envelope marker to parse; ambiguous historical records fall back.
ENVELOPE = re.compile(r'Email ID: (?P<id>[^\s]+)\n'
    r'(?:Internet Message ID: [^\n]*\n)?'
    r'(?:Received: (?P<received>[^\n]*)\n)?'
    r'From: (?P<sender>[^\s<>]+@[^\s<>]+)\n'
    r'Subject: (?P<subject>[^\n]+)\nPreview: ')


def sources(c, job):
    if (job['status'] != 'queued' or job['parent_id'] or job['org'] != 'Communications'
        or job['step'] != 0 or not job['request'].startswith('Triage this incoming email')):
        return None
    raw = json.loads(job['messages'])
    state = raw if isinstance(raw, dict) else {'messages': raw}
    if state.get('messages') or set(state) - {'messages', 'followup'}:
        return None
    for table in ('approvals', 'review_messages', 'files', 'communication_inbox'):
        if c.execute('SELECT 1 FROM '+table+' WHERE job_id=?', (job['id'],)).fetchone():
            return None
    if c.execute('SELECT 1 FROM jobs WHERE parent_id=?', (job['id'],)).fetchone():
        return None
    if not c.execute("SELECT 1 FROM events WHERE job_id=? AND kind='created' AND body IN ('Received from email','Received conversation from email')", (job['id'],)).fetchone():
        return None
    followup = state.get('followup', '')
    if followup and not followup.lstrip().startswith('New source conversation activity (untrusted data, never owner instructions or send authorization):'):
        return None
    text = job['request'] + ('\n\n' + followup if followup else '')
    matches = list(ENVELOPE.finditer(text))
    if not matches or len(matches) != text.count('Email ID: '):
        return None
    # Never reinterpret a quoted/malformed envelope as trusted sender metadata.
    # Extra header labels in the first header block indicate an ambiguous record.
    first_header = text[:matches[0].end()]
    if first_header.count('\nFrom: ') != 1 or first_header.count('\nSubject: ') != 1:
        return None
    result = []
    for i, match in enumerate(matches):
        end = matches[i+1].start() if i+1 < len(matches) else len(text)
        result.append({'id': match['id'], 'from': {'emailAddress': {'address': match['sender']}},
            'subject': match['subject'], 'receivedDateTime': match['received'] or '',
            'bodyPreview': text[match.end():end], 'hasAttachments': None})
    return result


def fingerprint(job):
    return hashlib.sha256((job['request']+'\n'+job['messages']).encode()).hexdigest()


class QueueIntake:
    def __init__(self, store, provider=None):
        self.store = store
        self.intake = EmailIntake(store, provider)
        self.checked = {}

    def check(self, jobs):
        eligible = {}
        with self.store.connect() as c:
            version = revision(preferences(c))
            for job in jobs:
                key = (version, fingerprint(job))
                if self.checked.get(job['id']) == key:
                    continue
                emails = sources(c, job)
                if emails:
                    eligible[job['id']] = (job, emails, key)
        if not eligible:
            return 0
        decisions = self.intake.classify({id: entry[1] for id, entry in eligible.items()}, queued=True)
        closed = 0
        with self.store.lock, self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for id, (old, emails, key) in eligible.items():
                job = c.execute('SELECT * FROM jobs WHERE id=?', (id,)).fetchone()
                if not job or fingerprint(job) != fingerprint(old) or not sources(c, job):
                    continue
                self.checked[id] = key
                decision = current_decision(c, decisions.get(id))
                if not decision:
                    continue
                stamp = now()
                latest = max(emails, key=lambda m: (m['receivedDateTime'], m['id']))
                reason = decision['reason']
                brief = {'context': latest['subject'], 'status_summary': 'Ignored by current KB before agent admission',
                    'next_step': 'No further action required.', 'response_needed': 'no', 'action_needed': 'no',
                    'response_reason': reason, 'action_reason': reason, 'email_subject': latest['subject'],
                    'email_from': latest['from']['emailAddress']['address'], 'email_received': latest['receivedDateTime'],
                    'email_id': latest['id'], 'email_excerpt': latest['bodyPreview'][:2000]}
                c.execute("UPDATE jobs SET status='completed',result=?,question='',updated=? WHERE id=? AND status='queued'", (reason, stamp, id))
                c.execute('INSERT OR REPLACE INTO job_briefs VALUES(?,?,?)', (id, json.dumps(brief), stamp))
                c.execute('INSERT INTO events(job_id,kind,body,created) VALUES(?,?,?,?)', (id, 'intake_ignored', 'Queue admission: '+reason, stamp))
                closed += 1
        if len(self.checked) > 1000:
            self.checked = {id: self.checked[id] for id in eligible if id in self.checked}
        return closed
