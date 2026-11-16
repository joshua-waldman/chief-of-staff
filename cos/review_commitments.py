"""Match saved review content to obligations; only owner choices change status."""
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

from .commitments import add_source, digest, history
from .db import now
from .knowledge import words

SCHEMA = '''
CREATE TABLE IF NOT EXISTS review_commitment_links(
 job_id TEXT NOT NULL, revision TEXT NOT NULL, commitment_id TEXT NOT NULL,
 evidence_id TEXT NOT NULL, relationship TEXT NOT NULL, summary TEXT NOT NULL,
 files TEXT NOT NULL, created TEXT NOT NULL,
 PRIMARY KEY(job_id,revision,commitment_id));
CREATE INDEX IF NOT EXISTS review_commitment_evidence ON review_commitment_links(evidence_id);
'''


@lru_cache(maxsize=32)
def document_text(path, sha256):
    from .tools import extract
    try:
        return extract(Path(path))[:50000]
    except Exception:
        # An unreadable attachment can still be reviewed directly by its owner.
        return ''


def snapshot(connection, review):
    """Bind the confirmation to the summary and exact declared file bytes."""
    files = []
    text = [review['summary']]
    ids = list(json.loads(review['file_ids']))
    # Include source documents shown as links in an inline review as well as
    # declared deliverables. A final synthesized review deliberately excludes
    # incidental research links, just like Store.detail's review file list.
    job = connection.execute("SELECT CASE WHEN json_valid(messages) THEN json_extract(messages,'$.final_review') ELSE 0 END AS final_review FROM jobs WHERE id=?", (review['job_id'],)).fetchone()
    if not job or not job['final_review']:
        ids.extend(re.findall(r'/api/files/([A-Za-z0-9_-]+)', review['summary']))
    from .tools import is_recovery_file
    for file_id in sorted(set(ids)):
        row = connection.execute('SELECT * FROM files WHERE id=?', (file_id,)).fetchone()
        if not row:
            files.append({'id': file_id, 'name': 'Unavailable file', 'sha256': None})
            continue
        if is_recovery_file(row['job_id'], row['path']):
            continue
        try:
            path = Path(row['path'])
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            sha = None
        files.append({'id': file_id, 'name': row['name'], 'sha256': sha})
        text.extend([row['name'], document_text(row['path'], sha) if sha else ''])
    return files, '\n'.join(text), digest(review['revision'], review['summary'], files)


def terms(text):
    # Small vocabulary equivalences help retrieve candidates, never prove completion.
    text = re.sub(r'\b(?:dashboard|hub)\b', 'portal', text, flags=re.I)
    text = re.sub(r'\b(?:accessing|access)\b', 'access', text, flags=re.I)
    return set(words(text)) - {'will', 'need', 'contact', 'learn', 'how', 'can', 'was', 'has', 'had', 'its', 'once', 'you',
                              'to', 'of', 'in', 'on', 'at', 'by', 'as', 'an', 'is', 'be', 'it', 'or', 'we', 'he',
                              'she', 'they', 'our', 'me', 'my', 'do', 'so', 'if', 'up', 'out', 'are', 'were'}


def review_context(store, review):
    with store.connect() as c:
        files, text, fingerprint = snapshot(c, {**review, 'file_ids': json.dumps(review['file_ids'])})
        job = c.execute('SELECT title,project FROM jobs WHERE id=?', (review['job_id'],)).fetchone()
        candidates = [dict(r) for r in c.execute("SELECT id,owner,deliverable,scope,status,version FROM commitments WHERE status IN ('open','proposed') ORDER BY updated DESC,id")]
        links = [dict(r) for r in c.execute('SELECT l.commitment_id,l.relationship,c.deliverable,c.status FROM review_commitment_links l JOIN commitments c ON c.id=l.commitment_id WHERE l.job_id=? AND l.revision=?', (review['job_id'], review['revision']))]
    content_terms = terms(text)
    context_terms = terms(job['title']) if job else set()
    matches = []
    for item in candidates:
        if job and job['project'] and item['scope'] not in {'global', job['project']}:
            continue
        wanted = terms(item['deliverable'])
        shared = wanted & content_terms
        # Require actual saved content overlap; the conversation title only helps rank.
        if len(shared) < 2:
            continue
        score = len(shared) / max(1, len(wanted)) + .05 * len(wanted & context_terms)
        if score < .2:
            continue
        excerpt = next((line.strip() for line in text.splitlines() if len(terms(line) & shared) >= 2), '')
        matches.append({**item, 'reason': 'Shared content: '+', '.join(sorted(shared)),
                        'excerpt': excerpt[:600], 'score': score})
    matches.sort(key=lambda r: (-r['score'], r['id']))
    return {'matches': matches[:5], 'choices': candidates, 'links': links,
            'snapshot_fingerprint': fingerprint}


def link_review(connection, review, selections, fingerprint):
    if not isinstance(selections, list) or len(selections) > 25:
        raise ValueError('Choose up to 25 commitments.')
    if not selections:
        return
    files, _, current = snapshot(connection, review)
    if fingerprint != current:
        raise ValueError('Review content or files changed; refresh before linking commitments.')
    if any(f['sha256'] is None for f in files):
        raise ValueError('A reviewed file is unavailable; restore it before linking commitments.')
    seen = set()
    for selection in selections:
        if not isinstance(selection, dict) or set(selection) != {'commitment_id', 'version', 'relationship'}:
            raise ValueError('Choose a commitment, current version and relationship.')
        identity = selection['commitment_id']
        relation = selection['relationship']
        if not isinstance(identity, str) or identity in seen or not isinstance(relation, str) or relation not in {'fulfills', 'progress', 'related'}:
            raise ValueError('Choose distinct commitments and valid relationships.')
        seen.add(identity)
        previous = connection.execute('SELECT relationship FROM review_commitment_links WHERE job_id=? AND revision=? AND commitment_id=?', (review['job_id'], review['revision'], identity)).fetchone()
        if previous:
            if previous['relationship'] != relation:
                raise ValueError('This review already has a different relationship; review the commitment record.')
            continue
        row = connection.execute('SELECT * FROM commitments WHERE id=?', (identity,)).fetchone()
        if not row or row['status'] not in {'open', 'proposed'} or type(selection['version']) is not int or row['version'] != selection['version']:
            raise ValueError('Commitment changed; refresh before confirming the relationship.')
        if relation == 'fulfills' and row['status'] != 'open':
            raise ValueError('Confirm the proposed assignment before completing it.')
        stamp = now()
        source = {'source_job': review['job_id'], 'source_file_id': '',
                  'source_identity': 'result-review:'+review['job_id']+':'+review['revision'],
                  'source_url': '', 'meeting_date': '', 'source_timestamp': '',
                  'excerpt': review['summary']}
        evidence_id, _ = add_source(connection, identity, source, 'completion' if relation == 'fulfills' else 'corroboration')
        connection.execute('UPDATE commitment_evidence SET reviewed=1 WHERE id=?', (evidence_id,))
        connection.execute('INSERT INTO review_commitment_links VALUES(?,?,?,?,?,?,?,?)',
                           (review['job_id'], review['revision'], identity, evidence_id, relation, review['summary'], json.dumps(files), stamp))
        status = 'completed' if relation == 'fulfills' else row['status']
        connection.execute('UPDATE commitments SET status=?,version=version+1,updated=? WHERE id=?', (status, stamp, identity))
        history(connection, identity, 'complete' if relation == 'fulfills' else 'review_linked', 'owner',
                'Owner confirmed the reviewed result '+relation+' this commitment.',
                {'evidence_id': evidence_id, 'review_job_id': review['job_id'], 'review_revision': review['revision'],
                 'relationship': relation, 'snapshot_fingerprint': current})
