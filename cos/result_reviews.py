"""Review generated work, not routine conversation triage or action receipts."""
import json
import re


def review_content(summary, files):
    """Describe the saved review snapshot, never the conversation's latest state."""
    context, separator, delegated = summary.partition('\n\nDelegated work for your review\n\n')
    if not separator and files:
        items = [{'title': f['name'], 'kind': 'document', 'content': '',
                  'file_ids': [f['id']]} for f in files]
        return {'review_title': ', '.join(f['name'] for f in files),
                'items': items, 'context': summary.strip()}
    bodies = re.split(r'(?m)^[^\n]+ · [^\n]+\n\n', delegated) if separator else [summary]
    items = []
    covered = set()
    for body in bodies:
        body = body.strip()
        if not body or any(item['content'] == body for item in items):
            continue
        linked = [f for f in files if '/api/files/'+f['id'] in body]
        first = body.splitlines()[0].strip().strip('#* _')
        heading = re.match(r'^(?:#{1,6}\s+([^\n]+)|\*\*([^\n]+?)\*\*)', body)
        # A generated analysis may cite transcripts and repository evidence.
        # Its heading identifies the work; those source filenames do not.
        title = (heading.group(1) or heading.group(2)).strip() if heading else ', '.join(f['name'] for f in linked) or first
        items.append({'title': title[:180], 'kind': 'document' if linked else 'inline',
                      'content': body, 'file_ids': [f['id'] for f in linked]})
        covered.update(f['id'] for f in linked)
    for file in files:
        if file['id'] not in covered:
            items.append({'title': file['name'], 'kind': 'document', 'content': '',
                          'file_ids': [file['id']]})
    title = items[0]['title'] if items else 'Saved review content unavailable'
    if len(items) > 1:
        title += ' (+'+str(len(items)-1)+' more)'
    return {'review_title': title, 'items': items,
            'context': context.strip() if separator else ''}

KINDS = {'strategy', 'generated_content', 'triage', 'action_receipt'}
OPERATIONS = {'delete_email', 'mark_email_read', 'mark_teams_read', 'calendar_rsvp',
              'send_email', 'reply_email', 'send_teams', 'jira_write', 'confluence_write',
              'merge_code', 'deploy_code'}


def own_work(connection, job, state):
    if state.get('source_only'):
        return False
    kind = state.get('result_kind')
    if kind is not None:
        return kind in {'strategy', 'generated_content'}
    # Compatibility for saved finishes from before result kinds existed.
    # Older tool/triage histories lack finish metadata entirely. Organization
    # alone is not evidence that those investigations produced owner work.
    if 'deliverable_file_ids' not in state:
        return False
    selected = set(state.get('deliverable_file_ids', []))
    if selected:
        for row in connection.execute("SELECT result FROM approvals WHERE job_id=? AND status='executed' AND tool IN ('create_document','create_presentation','write_file')", (job['id'],)):
            try:
                output = json.loads(row['result'])
            except (TypeError, ValueError):
                continue
            if isinstance(output, dict) and output.get('file_id') in selected:
                return True
    if job['org'] != 'Communications':
        # Preserve routed work tasks, but do not infer generated content from an
        # operation's completion message when no output was selected.
        operations = connection.execute("SELECT tool FROM approvals WHERE job_id=? AND status='executed'", (job['id'],)).fetchall()
        return not any(row['tool'] in OPERATIONS for row in operations)
    return False


def delegated_work(connection, job, seen=None, reviewed_before=None):
    """Keep completed descendant work reachable through one parent review."""
    seen = set(seen or ())
    if job['id'] in seen:
        return
    seen.add(job['id'])
    if reviewed_before is None:
        reviewed_before = connection.execute("SELECT MAX(created) FROM events WHERE job_id=? AND kind='result_reviewed'", (job['id'],)).fetchone()[0] or ''
    for child in connection.execute("SELECT * FROM jobs WHERE parent_id=? AND status<>'cancelled' ORDER BY created,id", (job['id'],)).fetchall():
        try:
            state = json.loads(child['messages'])
        except (TypeError, ValueError):
            state = None
        if child['status'] == 'completed' and child['updated'] > reviewed_before and isinstance(state, dict) and own_work(connection, child, state):
            yield child, state
        yield from delegated_work(connection, child, seen, reviewed_before)


def eligible(connection, job, state, seen=None):
    return own_work(connection, job, state) or next(delegated_work(connection, job, seen), None) is not None


def review_payload(connection, job, state, summary):
    """Snapshot actual drafts and declared files, even if a director omits them."""
    files = list(dict.fromkeys(state.get('deliverable_file_ids', [])))
    sections = []
    for child, child_state in delegated_work(connection, job):
        text = child['result'].strip()
        if text and text not in summary and not any(text in section for section in sections):
            sections.append(child['org']+' · '+child['title']+'\n\n'+text)
        for file_id in child_state.get('deliverable_file_ids', []):
            if file_id not in files:
                files.append(file_id)
    if sections:
        summary += '\n\nDelegated work for your review\n\n'+'\n\n'.join(sections)
    return summary, files


def reconcile(connection, root_ids=None, *, event_kind='result_review_repaired'):
    """Recover returned work without executing jobs or acknowledging owner review.

    Completed descendants can return while their director is still waiting or
    processing new messages. Migration archives are recoverable; user cancellations
    are not. Keep one review on the original root rather than copying work into
    each topic that mentions it.
    """
    from .db import now, uid
    repaired = []
    query = 'SELECT * FROM jobs WHERE parent_id IS NULL'
    args = ()
    if root_ids is not None:
        if not root_ids:
            return repaired
        query += ' AND id IN ('+','.join('?' for _ in root_ids)+')'
        args = tuple(root_ids)
    for job in connection.execute(query, args).fetchall():
        archived = job['status'] == 'cancelled'
        if archived:
            migrated = connection.execute("SELECT 1 FROM events WHERE job_id=? AND kind='topic_migrated'", (job['id'],)).fetchone()
            cancelled = connection.execute("SELECT 1 FROM events WHERE job_id=? AND kind='cancelled'", (job['id'],)).fetchone()
            if not migrated or cancelled:
                continue
        try:
            state = json.loads(job['messages'])
        except (TypeError, ValueError):
            continue
        if state == []:
            state = {}
        if not isinstance(state, dict):
            continue
        previous = connection.execute('SELECT * FROM result_reviews WHERE job_id=?', (job['id'],)).fetchone()
        children = list(delegated_work(connection, job))
        # A reviewed own result stays reviewed. New delegated work can still
        # arrive later and must get its own revision.
        own = (job['status'] == 'completed' and 'deliverable_file_ids' in state
               and own_work(connection, job, state) and not (previous and previous['reviewed']))
        if not children and not own:
            continue
        summary = job['result'] if job['status'] == 'completed' else ''
        saved_files = []
        if previous and previous['eligible'] and not previous['reviewed']:
            # Reassessment can replace the job result with unrelated triage;
            # preserve the actual pending snapshot, including its old files.
            summary = previous['summary']
            saved_files = json.loads(previous['file_ids'])
        if not summary.strip():
            summary = 'Completed delegated work from this request.'
        payload_state = state if own else {'deliverable_file_ids': []}
        summary, files = review_payload(connection, job, payload_state, summary)
        files = list(dict.fromkeys(saved_files+files))
        if previous and previous['eligible'] and not previous['reviewed'] and previous['summary'] == summary and json.loads(previous['file_ids']) == files and not archived:
            continue
        stamp = now()
        completed = max([child['updated'] for child, _ in children] + ([job['updated']] if own else []))
        connection.execute('''INSERT OR REPLACE INTO result_reviews
            (job_id,revision,summary,file_ids,completed,reviewed,eligible) VALUES(?,?,?,?,?,NULL,1)''',
            (job['id'], uid(), summary, json.dumps(files), completed))
        if archived:
            # Freeze the historical director; restoring a review must never
            # queue the obsolete chat-wide job or replay its actions.
            connection.execute("UPDATE jobs SET status='completed',question='' WHERE id=?", (job['id'],))
        connection.execute('INSERT INTO events(job_id,kind,body,created) VALUES(?,?,?,?)',
            (job['id'], event_kind, 'Returned completed work to the owner review queue without restarting the job.', stamp))
        repaired.append(job['id'])
    return repaired
