"""Review generated work, not routine conversation triage or action receipts."""
import json

KINDS = {'strategy', 'generated_content', 'triage', 'action_receipt'}
OPERATIONS = {'delete_email', 'mark_email_read', 'mark_teams_read', 'calendar_rsvp',
              'send_email', 'reply_email', 'send_teams', 'jira_write', 'confluence_write',
              'merge_code', 'deploy_code'}


def eligible(connection, job, state, seen=None):
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
    # Completed delegated work returns once through its originating request.
    seen = set(seen or ())
    if job['id'] in seen:
        return False
    seen.add(job['id'])
    for child in connection.execute("SELECT * FROM jobs WHERE parent_id=? AND status='completed'", (job['id'],)).fetchall():
        try:
            child_state = json.loads(child['messages'])
        except (TypeError, ValueError):
            continue
        if isinstance(child_state, dict) and eligible(connection, child, child_state, seen):
            return True
    return False
