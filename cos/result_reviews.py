"""Review generated work, not routine conversation triage or action receipts."""
import json

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
    for child in connection.execute("SELECT * FROM jobs WHERE parent_id=? AND status='completed' ORDER BY created,id", (job['id'],)).fetchall():
        try:
            state = json.loads(child['messages'])
        except (TypeError, ValueError):
            state = None
        if child['updated'] > reviewed_before and isinstance(state, dict) and own_work(connection, child, state):
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
