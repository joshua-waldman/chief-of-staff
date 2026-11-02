# Chief of Staff

A local Windows application that coordinates research, engineering, communications, and planning through a persistent agent workflow. Submit requests in your browser, follow delegated work, review drafts and proposed actions, and keep reusable guidance in a local knowledge base.

This repository is private and intended for the owner's use while development continues. It does not include account credentials, personal settings, messages, task history, or generated artifacts. Model calls currently require access to the AMD Unified gateway; a ChatGPT subscription does not provide that access.

## Download and install

Requirements:

- Windows 10 or 11 with Python 3.11 on PATH. Python 3.11 is the tested interpreter.
- Git to clone and update the app, and access to this private repository. You can also download its ZIP from GitHub and extract it.
- An AMD gateway API key and permission to use the configured models.
- Microsoft Edge for optional browser tasks.
- Node.js 22 for development checks only, and Docker Desktop for optional isolated code execution.

Clone into a local folder. Keep the SQLite database on one machine; do not run two installations against a shared or synchronized data directory.

```powershell
git clone https://github.com/joshua-waldman/chief-of-staff.git chief-of-staff
Set-Location chief-of-staff
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
python scripts/install_hooks.py
```

The repository is [joshua-waldman/chief-of-staff](https://github.com/joshua-waldman/chief-of-staff). You need access to this private repository before cloning. ZIP downloads do not have Git history or hooks; use a clone for development.

## Configure your installation

Copy the sanitized example before the first launch:

```powershell
Copy-Item config.example.json config.local.json
```

Edit `config.local.json` with your own email and local project paths. Remove the example project if you do not need engineering work. Project names become routing choices in the app. For example:

```json
{
  "self_email": "owner@example.com",
  "projects": {
    "Example Project": "C:/Projects/ExampleProject"
  },
  "monitor_enabled": false
}
```

This file is excluded from Git. Never place tokens in it. Other defaults, including model names, skill discovery paths, polling, and browser research hosts, are defined in `cos/config.py`. After launch, use **Connections** to adjust model assignments and existing project paths. Restart the app after editing the file directly. Choose model IDs your gateway account actually supports.

Data defaults to `data/` beside the source. To use another local folder, set `COS_DATA_DIR` in the server's environment before launching. Keep that folder private and outside any source repository. Generated local skills currently remain under the application's `data/skills/` even when the runtime data directory is overridden.

## Start and deploy locally

Double-click **Start-ChiefOfStaff.cmd**, or run:

```powershell
.\Start-ChiefOfStaff.ps1
```

The launcher creates `.venv` and installs dependencies if that environment does not exist. It installs a per-user Windows scheduled task and starts the app hidden in the background. Then open [http://127.0.0.1:8765](http://127.0.0.1:8765). Closing Codex, the browser, or the launcher window does not stop the app. The task starts at Windows sign-in, has no runtime limit, continues on battery, and restarts the server five seconds after an exit. Windows retries the supervisor after a failure. No administrator account or stored password is required. If PowerShell blocks the script, use your organization's approved script execution policy or start the installed interpreter directly:

```powershell
.\.venv\Scripts\python.exe run.py
```

To choose another port, use `run.py --port 8766` or `Start-ChiefOfStaff.ps1 -Port 8766`, then open that port in your browser.

Deployment means running this app on your own Windows machine. The server binds to `127.0.0.1`; it is a single-user local application. The scheduled task runs under your signed-in Windows account so it can access your credentials and integrations. It runs while you are signed in, including when the desktop is locked; sleep suspends processing and sign-out stops it. It resumes after wake or starts again at the next sign-in. Only run one server per data directory.

Use `Start-ChiefOfStaff.ps1 -Stop` before updates or to stop the background task until the next sign-in. Run the launcher again to start it immediately. For development, stop the task first and use `Start-ChiefOfStaff.ps1 -Foreground`; Ctrl+C stops that foreground instance. Server logs are saved in `data/launcher/`, retaining the current and previous run. To remove automatic startup permanently, stop the app and delete your `ChiefOfStaff-<Windows user SID>` task in Task Scheduler. Re-running the normal launcher registers it again. A changed port takes effect after stopping the existing task and launching again with `-Port`.

## Connect services

1. Open **Connections**, save your AMD gateway key, and test the connection. The key is stored in Windows Credential Manager as `AMD-ChiefOfStaff / gateway`. Alternatively set `LLM_GATEWAY_KEY` in the server environment. The app does not load `.env` files. Inference uses `https://llm-api.amd.com/Unified/v1`; no public-provider fallback is configured.
2. For Outlook and Teams, install the compatible `slai-email` and `slai-teams` skills in your own `.codex/skills` or `.claude/skills` folder, or configure `skill_roots` in your local settings. Their scripts and tenant authorization are separate prerequisites and are not bundled in this repository. Use **Start Microsoft sign-in** and follow the device-code instructions. Credentials remain in the integration's user-profile store.
3. For Jira and Confluence, install the compatible `slai-atlassian` skill, then choose **Jira & Confluence → Open local credential setup**. Enter your token only in the local PowerShell window and test the connection. Configuration stays in your user profile outside the app.
4. Optionally review discovered Claude Desktop/plugin MCP servers in **Connections**. Enable only the definitions you intend to run. Inventory alone does not start a server; tool calls require approval. MCP credentials and executables must already work on this machine.

Discovery does not transfer credentials from a Codex or Claude conversation. Missing skills or tenant permissions limit the corresponding features. The basic UI and configuration work before these connections are complete, but real model-backed requests require gateway access.

For Docker tasks, install and start Docker Desktop and prepare the supported images:

```powershell
docker pull python:3.11-slim
docker pull node:22-slim
```

Docker tasks run without network access, with resource limits and only their job workspace mounted. Browser tasks use Edge and a separate local profile; allow exact research hostnames in **Connections**, and sign in separately when required.

Context recovery automatically releases the last visual input when required text leaves insufficient room, retaining the image reference and all owner directions. Legacy source updates with a leading newline remain source evidence during compaction. On startup, internal context-size stops receive one automatic retry with saved findings and receipts; pending approvals and uncertain actions remain waiting. An exhausted triage recovery with uninspected Teams images receives one focused attempt to retrieve those images and finish from saved findings. Restart the server after updating to recover existing jobs.

Public research follows a bounded number of redirects, validating each destination against the exact host allowlist and public HTTPS requirements. Microsoft Learn Graph documentation may use the `view` and `tabs` display selectors; arbitrary query strings remain blocked. Successful source reads are saved as immutable evidence files and carried through context recovery. A successful retry or browser fallback clears its matching current error, while unrelated failures and the historical activity log remain available. Resuming a job replaces its stale status and question; completion shows the final result.

Agent context is scoped to the current task. Delegation starts fresh child contexts with a focused request and only explicitly selected `file_ids` (up to 16 per child); no files are copied by default. Parent history and recovery archives are excluded. Children select output `file_ids` when calling `finish`, so only declared deliverables return to the parent. Technology research delegation proceeds without approval for external reads and COS-local findings and deliverables. Research restrictions persist through delegated work; external changes, including routine Jira writes from research jobs, require action approval. Other Communications delegation and subsequent sends retain their separate exact-action approvals. On restart, pending research-only delegation approvals and Technology jobs stopped solely to confirm the old delegation approval rule are queued for reassessment; substantive questions and uncertain actions remain waiting.

Artifact links in delegated requests must have their corresponding job-local file IDs selected in `file_ids`. Missing selections are rejected before any child starts, with a local ID hint when available. Selected links are rewritten to the child's copies. `read_file` accepts local paths, attached IDs and `/api/files/ID` links; older links resolve only when a matching artifact is already registered in the current job. Links alone never grant access to another job's files.

When research needs email or Teams evidence, delegate to Communications with `source_only:true`. These source readers can run one level beyond the normal delegation depth limit and return evidence and selected files to the requesting job. The server prevents them from delegating again or changing external state. Organization routing limits alone should not require the owner to upload mail or recreate the request. Restart the server to load this behavior, then resume an existing blocked job with direction to retrieve its sources through a Communications source reader.

For GitHub evidence, sign in each required account using `gh auth login`, then configure **Connections → GitHub repository accounts** with repository owners mapped to login names, for example `{"work-organization":"work-login"}`. Tokens stay in GitHub CLI's credential store; COS does not switch the active GitHub account or fall back to the account hosting its own repository. Technology and Engineering can use `repository_search` to discover repositories by name and `repository_read` to retrieve a README or a named text file as commit-pinned, job-local evidence. These authenticated GET tools are independent of the public research host allowlist. Organization SSO and repository permissions still apply.

When a repository location is missing, agents check meeting knowledge and original meeting chat messages before asking the owner. Communications source readers preserve repository links and complete branch names, including slashes, in their results. Repository source reading may also be delegated with `source_only:true` to Technology or Engineering at the normal depth limit. Changing account routes or installing this update does not resume existing blocked source-access jobs; they remain available for manual continuation.

Delegated inputs and returned deliverables use compact storage paths while retaining their original download names and file provenance. If a result handoff fails, only that parent request moves to **Needs attention**; unrelated queued work continues. Child files and the pending handoff remain saved. After resolving the file problem, send direction to the blocked parent to retry the handoff without rerunning its children.

Context checkpoints carry a bounded recent action index, current findings and source read ranges instead of embedding every historical tool result. Full receipts remain available through the job-scoped `read_action_receipt` tool. Text reads default to 4,000 characters and are capped at 8,000 per page. After twelve fresh source pages the agent must save findings with `update_brief` before reading more. Repeated covered ranges and recovery-archive reads do not count as new evidence, even when filenames or requested page sizes change. Existing blocked jobs remain stopped; resuming restores read coverage from saved receipts without replaying tools. Existing archives are retained for diagnosis and are not automatically deleted.

For staged implementation, `edit_file` replaces one exact fragment in a job workspace file using its current SHA256. It preserves a backup and rejects stale hashes or ambiguous matches. This lets agents make small changes without rewriting large files in a model response. Live project edits, application changes and host validation retain their existing approval requirements.

Repeated unchanged tool results trigger automatic recovery from retained findings and source coverage. Recovery allows two verification reads and one brief update, then offers a bounded focused delivery phase instead of immediately asking the owner to continue. Available actions are constrained in both the tool schema and execution checks. Truncated model responses are discarded without execution or replay; COS increases the output budget up to 16,384 tokens and requests smaller complete actions. On restart, jobs stopped by the previous recovery version are queued automatically, preserving their approvals and receipts. Pending approvals, uncertain actions and unrelated setup questions remain waiting. Recovery never fabricates a deliverable or bypasses action approvals; persistent failures after both phases retain a diagnostic stop.

## Use the tool

Communication triage evaluates your personal obligations before proposing replies or work. FYIs, updates, optional suggestions and tasks assigned to others finish with a useful summary and no follow-up. Completed informational summaries offer optional source-specific mark-as-read controls through exact-action approval (Teams marks the entire chat). Approving or rejecting that read action leaves the summary completed without another planning pass.

Teams activity is grouped by topic within each chat, including one-on-one chats. Related follow-ups and owner replies return to the topic's saved context; unrelated subjects get separate jobs, summaries, guidance and approval updates. Topic routing uses the triage model with bounded previews and recent topic candidates. Ambiguous subjects get a separate topic; failed routing retains the checkpoint for retry. Original text is retained in full. Topic source reads return only assigned messages, while replies and optional mark-as-read actions use the real chat ID. Marking read still affects the entire Teams chat.

After restart, existing Teams history is also split into topics using saved messages and relevant owner feedback, proposals, action receipts and delegated outcomes. Original jobs, summaries and files remain in history. Old pending approvals are superseded, and replaced active jobs are retired after their chat's import commits. Each chat migrates atomically and retries after failure; chats with executing or uncertain external actions wait for outcome verification. Imported topics initially summarize and reassess history with tools restricted to reads and brief updates, so old requests cannot replay actions. Fresh source activity or owner direction resumes normal handling with new action approvals. Migration uses the configured triage model and runs while connected, even when ordinary source polling is disabled.

Historical owner-action decisions must cite an exact excerpt from an original message or owner feedback in the same topic through `owner_obligation_source_id` and `owner_obligation_evidence`. Generic result-review guidance and unverified tests alone do not establish an obligation. Optional review remains available through completed results; missing evidence must be described explicitly. On startup, unsupported imported follow-ups are queued for reassessment unless an approval or external action needs resolution. A recovered brief-validation error preserves the task summary and next step.

1. Enter a request in **Overview**, optionally attach files, and select a registered project or let the app route it.
2. Follow progress in **Job tracker**. The initial organizations are Communications, Technology, Engineering, Strategy, and Self-Improvement. Directors can delegate work and resume with child results.
3. Answer questions and inspect proposed actions in **Needs attention**. Review recipients, bodies, arguments, and diffs before approving. Rejection or new direction lets the agent revise its work.
4. Open **Ready for review** for completed strategy, research and content-generation tasks and their declared deliverables. Routine email summaries, FYI/ignore decisions, deletions, mark-read operations and other action receipts stay in job history. The sidebar count, Overview notice and in-app completion notification surface returned work. Opening a result leaves it queued until you click **Mark reviewed**. Send direction to COS to request revisions; revised work returns as a fresh review item. Download artifacts from job details. Use **Download diagnostics & provenance** for a redacted troubleshooting bundle; inspect any export before sharing it.
5. Use **Knowledge base** to manage preferences and reusable context. Forgotten entries stop appearing in future retrieval; original jobs and backups remain.

The review inbox persists across restarts and includes earlier completed work tasks with saved finish metadata. On restart, earlier routine triage and action-only entries are removed from the review queue without changing job history or recording an owner review. Delegated results return through their parent request; automatic KB ignore closures stay in history. Review acknowledgement records that you reviewed the result; external actions retain their separate approvals. Restart the server and refresh the browser after updating.

Parent review items preserve the full text and declared files of completed delegated work, including nested drafts, even when the director finishes with a triage summary. Source-only readers and action receipts do not create review items. Startup repairs pending reviews that omitted delegated drafts, with a fresh review revision; already reviewed results stay acknowledged. Producing a draft does not record owner acceptance or complete its underlying commitment. Review cards name the saved document files or the heading of generated inline content instead of the conversation job. Job details list each document or inline draft to read, with thread-handling context collapsed separately. Titles come from the saved review snapshot so newer triage does not rename older work awaiting review.

Delegated research stays available to the parent as internal inputs. For newly delegated substantive work, the director must synthesize the requested final deliverable and finish with `final_output:true`; intermediate notes do not enter the owner review queue. A final handoff selects only authored outputs or the complete inline report, keeps citations as sources, and records which child versions were incorporated so later triage cannot resurface old notes. Topic migration collects returned work before archiving its original conversation. Startup also recovers missing reviews from migration archives without restarting those historical jobs, and keeps the review on the original request to avoid duplicating it across topics. User cancellations and reviewed results stay closed; new child results get a fresh review revision. Legacy triage histories without saved finish metadata do not qualify as generated work merely because they ran in a research organization.

Try: “Prepare a short project planning brief with open questions and next steps.” For engineering, register a project first and request a proposed patch with test evidence.

Email and Teams job details support a conversation with the Chief of Staff. **Send direction to COS** can revise a draft or change a handling decision. Sending direction does not send a message. Revised actions require fresh approval. **Remember guidance for similar emails** saves explicit reusable guidance when checked.

Communications details show **Original messages**, collapsed by default, above the summary, with full text, sender and date for the latest five messages or emails, newest first. Collapse or expand the conversation using its heading; collapse buttons are also available at the top and bottom of the expanded thread. Older email jobs load their source text when opened; use **Load original messages** to retry a failed read or load the latest five Teams messages. Collected messages and full source reads are retained separately from summaries. A preview stays labeled as a preview until the full email is available.

External sends, Confluence writes, application patch application, host commands, browser interactions, imported MCP calls, merges, deployments, publishing, and deletion require review. Routine Jira operations and job-local artifact work follow the server's policy. See `cos/policy.py` for action classification. Approved host commands and MCP processes run with your Windows account's permissions.

Self-Improvement jobs autonomously create application feature branches, run Python pytest/unittest discovery checks, and apply staged application source patches with target hashes and backups. Local tests use the server's running Python interpreter and may run from the application source directory or the job workspace. They execute trusted development code with host permissions; this is not a sandbox. Arbitrary host commands and external actions retain their approval gates. Definite validation and process-launch failures return to the agent for correction without owner verification. On startup, the app also resumes legacy commands blocked by the missing-executable or rejected-working-directory errors and retires pending Self-Improvement test approvals. Timeouts and interrupted mutations with unknown outcomes remain subject to verification. Restart the server after updating to load the policy and recover those jobs.

## Monitoring and remote requests

### Scheduled tasks

Open **Scheduled tasks** to create a named request, optionally select a registered
project, and choose **Once**, **Daily**, or **Weekly**. Set the start date/time and
an IANA timezone such as `America/Chicago`. Weekly tasks run on the start date's
weekday. Daily and weekly tasks retain their local clock time across daylight
saving changes. A repeated clock time runs once at its first occurrence; a time
inside a spring-forward gap runs at the first valid minute after the gap.

Schedules are stored in SQLite and survive restarts. COS must be running and
connected to enqueue and execute them; it cannot wake the computer or start the
server. After downtime or a disconnect, overdue occurrences produce one catch-up
run, then the next future occurrence. A recurring task waits while its previous
job is queued, running, awaiting review/input, or blocked, so unattended approvals
cannot create an overlapping backlog. Ordinary completed results may remain in
Ready for review without blocking the next run.

Edit, pause, resume, or delete schedules from the same view. Changes apply to
future runs; existing jobs and the saved run receipts remain. Recent runs link to
their jobs and show their status. Removed project registrations stop new runs
until you edit the schedule to select a valid project. Each run is an ordinary COS
request with the existing queue limits, context, result review, and action approval
rules. Creating a schedule does not approve its future external actions. Install
updated requirements (including `tzdata` on Windows), restart the server, and
refresh the browser after updating.

With the default four parallel agents, at least one slot is reserved for Communications. Research, engineering, planning and other work can use up to three slots; Communications also uses any of those slots left idle, so all four agents can process email and Teams when no other work is ready. As borrowed slots become free, ready work from other organizations takes priority over additional communications jobs. The reserved slot stays available when the communications queue is empty. Waiting for approval or settling conversation activity releases the worker so another eligible job can run. With a single configured agent, both queues share that worker in queue order. Model capacity limits still apply across agents.

Conversation replies settle for 15 seconds before reassessment, with a 60-second maximum coalescing wait for continuously busy chats. Prior findings and source details survive updates; obsolete drafts and approvals are retired immediately. `max_steps` controls automatic worker handoffs rather than stopping a long-lived job for owner input. Progress is saved and the job returns behind other ready work. Repeated unchanged evidence or errors trigger a warning and then a separate progress safeguard (`max_stalled_steps`, default 12); new source activity or owner direction resets that safeguard. Existing jobs stopped solely by the former step-budget question resume automatically on startup, while real questions and unresolved actions remain waiting.

Use **Disconnect** in the header to pause between agent steps. In-flight model calls and their tool actions finish, then each agent saves its progress before starting another step. The status changes from **Finishing current agent steps** to **Disconnected · Queue paused** when they settle. **Connect** continues from saved results without replaying completed actions. Queued requests, delegated work, and automatic continuations wait until you click **Connect**. The pause persists across restarts. Monitoring continues collecting requests according to its own settings. You can also set `queue_paused` in local settings.

After Microsoft sign-in, enable monitoring in **Connections**. Initial collection starts with current messages rather than importing your entire mailbox. Messages are deduplicated into persistent email conversations and Teams chat jobs. New activity updates the existing job; changed drafts need new approval. Collection failures and access limitations appear in the app, and unsuccessful sources retry from their saved checkpoints.

Send a command in your own Teams saved-messages chat, for example:

```text
!cos Prepare a short project planning brief
```

Only the authenticated owner's messages in the configured self-chat create top-level requests. Approvals remain in the local web app. Meeting processing uses the installed Teams skill and its existing Microsoft sign-in to retrieve accessible transcript files and save source-linked observations; it does not transcribe recording audio or infer content from filenames.

### Meeting transcript access

COS reuses Claude's existing `meeting-recap/scripts/fetch_transcript_graph.py` helper, the installed Teams skill and the shared Microsoft credentials. It reads the transcript attached to a recording through SharePoint Stream, then tries separately shared transcript files. No separate app registration, transcript sign-in or SharePoint MCP server is required.

The helper is resolved from the installed `meeting-recap` skill or the existing local Claude SLAI marketplace checkout. If unavailable, install that existing skill; shared-file retrieval remains available. COS does not copy credentials, use the reference kit's hardcoded session paths, or depend on Copilot recap availability.

`meeting_transcripts {chat_id,date?:YYYY-MM-DD}` reads recording cards in the latest 200 messages. It defaults to the newest recording date and can select a specific occurrence by date. Results are registered as job files for the existing evidence-checked summary workflow. Unknown speakers cannot establish an action assignee. Access failures and absent transcripts remain explicit; an MP4 or recap link is not itself transcript text.

## Updates, backups, and moving machines

Stop the server before updates or migration. Keep a private backup of `config.local.json`, the full runtime data directory, and `data/skills/` if used. These contain personal information and must never be added to Git.

```powershell
.\.venv\Scripts\python.exe manage.py backup
git pull --ff-only
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

`manage.py backup` makes a consistent SQLite backup under the data directory's `backups/` folder. It does not copy attachments or settings; copy the complete data directory separately with the server stopped. Back up before pulling so it matches the version being replaced.

On another machine, install the source and dependencies, privately copy your configuration and data, update project paths, and re-enter credentials/sign in under that machine's account. For file references after moving the application root, run with the server stopped:

```powershell
.\.venv\Scripts\python.exe manage.py relocate --old-root "C:/Old/ChiefOfStaff" --new-root "C:/Apps/ChiefOfStaff"
```

This helper updates recorded file references and job-message paths rooted at the old application location. Custom data directory moves and other absolute references need separate verification. Test existing attachments and integrations before enabling monitoring.

## Development and privacy checks

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node --check web/app.js
node --test tests/test_refresh.cjs
python scripts/check_repository.py
```

`manage.py doctor` reports local gateway/integration availability and project paths. Run it privately; its output is machine specific. Automated tests use synthetic fixtures and connector doubles. Live acceptance definitions in `acceptance.json` are optional: register the sample project or adapt the requests, then run them from **Acceptance tests**. Live tests may use model quota, read connected services, and create artifacts; review proposed external actions.

Git uses a default-deny allowlist. Only reviewed source, synthetic tests, the sanitized example, documentation, hooks, and CI definitions are eligible. Local configuration, databases, attachments, logs, screenshots, exports, backups, credential files, and environments are excluded. Existing private development evidence in `docs/` stays local.

Run `python scripts/install_hooks.py` after each clone. Commit hooks check staged Git blobs; push hooks check every reachable commit, including deleted files, for disallowed files and common credential or personal-data patterns. CI repeats the source check and tests on pushes and pull requests. Before committing, review exactly what is staged:

```powershell
git add README.md cos tests web
python scripts/check_repository.py --staged
git diff --cached --stat
git diff --cached
```

Add new source paths deliberately to both `.gitignore` and the checker's allowlist. Never force-add private files or bypass the hooks. Pattern checks cannot recognize every kind of personal information; human diff review remains necessary. Git commit author metadata is also retained in history; use an appropriate repository identity if personal metadata should be excluded.

## KB-driven email intake

Before scheduling agents, intake batches up to 12 new email conversations for one tool-free call to the configured triage model. It checks all active owner-authored or owner-reviewed KB preferences, preserving their scope, conditions and exceptions. Senders, projects and rule wording are not hardcoded. KB edits and forgotten rules apply on the next intake pass without a restart. Suggested knowledge and task outcomes cannot justify dismissal.

The scheduler also checks untouched queued email jobs, including the backlog from earlier versions, before launching an agent. It evaluates all recoverable source envelopes together and revalidates KB and job state before recording a no-action closure. Ambiguous legacy envelopes, existing agent work, owner feedback, approvals, attachments already staged as job files, and delegated tasks continue through their existing workflow. Completed history is retained. Unchanged queue checks are cached in memory; KB or source changes invalidate that cache.

Clear ignore matches close with explicit no-response/no-action decisions and an audit reason containing the supporting KB IDs and revision. Ambiguous or conflicting guidance, insufficient preview evidence, invalid model output and classifier failures use normal triage. The classifier uses one request attempt with an eight-second network timeout and cannot send or delete mail. The ingestion transaction rechecks the KB revision to reject stale decisions. Already-seen mail is not classified again; owner feedback and existing work take precedence. Polling still controls detection time; the shortcut avoids the multi-step agent queue while retaining one lightweight classification pass.

## Troubleshooting

- **Job files and recovery archives:** job details show outputs under **Deliverables** and keep files from the job workspace's reserved `context/` directory in a collapsed **Internal recovery files** section. This also applies to existing jobs. Older recovery files are compressed losslessly into `recovery-archive.zip` after a job stops running, once at least 12 loose files qualify. The two newest checkpoint and history snapshots and the current checkpoint stay loose. Original file IDs, paths, checksums, text pagination, and image reads continue to work through the archive. Only registered generated recovery files are eligible; deliverables remain untouched. The bundle is verified and its index committed before loose copies are removed. Active and queued jobs are skipped, and maintenance failures do not fail or replay job actions. Preview cleanup for existing jobs with `python manage.py prune-recovery`; use `--job-id JOB_ID` to limit it and `--apply` to perform it. This reduces loose file count and disk usage without discarding historical evidence. Back up the complete `data/jobs/` directory alongside the database, including ZIP bundles.
- **Job exceeds the context window:** the runner archives oversized tool results and older tool turns under the job workspace's `context/` folder, checkpoints the current brief, action receipts, child results and artifact paths, and retries inference with less history. Full archived evidence remains available through `read_file`; completed tools are not replayed by recovery. Historical Teams imports move their retained source records into a lossless primary-evidence file and read it in bounded pages, keeping task directions and later owner corrections in context. On restart, imports blocked by the former embedded-evidence overflow are automatically queued once, unless an action is pending or its outcome is uncertain. Recovery makes at most two reduced-context retries, then blocks visibly if mandatory policy and directions still cannot fit. Resume an existing failed job with direction after restarting on this version; progress and action reviews are retained. Keep briefs current and save findings while reading large sources in batches.
- **Gateway context limit differs by model:** set `context_window_tokens` (default conservative fallback: 128000) or `model_context_windows` (a map from exact model ID to token limit) in `config.local.json` using limits verified with the gateway administrator. Input budgeting reserves 25% plus 8192 output tokens and estimates text conservatively by UTF-8 bytes; it is not an exact tokenizer. The runner uses local checkpoints and does not assume AMD supports the native OpenAI compaction endpoint. Model reasoning history is released during compaction. Restart after editing configuration.
- **Teams image-only messages:** Triage downloads only images belonging to the current topic and views them before finishing or requesting owner attention; inaccessible images remain explicit evidence gaps. Truly empty new messages do not start jobs. Blank historical records can be re-read to recover image references, and a blank record alone does not create an owner task. Historical imports permit these source image reads while retaining their restrictions on sends and other actions. Original HTML and image references are retained.
- **Jobs with many screenshots:** older images, including legacy tool images stored as user messages, are saved as reloadable image files during compaction. Normal compaction keeps the newest image visible; forced overflow recovery may release it as well. Text directions in mixed image/text messages are preserved exactly. Archived images remain untrusted evidence and can be inspected again with `read_file`.
- **Gateway request fails:** test the key in Connections and check model permissions. Gateway access is separate from a ChatGPT subscription.
- **Microsoft or Atlassian connection unavailable:** verify the required skills are installed and authenticate on this machine. Tenant restrictions may prevent access to some chats or files.
- **Port already in use:** stop the existing instance, or use another port with a separate data directory. Do not run concurrent servers against the same database.
- **UI says the server restarted:** refresh the browser to renew the local session.
- **Dependency import fails:** rerun the requirements installation using `.venv\Scripts\python.exe`.
- **Git rejects a commit:** review the reported paths and remove local data or replace personal values with synthetic examples. The checker does not print matched secrets.

## Configurable communication and engineering workflows

Email attachment downloads use source attachment IDs as directories, retaining the
original filename and metadata. Same-named attachments remain separate when passed
to delegated jobs. Reference/item attachments are reported as coverage gaps rather
than claimed as downloaded.

Reply approvals show actual recipients and the complete prepared plain-text body,
including accessible conversation history and sender/date/recipient/subject headers.
The server retrieves conversation messages, including earlier messages omitted from
the latest quoted body, before preparing approval. Editing a reply cannot remove its
reviewed history; use the review conversation to request a new preview. HTML styling
and embedded images are not reproduced in the outgoing plain-text quote. Existing
pending reply approvals without prepared history need a fresh proposal after upgrade.

Communications can read calendar events and propose accept, tentative, or decline
responses with an explicit organizer-notification choice. Mail deletion (to Deleted
Items), mail marking as read, and Teams chat marking as read also require exact-action
approval and record execution receipts. These use installed Microsoft skill
authentication; tenant permissions may restrict calendar or Teams write operations.
Rejected operations surface for owner attention; uncertain outcomes are never retried
automatically. No KB rule itself authorizes these external actions.

Incoming-conversation jobs with `action_needed` set to `yes` or `unknown` remain in
Needs attention when the agent tries to finish. Resolve the work or explicitly dismiss
it through owner direction before closure. Previously completed history is retained.

Knowledge entries can bind to an exact email or Teams conversation ID using the
Knowledge base form. Job review also offers **Only this conversation**, which takes
the ID from the job's recorded source. Bound guidance is excluded from other jobs
and intake decisions. Existing prose-only entries retain their existing scope; edit
them to add a binding when appropriate. Agent suggestions still require review.

In **Connections → Engineering workflow**, configure optional rules keyed by any
registered project name, or `*` for shared defaults. No project, sender, parent key,
or organization-specific keyword is supplied by this feature. For example:

```json
{
  "*": {
    "branch_prefix": "feature/",
    "require_ticket": true,
    "require_feature_branch": true
  },
  "Example Project": {
    "jira_parent": "EXAMPLE-100",
    "jira_issue_type": "Task"
  }
}
```

`create_feature_branch` proposes `prefix + ticket + descriptive name` in the job's
registered repository. It never resets or overwrites an existing branch. Configured
branch requirements are checked for each target project before patch approval and
again before application. Jira creation accepts an explicit parent or uses the
configured default, checking live create metadata, hierarchy levels and same-project
subtask restrictions. Unverifiable hierarchy blocks creation rather than guessing.
An empty workflow configuration imposes no new project requirements.

Restart the server after updating source to load these capabilities and the additive
conversation-binding table. Refresh the browser afterward. Installing this version
does not accept KB suggestions, change personal workflow settings, or execute actions.

## Meeting commitment register

The **Commitments** view keeps source-linked meeting actions independently of job
completion. Agents use `search_commitments` before `record_commitment`, or attach
corroborating/completion evidence to an existing record with `add_commitment_evidence`.
Each proposal must cite an exact excerpt and timestamp from a transcript registered
to the source job. The transcript hash is checked before evidence is accepted; older
cached transcripts without integrity metadata must be collected again. Owner, deliverable, scope, meeting date, deadline wording, normalized
date and certainty are stored separately. Inferred deadlines require an explanation;
unknown deadlines cannot acquire a calendar date. Unknown speakers and indirect
suggestions must not be treated as assignments.

New records are **proposed**, requiring owner acceptance before becoming **open**.
You can correct fields, confirm completion using cited evidence or a personal
verification note, dismiss with a reason, and reopen with a reason. Source evidence
can request completion review but cannot close or reopen an item automatically.
Active proposals and accepted commitments contribute to Needs attention, even if
their original meeting job finishes. Search and status filters retain closed history.

Exact source/deliverable repeats are idempotent. Related records for the same owner
and scope appear as possible duplicates. Compare both records before merging;
merging retains the target's fields, combines source evidence, preserves the original
record as a redirect and writes an audit entry. Neither repeated extraction nor new
evidence resurrects a dismissed or completed item. Concurrent review uses versions
and rejects stale changes instead of overwriting them.

The additive database tables are created on server restart. Existing summaries and
planning files are retained; no bulk conversion or historical assignment is inferred
on startup. To populate older meetings, ask COS to reread their registered transcripts
and propose commitments for review. The register is local memory: accepting a record
does not send messages, schedule meetings, create tickets or approve delegated work.

## Source map

- `cos/server.py`: local API, sessions, attachments, and connections.
- `cos/engine.py`: durable orchestration, delegation, and approvals.
- `cos/provider.py`: gateway requests and model routing.
- `cos/policy.py` and `cos/tools.py`: action policy and execution.
- `cos/integrations.py`: installed skills and MCP definitions.
- `cos/monitor.py` and `cos/monitor_bridge.py`: collection and remote requests.
- `cos/knowledge.py`, `cos/meetings.py`, and `cos/reviews.py`: reusable context and review.
- `web/`: browser interface. `tests/`: automated regressions.

## Quick message decisions

Email and Teams conversations begin with a compact triage pass before research, drafting or delegation. Full email bodies are read when only previews are available. The first brief includes a useful summary and one decision: **Summarized for My Reading**, **Response Needed**, **Action Needed**, **Response and Action Needed**, **Can Be Ignored and Marked Read**, or **Needs Review**. Reply and work requirements remain separate; uncertain evidence stays visible for follow-up. Large or resumed conversations use the normal workflow with the same decision fields.

Decisions and summaries appear in the job queue and at the top of message details. Use **All message decisions** to filter the queue. New conversation activity resets the decision for reassessment. Marking read uses the existing exact-action approval flow; the ignore category describes a recommendation, not a completed read operation. Existing KB-based ignore shortcuts continue to operate before agent admission.

### Model capacity recovery

Model rate limits save the job as **Waiting for model capacity** and release its worker. Jobs sharing a model share a durable cooldown across restarts. The gateway honors `retry-after-ms` and `Retry-After` (seconds or HTTP date); otherwise delays increase from about 5 to 60 seconds with jitter. Capacity waits preserve the reserved Communications slot and the shared worker budget for other organizations. In **Connections → Models & orchestration**, **Concurrent requests per model** (`model_parallel_requests`, default 1, range 1–12) limits simultaneous calls to each model across all agents. This is separate from **Parallel agents**: four agents sharing one model can make only one model call at a time when this setting is 1. Set both to 4 to allow four agents to call the same model concurrently, subject to gateway limits. Save configuration to apply the setting; no server restart is needed. Responses calls honor the requested output budget.

After `capacity_recovery_seconds` (default 900 seconds), automatic recovery pauses with one shared attention notice. Open an affected job for **Retry now**, gateway diagnostics, or cancellation. Retry now resets the shared model cooldown, so other jobs using that model can also resume. Completed tool results and approvals are retained; retries repeat inference only. Jobs that failed with the old gateway 429 error resume on startup when they have no pending or uncertain action. All inference continues through the AMD gateway.

### Time-sensitive attention

Request lists and job details use a subtle amber accent and a **Time sensitive**
badge when the model assesses that prompt owner attention matters, even without
an explicit deadline. The model considers blockers, dependencies, incidents,
expiring opportunities and consequences of delay; the job's **Timing** field
explains the assessment. Urgent wording alone does not establish urgency.
Supported deadlines within 48 hours show **Due soon**; past deadlines show
**Overdue** in red. Completed and cancelled requests have no urgency accent.

Open and proposed commitments use calendar dates: today and the next two days
are highlighted, while earlier dates are overdue. Inferred commitment dates
remain amber and say **Possible timing — confirm**. The Timing column always distinguishes routine, unclear and not-yet-assessed
requests. Enable **Connections → Timing assessments** for a tool-free background LLM
pass that assesses existing attention items
without resuming their work or changing approvals. It uses saved requests,
briefs, messages and pending actions, and defers if those facts change during
assessment. Assessment pauses when COS is disconnected; gateway failures leave
items visibly unassessed for retry. Restart COS and refresh the browser after updating.

Saved results stay in Ready for review while new source activity reopens their parent conversation. Reviewing acknowledges that saved revision only and leaves the ongoing job running. Explicit revision feedback retires the old review until revised work returns; unrelated triage cannot replace an unreviewed draft.

On startup, unreviewed Communications closures that exposed research bundles instead of a final report resume for COS synthesis. Saved child findings and selected evidence are cached in the parent workspace; completed children, reviewed results, user cancellations, and pending or uncertain external actions are preserved. Repository reviewers can use `repository_tree` to discover implementation and test paths at a pinned commit before reading them with `repository_read`.

If a restricted source reader blocks or fails after collecting evidence, COS returns its retained job-local source files, draft notes, read coverage, and blocker to the director as partial inputs. The reader remains incomplete; these inputs never become owner review items automatically. Startup repairs older empty failure handoffs once, preserving the reader and its evidence. Returned work receives one completion turn before newer work in its queue, then normal fairness resumes. Tool calls use native argument objects to avoid nested JSON escaping failures; legacy saved calls remain readable.

A replacement reader verifying retained evidence has a bounded 24-model-turn continuation. If it cannot return findings within that phase, it stays incomplete and its retained inputs and gaps return to the director automatically. This bound does not complete the owner task or publish a draft; it prevents another unbounded source-verification loop from starving final synthesis.

Communications and Strategy synthesis also has a bounded verification phase after completed delegated work returns. After 24 model turns, COS allows two final verification reads and eight delivery turns. New source pages cannot reset this phase. COS must author and explicitly finish the requested output, with any remaining coverage limits, or stop visibly without publishing an incomplete review. Recent returned reports remain primary evidence through context compaction; the newest complete tool turn is preserved when it fits the actual context limit.


Button actions show progress immediately while the server records or executes the
request. Database and file operations run off the HTTP event loop so a waiting
write does not freeze unrelated requests. Routine refreshes use compact job
responses and pause while the browser tab is hidden. Job details initially show
the latest 30 activity events; **Load older activity** retrieves earlier events in
pages without discarding the stored history. Background detail refresh pauses
while the activity timeline is open so loaded history stays in place; use its
**Refresh** button for the latest events. Restart the server and refresh the browser
after installing this update. Database query indexes are added on startup.
