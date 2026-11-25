# Chief of Staff v1.3.0

This release brings the task inbox into Teams and improves visibility and control in both Teams and the web app.

## Highlights

- **Review and respond in Teams.** In the configured owner self-chat, use `!cos attention` and `!cos reviews` to see tasks needing your input, approvals, saved results, commitments, and monitoring issues. Open an item to see its saved details and available commands.
- **Check task progress in Teams.** `!cos status` summarizes top-level work. Search by task name or ID, or request one exact task with `!cos status <task name or ID>`. Lists show a readable task name and context, current status, and next steps in a table.
- **Clearer Teams messages.** Attention, review, and status lists use a consistent table layout. Task names keep their meaningful wording and use an ellipsis when shortened. Replies use readable formatting and paginate long content while keeping complete rows together.
- **More model control.** Configure an optional decision model separately from task execution and select the available gateway models in Connections.
- **A more personal workspace.** Choose System, Light, or Dark appearance; the browser remembers your choice. You can also keep job details closed after delegating a request.
- **Preserved message formatting.** Incoming message HTML is handled more accurately when COS creates communication tasks.

Teams commands are prompted and limited to the configured owner self-chat. They do not send proactive inbox messages. Approval and external-action safeguards remain in place.

## Upgrade

Existing users: in COS, select **Check for updates**, then **Update and restart**. Refresh the browser after COS restarts. Configuration, credentials, and saved data are preserved.

New installations: download `chief-of-staff-v1.3.0-windows-source.zip` and `SHA256SUMS.txt`. Verify the ZIP hash before extracting, extract outside synchronized folders, then run `Setup-ChiefOfStaff.cmd`. Windows 10 or 11, Python 3.11, AMD gateway access, and access to this private repository are required.

For agent-assisted setup, use `SKILL.md` or `cos-setup-v1.3.0.zip`. Credentials remain in the local user profile.

## Validation

Windows CI passed 1,436 Python tests, with 6 skipped and 19 subtests; all 90 frontend tests passed. JavaScript syntax and repository privacy checks passed.
