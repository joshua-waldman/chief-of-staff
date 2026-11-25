# Chief of Staff

A local Windows application that coordinates research, engineering, communications, and planning through a persistent agent workflow. Submit requests in your browser, follow delegated work, review drafts and proposed actions, and keep reusable guidance in a local knowledge base.

This repository is private and intended for the owner's use while development continues. It does not include account credentials, personal settings, messages, task history, or generated artifacts. Model calls currently require access to the AMD Unified gateway; a ChatGPT subscription does not provide that access.

Use the **Theme** selector in the header to choose System, Light, or Dark. System follows your operating system appearance. Your choice is saved per browser and applies across tabs and page reloads.

When delegating an initial request, uncheck **Open job details after delegating** to stay on the current page. The browser remembers your choice; you can open the request later from Recent requests or Job tracker.

## Download and install

Requirements:

- Windows 10 or 11 with Python 3.11 on PATH. Python 3.11 is the tested interpreter.
- Git to clone and update the app, and access to this private repository. You can also download its ZIP from GitHub and extract it.
- An AMD gateway API key and permission to use the configured models.
- Microsoft Edge for optional browser tasks.
- Node.js 22 for development checks only, and Docker Desktop for optional isolated code execution.

Clone into a local folder. Keep the SQLite database on one machine; do not run two installations against a shared or synchronized data directory.

```powershell
git clone --branch v1.2.1 --depth 1 https://github.com/joshua-waldman/chief-of-staff.git chief-of-staff
Set-Location chief-of-staff
python scripts/install_hooks.py
```

The stable version for new installations is [v1.2.1](https://github.com/joshua-waldman/chief-of-staff/releases/tag/v1.2.1). Download `chief-of-staff-v1.2.1-windows-source.zip` from that release if you prefer ZIP setup; it contains application source and setup scripts, and requires Python and dependency installation. Verify the download against the attached `SHA256SUMS.txt`. Use the main branch for development.

The repository is [joshua-waldman/chief-of-staff](https://github.com/joshua-waldman/chief-of-staff). You need access to this private repository before cloning or downloading release assets. A release clone starts detached at the tag. ZIP downloads do not have Git history or hooks; use a clone for development.

### Guided first-time setup

After cloning or extracting into a local folder **outside OneDrive or other
synchronized folders**, double-click
**Setup-ChiefOfStaff.cmd**, or run the PowerShell command:

```powershell
.\Setup-ChiefOfStaff.ps1
```

The wizard asks for the owner email on a new installation and whether this is a
desktop host that should support laptop access. It creates a Python 3.11 virtual
environment if needed, installs the requirements, preserves existing settings,
registers/starts the per-user task and checks the local COS page. Existing Python
3.11 and repository access are prerequisites; it does not install system software.
The manual venv/configuration steps are unnecessary when using the wizard. New
installations start with the queue paused and monitoring off. Complete **Connections**
before clicking **Connect** or enabling monitoring. Enter the gateway key only in
the local credential UI; setup does not request secrets in a terminal or chat.

Choose **Yes** for optional desktop access to create the host's **Chief of Staff**
browser icon and a private laptop connection kit under
`%LOCALAPPDATA%\AMD-ChiefOfStaff\LaptopSetup`. This reads the current host's DNS
name/suffix and configured RDP port, so each owner gets their own target. The kit
contains **COS Remote Desktop**, the standalone shortcut installer and instructions.
Copy it to the laptop through approved private storage. It does not include COS
data or credentials and does not provision a private route or enable RDP. Verify
the name/port from the laptop, then the remote login and COS page. If IT requires a
different private alias, supply `-RemoteComputer` explicitly.

For an agent or other noninteractive shell, collect the owner's choices first:

```powershell
.\Setup-ChiefOfStaff.ps1 -SelfEmail owner@example.com -DesktopAccess Enable -NonInteractive -NoBrowser
# Or use -DesktopAccess Skip when remote desktop access is not wanted.
```

Use the actual owner's email; it is not a password. Omit `-SelfEmail` when reusing
unchanged existing configuration. Optional parameters include `-Port`,
`-RemoteComputer`, `-RemotePort`, `-LaptopKitDirectory` and `-PythonExe`.
`-WhatIf` previews the setup without installing packages, writing files or
registering/starting a task. Setup refuses to replace another installation's task,
install a host in a detected OneDrive folder, change the environment of a running
instance, reuse an occupied port, overwrite
owner configuration or replace an existing laptop kit. Inspect the reported
conflict; do not launch a duplicate server. Reuse a healthy installation rather
than rerunning first-time setup. Use the organization's approved script policy if
PowerShell execution is blocked.

To add desktop access to an already running COS installation without reinstalling:

```powershell
.\scripts\Install-CosShortcuts.ps1
.\scripts\New-CosLaptopKit.ps1
```

The kit generator runs **on the COS host**. Choose a new `-Destination` when a kit
already exists. A laptop-only client should use the resulting kit or the standalone
shortcut installer described below; it should not run the host setup wizard.

### Setup with Claude or Codex

Attach [`skills/cos-setup/SKILL.md`](skills/cos-setup/SKILL.md) to a session and say:
“Use this skill to inspect this machine and set up COS. Ask for missing choices
and let me complete sign-in privately.” The file is self-contained and can also
be installed as a `cos-setup` skill using your client's normal skill mechanism.

The agent distinguishes a new host, an existing installation and a laptop client;
checks prerequisites; performs the selected setup; helps configure the gateway and
chosen integrations; and verifies the local/remote steps it can actually test.
It optionally obtains the application itself: reuse existing files, clone the
private repository, or download/extract an authenticated ZIP. The user may supply
an organization-approved mirror and revision. A skill-hub installation does not
grant access to the private repository, and laptop-only setup skips a full clone.
It asks about optional desktop access, services and custom COS organizations.
Keep the built-in teams or add your own with a name, leader title and purpose;
the agent registers selected additions through Self-Improvement once the gateway
and queue are ready, then verifies them in **Organizations**. Custom teams use
shared tools and delegation; their definitions do not grant built-in specialist
permissions. Existing teams are preserved. Private repository access, gateway
credentials, MFA/device-code sign-in and corporate connectivity provisioning may
require the owner or IT.
The skill reports these as pending instead of claiming setup is complete. It does
not require another installed skill just to begin; integration-specific helpers
are discovered only when needed.

## Configure your installation

### Optional 30-day warm start

After configuring the gateway and Microsoft sign-in, open **Connections → 30-day
warm start** and choose **Start 30-day warm start**. Connect the workspace to allow
processing. This optional import builds searchable background context from a fixed
30-day window ending when you start it. It scans newest days first, uses email
previews (including accessible sent mail) and Teams text, and does not open
attachments, images, or meeting transcripts.

The import never creates jobs, commitments, drafts, delegated work, or external
actions, including from historical `!cos` commands. Its worker has no action
tools and bypasses message intake. Active monitoring processes new activity
separately; importing history neither enables monitoring nor changes its
checkpoints or seen-message records. Imported context can help interpret a new
request, but never establishes a current obligation or an owner preference.

Progress is saved after each day/source collection and each summary. **Pause
after current batch**, disconnecting the workspace, or restarting COS preserves
saved progress. Resume continues the original window. A failed collection retains
that window for retry. Model capacity cooldowns defer summaries automatically.
The import is limited to 200 summary batches of at most 16,000 input characters
each, using the configured triage model; high-volume accounts may have partial
coverage. Collection timeouts and pagination limits stop progress for retry rather
than claim complete coverage. Teams access denials are shown as coverage gaps.

Find summaries in **Knowledge base**, labeled `warm-start`, with their dates and
**Source excerpts**. They are automatically searchable as historical observations,
not reviewed guidance. You can correct or forget them. Forget excludes a summary
from retrieval; retained source excerpts and database backups remain. Completion
is a single onboarding import, not an ongoing historical scan.

For **manual installation only**, copy the sanitized example before the first
launch. Skip this step if guided setup already created `config.local.json`; do not
overwrite an existing configuration:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
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

### Model selection and optional automatic routing

**Connections → Models & orchestration** loads the complete model catalog from
your AMD gateway account. Use the dropdowns to assign chief, director, worker and
triage models. **Refresh available models** requests a fresh catalog; discovery is
otherwise cached for five minutes. Specialized models remain visible but are
disabled for incompatible assignments. Saved model IDs remain visible if the
catalog cannot be loaded or an assignment is no longer listed. Catalog visibility
does not guarantee inference permission or tool compatibility.

Automatic selection is **off by default**. Enable **Select and route models
automatically**, choose the eligible roles, and select a decision model plus task
models for fast work, general work, reasoning, coding and vision. Blank task
assignments follow the indicated role assignment. The default decision model is
`Microsoft-Decision-1.1`; other gateway models advertising decision support, such
as `vLLM-SR-d3-27B`, can also be selected. COS calls the AMD `/systemone` endpoint
with a bounded request excerpt and task features. Routing adds a decision-model
request when a conversation needs a new selection.

The router chooses only among the configured task models, filtered by gateway
capabilities, known context/output limits and current cooldowns. A successful
choice stays through tool turns while compatible; new user directions, changing
routing settings or introducing requirements it cannot meet allow a new selection. Unavailable
discovery, failed decisions, or confidence below the configured threshold use the
assigned role model. Its normal access, capability and capacity checks still apply.
Routing does not retry an executed action or alter approval policy.

Model events and diagnostic traces include the selected model, routing reason,
profile, confidence and decision usage when applicable. Context budgeting uses
the smallest configured candidate window and retains the conservative configured
limit unless explicitly adjusted; it does not assume every model supports a large
context. Provider-specific reasoning state is replayed only to its originating
model.

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

### Stable release updates

COS checks GitHub for a stable release at startup and every 12 hours while it is
running. **Check for updates** in the sidebar also checks immediately. Development
commits on `main`, draft releases and previews do not trigger an update. Releases
must use a stable version tag such as `v1.2.3`. The notice includes release notes,
**Update and restart**, and **Remind me tomorrow**. Installation always requires
the owner's click; no model or background release check installs code.

For this private repository, sign in with GitHub CLI and map `joshua-waldman` to
the account with repository access under **Connections → GitHub accounts**.
Public repositories can be checked without sign-in. Connection failures do not
stop COS; use the manual check to see details. No published release is also shown
as unavailable rather than treated as an update.

Automatic installation requires the Windows host to be running through its normal
scheduled task. Git installations must have a clean `main`, `master`, or detached
release checkout, with the official repository as `origin`; local changes and
feature branches are refused. Updates fetch and check out the exact published
tag's commit, leaving Git installations detached at that release. ZIP installations
download the exact release commit and replace application source files. Back up
custom source edits before using a ZIP installation's updater.

COS downloads and validates the release before pausing new work. It allows active
workers, monitoring, assessments and approved actions to finish, waiting up to
30 minutes before deferring the update. It then starts a separate hidden helper
that stops the scheduled task, backs up SQLite consistently, installs the source
and Python requirements, and validates startup with background work disabled.
After validation it restarts normally and the browser reconnects. Configuration,
credentials and private data are preserved. The existing queue/monitoring choices
are retained. The helper verifies task ownership and declines foreground runs or
another installation's task.

If Windows leaves the task's child server running, the updater stops only the
already-drained server identified by its PID and Windows creation time. It applies
the same identity check to the isolated startup trial; it never kills an arbitrary
process just because it is listening on COS's port.

Startup failures before background work resumes attempt to restore the previous
code, database and requirements. Dependency restoration can require network access.
After normal work resumes, COS does not automatically restore an older database.
Recovery logs, source/database backups and `result.json` are retained in
`<COS_DATA_DIR>/updates/<attempt>/` (normally `data/updates/`). If recovery itself
fails, preserve this directory and inspect the logs before restarting. A remaining
`.cos-update-trial` file deliberately disables background work until recovery is
complete. Locked files, unavailable dependencies or Windows policies that prevent
the detached helper can prevent installation; the updater does not bypass them.

To publish: merge and validate the intended code, create a new immutable version
tag such as `v1.2.3`, and publish a GitHub Release for that tag with release notes.
Designate it the latest stable release. Keep previews marked as prereleases and
never move a published version tag. New users should install a release tag/ZIP;
users of an older version without this updater need one manual update to obtain
it. Publishing a release is separate from merging a PR.

### Desktop icons

After the normal launcher has registered COS to start at Windows sign-in, run
this once **on the COS host** from the installation/checkout directory:

```powershell
.\scripts\Install-CosShortcuts.ps1
```

This creates **Chief of Staff** on the current user's Windows desktop. Double-click
it to open COS in the default browser, including inside a remote desktop session.
The icon opens the UI; COS continues to run through its existing scheduled task.
It does not create a second server. If you deliberately stopped COS, double-click
`Start-ChiefOfStaff.cmd` in the installation folder to resume it. If you changed
the server port, create/update the icon with `-Port 8766 -Force`.

On a **Windows laptop**, create the connection icon once, using the COS host's
approved private DNS name:

```powershell
.\scripts\Install-CosShortcuts.ps1 -Mode Remote -RemoteComputer desktop.example.com
```

This creates **COS Remote Desktop**. Connect to your approved private network,
double-click that icon, sign in, then double-click **Chief of Staff** inside the
remote desktop. The connection icon uses the built-in Windows RDP client and
prompts for credentials; it stores no password. If Windows Hello produces a
certificate/UPN error, choose **More choices → Use a different account** and use
your organizational account password if that sign-in method is available.
The `/prompt` switch requests the Windows credential dialog; it does not force a
password provider or change the laptop's Windows Hello policy. The generated
connection shortcut is specific to the supplied host. Distribute the generic
installer or have each host generate its own kit for another owner.

Remote mode needs only this standalone PowerShell script; copy it to the laptop
without installing COS, Python, integrations or a second database. Keep generated
shortcuts private since a remote shortcut includes the desktop's hostname. Delete
an icon to remove it. Re-running leaves existing icons unchanged; use `-Force` to
replace them. `-DesktopDirectory <existing-folder>` changes the output location,
`-RemotePort` supports a configured RDP port, and `-WhatIf` previews file creation.
Use your organization's approved PowerShell execution policy if scripts are blocked.

Connecting through RDP normally locks the **host's physical console**; it does
not sign out your account or stop COS. Unlocking the host locally can disconnect
the remote session. This differs from the laptop locking due to its own inactivity
policy. Close/disconnect the RDP window when finished rather than signing out;
corporate session timeouts and sleep policy still govern availability. A shortcut
does not provide simultaneous local and remote control or change those policies.

## Remote access

Keep one authoritative COS host and use its existing loopback interface. Prefer
Remote Desktop over your organization's approved private connection, then open
`http://127.0.0.1:8765` in the **desktop session's** browser. A laptop VPN connection
does not by itself establish a route or authorization to that specific desktop.
Do not expose COS publicly or synchronize an active SQLite data directory.

On the desktop, collect a read-only readiness inventory:

```powershell
.\scripts\Test-CosRemoteAccess.ps1
```

The report shows the OS, RDP enablement/NLA, relevant listeners, scheduled-task
working directories and installed connectivity software. Unknown probes stay
unknown. Reports contain private machine/path information; keep them out of Git.
The script neither enables remote access nor changes firewalls, power or policy.

On the laptop, while connected through an approved private route, check only the
specific desktop and RDP port supplied by IT:

```powershell
.\scripts\Test-CosRemoteAccess.ps1 -Target desktop.example.com -TargetPort 3389
```

Success means a TCP connection completed, not that owner login, MFA or the COS
workflow has passed. If approved, connect with Remote Desktop Connection
(`mstsc.exe`), using that same desktop name. On failure, ask IT to verify private
DNS, routing, desktop ACLs/firewall and RDP entitlement; do not open public ports.
For an office desktop, check the laptop's approved VPN/private-access route first.
For a desktop behind home NAT, corporate-resource VPN access alone is insufficient.

An optional direct SSH tunnel lets you use the laptop browser. It requires an
independently approved SSH server/account and private route to the desktop, plus a
host key already verified through a trusted channel and recorded in known_hosts.
The helper requires Python 3.11 but no COS dependencies or local COS installation.
Copy just the helper to a laptop if needed. Preview first:

```powershell
python scripts/connect_cos.py --host desktop.example.com --user owner
# Add --connect to run SSH after the prerequisites have been established.
```

The helper fixes both ends to `127.0.0.1`, requires strict SSH host-key checking,
disables agent/X11 forwarding, and fails if the local port cannot bind. It loads
no SSH config files, preventing inherited extra forwards or local commands; sites
requiring an SSH config, jump host or special authentication need a separately
reviewed configuration. The helper supports direct desktop DNS/IPv4 destinations.
Keep its terminal open; Ctrl+C closes the tunnel. It installs nothing and does
not verify corporate approval. SSH startup does not prove remote COS availability.
Use `--local-port 18765` if 8765 is occupied, and browse the printed loopback URL.
Refresh after COS restarts. Local cookies are not port-isolated; avoid another COS
instance on the laptop and consider a dedicated browser profile.

Validate history, an old attachment, downloads, a harmless request, answering a
question, approval previews, reconnection and restart/session renewal. Use local
fixtures for approval tests. Disconnecting RDP should preserve the signed-in
session; verify corporate timeout, sleep and reboot/sign-in policies before
depending on unattended processing. Schedules cannot wake or sign in the host.

### Recommendation audit (2026-11-16)

The supplied remote-access recommendation correctly identifies the local token
as request/CSRF protection rather than owner authentication: the root page
provides it. `run.py` still binds to loopback, and `cos/server.py` still checks
Host/Origin and API cookie/write tokens. The interactive scheduled-task design,
SQLite WAL usage and database-only `manage.py backup` behavior also match the
reviewed source. Full recovery still needs coordinated artifacts, settings and
integration provisioning; the existing backup command alone is insufficient.

Two qualifications affect implementation. First, the document describes exact
action approvals as if every external write always needs an owner click; current
configuration supports automatic approval review. Remote access preserves the
configured approval policy and must not be represented as adding manual gates.
Second, the document's static citations are not deployment revision evidence.
The live scheduled task observed during this audit uses a separate installation
from this development checkout. New helpers here have not been deployed there.

Read-only host checks found Windows 11 Enterprise, enabled RDP with NLA, a running
interactive COS task, COS listening only on IPv4 loopback at port 8765, and RDP
listeners on 3389. Zscaler appeared in installed-software records; GlobalProtect
and an SSH server service did not appear in the probes. These findings establish
local state only. The owner confirmed an office-network desktop and a Windows 11
Enterprise laptop. Active data-root overrides, backup recovery, effective access
policy and full COS workflow tests remain unverified. The owner
subsequently confirmed a successful laptop TCP check to RDP and an authenticated
Remote Desktop session. That establishes the tested route/login, rather than
every off-network scenario or COS workflow. Desktop icons were then created;
no firewall, authentication or power policies were changed.

AMD's [IT onboarding guide, pages 8 and 17](https://amdcloud.sharepoint.com/sites/amd-info-tech/Shared%20Documents/Global%20AMD%20NEO%20IT/Global-AMD-NEO_IT.pdf?web=1)
identifies GlobalProtect for MFA-protected private access and Zscaler for internet
security. This is a documented route to investigate with IT, not proof that this
owner can reach this desktop on 3389 or install/enable SSH. Use AMD Software Center
or IT to provision the laptop's approved client if absent. The same guide directs
employees to ACE and ServiceNow for IT help. Ask: "Can my managed Windows laptop,
off-network through the approved private connection, reach my office Windows
desktop by private DNS on RDP, using my own account? Which client, entitlement and
device controls are required?" No support request was sent by this audit.

Microsoft confirms [RDP host editions and network prerequisites](https://learn.microsoft.com/en-us/windows-server/remote/remote-desktop-services/remotepc/remote-desktop-allow-access).
[Windows OpenSSH setup](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse)
can involve service and firewall changes. Tunnel options follow the primary
[SSH manual](https://man.openbsd.org/ssh.1) and
[client configuration manual](https://man.openbsd.org/ssh_config.5).
Synthetic tests cover loopback Host/Origin behavior at default and alternate
ports, session/CSRF checks and proxy-host rejection. They do not replace an actual
SSH/RDP connection or end-to-end file/approval tests. A future HTTPS private URL
needs owner authentication before every route, TLS and explicit proxy/origin
design before any network exposure.

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

Docker tasks run without network access, with resource limits and only their job workspace mounted. Browser tasks use Edge. Automatic `browser_read` research uses a fresh, unsigned-in profile; reviewed `browser_action` interactions, including reviewed navigation, use a separate persistent profile for sign-in. **Connections → Web research** defaults to **Public web**. Choose **Restricted hosts** to require exact hostnames for pages and supporting resources. Existing hostname lists are retained and become active when restricted mode is selected. Reviewed interactions retain the AMD/Microsoft SSO host exceptions, with public HTTPS checks.

Context recovery automatically releases the last visual input when required text leaves insufficient room, retaining the image reference and all owner directions. Legacy source updates with a leading newline remain source evidence during compaction. On startup, internal context-size stops receive one automatic retry with saved findings and receipts; pending approvals and uncertain actions remain waiting. An exhausted triage recovery with uninspected Teams images receives one focused attempt to retrieve those images and finish from saved findings. Restart the server after updating to recover existing jobs.

Public research follows a bounded number of redirects, validating each destination against public HTTPS requirements and, in restricted mode, the exact host allowlist. Both modes block private network targets, embedded credentials, and custom ports. Ordinary query parameters and fragment links are supported; never include confidential material or credentials in URLs. Browser subresources receive the same destination checks, and read-only browsing blocks mutating HTTP methods and WebSockets. External actions retain their existing review controls. Successful source reads are saved as immutable evidence files and carried through context recovery. A successful retry or browser fallback clears its matching current error, while unrelated failures and the historical activity log remain available. Resuming a job replaces its stale status and question; completion shows the final result.

Agent context is scoped to the current task. Delegation starts fresh child contexts with a focused request and only explicitly selected `file_ids` (up to 16 per child); no files are copied by default. Parent history and recovery archives are excluded. Children select output `file_ids` when calling `finish`, so only declared deliverables return to the parent. Technology research delegation proceeds without approval for external reads and COS-local findings and deliverables. Research restrictions persist through delegated work; external changes, including routine Jira writes from research jobs, require action approval. All organizations may delegate preparation without approval; every external change, including all Jira writes, retains exact-action approval. On restart, pending delegation approvals and Technology jobs stopped solely to confirm the old delegation approval rule are queued for reassessment; substantive questions and uncertain actions remain waiting.

Artifact links in delegated requests must have their corresponding job-local file IDs selected in `file_ids`. Missing selections are rejected before any child starts, with a local ID hint when available. Selected links are rewritten to the child's copies. `read_file` accepts local paths, attached IDs and `/api/files/ID` links; older links resolve only when a matching artifact is already registered in the current job. Links alone never grant access to another job's files.

When research needs email or Teams evidence, delegate to Communications with `source_only:true`. These source readers can run one level beyond the normal delegation depth limit and return evidence and selected files to the requesting job. The server prevents them from delegating again or changing external state. Organization routing limits alone should not require the owner to upload mail or recreate the request. Restart the server to load this behavior, then resume an existing blocked job with direction to retrieve its sources through a Communications source reader.

For GitHub evidence, sign in each required account using `gh auth login`, then configure **Connections → GitHub repository accounts** with repository owners mapped to login names, for example `{"work-organization":"work-login"}`. Tokens stay in GitHub CLI's credential store; COS does not switch the active GitHub account or fall back to the account hosting its own repository. Technology and Engineering can use `repository_search` to discover repositories by name and `repository_read` to retrieve a README or a named text file as commit-pinned, job-local evidence. These authenticated GET tools are independent of the public research host allowlist. Organization SSO and repository permissions still apply.

### Stash / Bitbucket Server repositories

Teams can use repositories on `stash.amd.com` or another explicitly configured
Bitbucket Server/Data Center instance alongside GitHub. Bitbucket Cloud is not
covered by this connector. COS installation and release updates still use GitHub;
your team's project repositories can stay on Stash.

1. In **Connections → Stash / Bitbucket Server**, configure the server URL and
   project-key-to-login routes, then save configuration. For example:

   ```json
   {"amd":{"url":"https://stash.amd.com","projects":{"ATE":"your-login"}}}
   ```

   Replace `ATE` with the actual Stash project key. Multiple servers and project
   accounts are supported. A server URL may include an HTTPS port or context path.
2. In **Stash credentials**, enter the saved server name (`amd`), project key and
   your personal access token. Tokens are saved in the OS credential store outside
   OneDrive; settings, model context and action receipts never contain the token.
   Use repository read access for source research and repository write access for
   pushes and PR operations. Stash still enforces branch permissions, required
   reviewers and merge checks. Network/VPN access and your organization's token
   policy apply. Signing in to Jira or a Stash browser tab does not configure this
   token connection.
3. For engineering updates, clone the project locally with Git and register its
   absolute path in the `projects` setting, as for GitHub projects. COS uses that
   clone to create an isolated worktree; it does not change the shared checkout's
   branch. Existing HTTPS and SSH origins are recognized. The connector uses the
   configured HTTPS account for remote reads, fetches and pushes, including when
   the local clone uses SSH.

Repository reads accept a full browser URL such as
`https://stash.amd.com/projects/ATE/repos/example`, an HTTPS clone URL such as
`https://stash.amd.com/scm/ATE/example.git`, or a browse link with
`?at=refs%2Fheads%2Ffeature%2FATE-123`. Pass the source file path separately.
Short `owner/repository` identifiers continue to mean GitHub, so identical names
on different servers cannot silently select the wrong provider. For discovery,
use `repository_search` with `server:"amd"` and optionally `owner:"ATE"`;
follow each result's `next_offset`. `repository_connections` shows the configured
routes without credentials. Source reads and tree pages are pinned to immutable
commits and do not depend on the public web host allowlist.

To publish Stash changes, Engineering creates a feature worktree with
`base:"remote_default"`, stages and applies changes, runs the project checks, and
calls `prepare_publication`. After committing the tested package through the
existing reviewed `run_command` flow, it uses `repository_publish` for `push`,
`create_pr` (with title and description), and, when requested, `merge` (with PR
number). Each write has its own exact-action approval. The server rechecks the
package, commit, account, repository, current default branch and PR version before
execution. A changed base requires integration and retesting. Pushes do not force
updates, and merge calls respect the server's configured merge strategy and checks.
Interrupted writes are reconciled through reads rather than automatically replayed.

`verify_publication` checks the merged PR's exact source commit and packaged file
contents. Where Stash supplies the historical merge commit, verification reads
that commit. Otherwise it checks the current default-branch commit and explicitly
records that identity as `verified_target_commit`; later changes to those files
can prevent verification. Deleted files are also checked. Remote file evidence
is limited to 1 MB per file; use a separate workflow for larger binary changes.
The connector is covered by simulated Stash API tests and real local Git transport
tests. Validate your server version, token policy and merge workflow with a test
repository before rollout.

When a repository location is missing, agents check meeting knowledge and original meeting chat messages before asking the owner. Communications source readers preserve repository links and complete branch names, including slashes, in their results. Repository source reading may also be delegated with `source_only:true` to Technology or Engineering at the normal depth limit. Changing account routes or installing this update does not resume existing blocked source-access jobs; they remain available for manual continuation.

Delegated inputs and returned deliverables use compact storage paths while retaining their original download names and file provenance. If a result handoff fails, only that parent request moves to **Needs attention**; unrelated queued work continues. Child files and the pending handoff remain saved. After resolving the file problem, send direction to the blocked parent to retry the handoff without rerunning its children.

Context checkpoints carry a bounded recent action index, current findings and source read ranges instead of embedding every historical tool result. Full receipts remain available through the job-scoped `read_action_receipt` tool. Text reads default to 4,000 characters and are capped at 8,000 per page. After twelve fresh source pages the agent must save findings with `update_brief` before reading more. Repeated covered ranges and recovery-archive reads do not count as new evidence, even when filenames or requested page sizes change. Existing blocked jobs remain stopped; resuming restores read coverage from saved receipts without replaying tools. Existing archives are retained for diagnosis and are not automatically deleted.

For staged implementation, `edit_file` replaces one exact fragment in a job workspace file using its current SHA256. It preserves a backup and rejects stale hashes or ambiguous matches. This lets agents make small changes without rewriting large files in a model response. Live project edits, application changes and host validation retain their existing approval requirements.

Repeated unchanged tool results trigger automatic recovery from retained findings and source coverage. Recovery allows two verification reads and one brief update, then offers a bounded focused delivery phase instead of immediately asking the owner to continue. Available actions are constrained in both the tool schema and execution checks. Truncated model responses are discarded without execution or replay; COS increases the output budget up to 16,384 tokens and requests smaller complete actions. On restart, jobs stopped by the previous recovery version are queued automatically, preserving their approvals and receipts. Pending approvals, uncertain actions and unrelated setup questions remain waiting. Recovery never fabricates a deliverable or bypasses action approvals. Internal loop failures stay queued for automatic supervisor replanning with the director model and retry delays from one minute up to one hour; they do not become owner setup requests. Saved receipts and source coverage survive retries and restarts. Recovery retains the full orchestration prompt before an initial brief exists, and rejected brief updates do not consume the successful-update allowance. Teams notification emails discover actual Teams chat IDs through list_chats instead of using Outlook conversation IDs.

## Use the tool

Communication triage evaluates your personal obligations before proposing replies or work. FYIs, updates, optional suggestions and tasks assigned to others finish with a useful summary and no follow-up. Completed informational summaries offer optional source-specific mark-as-read controls through exact-action approval (Teams marks the entire chat). Approving or rejecting that read action leaves the summary completed without another planning pass.

Teams activity is grouped by topic within each chat, including one-on-one chats. Related follow-ups and owner replies return to the topic's saved context; unrelated subjects get separate jobs, summaries, guidance and approval updates. Topic routing uses the triage model with bounded previews and recent topic candidates. Ambiguous subjects get a separate topic; failed routing retains the checkpoint for retry. Original text is retained in full. Topic source reads return only assigned messages, while replies and optional mark-as-read actions use the real chat ID. Marking read still affects the entire Teams chat.

After restart, existing Teams history is also split into topics using saved messages and relevant owner feedback, proposals, action receipts and delegated outcomes. Original jobs, summaries and files remain in history. Old pending approvals are superseded, and replaced active jobs are retired after their chat's import commits. Each chat migrates atomically and retries after failure; chats with executing or uncertain external actions wait for outcome verification. Imported topics initially summarize and reassess history with tools restricted to reads and brief updates, so old requests cannot replay actions. Fresh source activity or owner direction resumes normal handling with new action approvals. Migration uses the configured triage model and runs while connected, even when ordinary source polling is disabled.

Historical owner-action decisions must cite an exact excerpt from an original message or owner feedback in the same topic through `owner_obligation_source_id` and `owner_obligation_evidence`. Generic result-review guidance and unverified tests alone do not establish an obligation. Optional review remains available through completed results; missing evidence must be described explicitly. On startup, unsupported imported follow-ups are queued for reassessment unless an approval or external action needs resolution. A recovered brief-validation error preserves the task summary and next step.

1. Enter a request in **Overview**, optionally attach files, and select a registered project or let the app route it.
2. Follow progress in **Job tracker**. The initial organizations are Communications, Technology, Engineering, Strategy, and Self-Improvement. Directors can delegate work and resume with child results.
3. Answer questions and inspect proposed actions in **Needs attention**. Review recipients, bodies, arguments, and diffs before approving. Rejection or new direction lets the agent revise its work. Approval is saved before the app acknowledges your click; the approved action runs in the background and job details show its progress. Acknowledgement does not mean execution succeeded. Results and uncertain outcomes remain in the job, and interrupted mutations never replay automatically.
4. Open **Ready for review** for completed strategy, research and content-generation tasks and their declared deliverables. Routine email summaries, FYI/ignore decisions, deletions, mark-read operations and other action receipts stay in job history. The sidebar count, Overview notice and in-app completion notification surface returned work. Opening a result leaves it queued until you click **Mark reviewed**. Approving an external action does not acknowledge its result. If a result was acknowledged unintentionally, open it in Job tracker and choose **Return to Ready for review**; this restores the saved result without rerunning actions or changing commitments. Send direction to COS to request revisions; revised work returns as a fresh review item. Download artifacts from job details. Use **Download diagnostics & provenance** for a redacted troubleshooting bundle; inspect any export before sharing it.
5. Use **Knowledge base** to manage preferences and reusable context. Forgotten entries stop appearing in future retrieval; original jobs and backups remain.

The review inbox persists across restarts and includes earlier completed work tasks with saved finish metadata. Automatic communication assessments explicitly headed as triage, Jira updates, account reminders or topic summaries stay in history even if a model mislabels them as strategy. This also repairs saved pending snapshots without acknowledging owner review or clearing commitments; requested drafts, reports and documents remain reviewable. On restart, earlier routine triage and action-only entries are removed from the review queue without changing job history or recording an owner review. Delegated results return through their parent request; automatic KB ignore closures stay in history. Review acknowledgement records that you reviewed the result; external actions retain their separate approvals. Restart the server and refresh the browser after updating.

Parent review items preserve the full text and declared files of completed delegated work, including nested drafts, even when the director finishes with a triage summary. Source-only readers and action receipts do not create review items. Startup repairs pending reviews that omitted delegated drafts, with a fresh review revision; already reviewed results stay acknowledged. Producing a draft does not record owner acceptance or complete its underlying commitment. Review cards name the saved document files or the heading of generated inline content instead of the conversation job. Job details list each document or inline draft to read, with thread-handling context collapsed separately. Titles come from the saved review snapshot so newer triage does not rename older work awaiting review.

Completed-result details suggest active commitments using the saved summary and declared document content, with a small vocabulary mapping for terms such as hub/dashboard. Suggestions explain shared content and do not establish fulfillment. Choose a suggested commitment or another active record and classify the result as Fulfills, Progress, or Related. “Mark reviewed & update selected commitments” records those choices and completes only accepted commitments explicitly marked Fulfills; proposed assignments must first be accepted in the commitment register. Ordinary “Mark reviewed” leaves commitments unchanged. Both records retain links, the saved summary, review revision, and document SHA-256 hashes. Changed reviews, files or commitments require a refresh before confirmation; multiple selections are saved atomically and retries do not duplicate evidence. Unavailable files prevent linking but do not prevent ordinary review. Restart the server and refresh the browser after updating.

Delegated research stays available to the parent as internal inputs. For newly delegated substantive work, the director must synthesize the requested final deliverable and finish with `final_output:true`; intermediate notes do not enter the owner review queue. A final handoff selects only authored outputs or the complete inline report, keeps citations as sources, and records which child versions were incorporated so later triage cannot resurface old notes. Topic migration collects returned work before archiving its original conversation. Startup also recovers missing reviews from migration archives without restarting those historical jobs, and keeps the review on the original request to avoid duplicating it across topics. User cancellations and reviewed results stay closed; new child results get a fresh review revision. Legacy triage histories without saved finish metadata do not qualify as generated work merely because they ran in a research organization.

Try: “Prepare a short project planning brief with open questions and next steps.” For engineering, register a project first and request a proposed patch with test evidence.

Email and Teams job details support a conversation with the Chief of Staff. **Send direction to COS** can revise a draft or change a handling decision. Sending direction does not send a message. Revised actions require fresh approval. **Remember guidance for similar emails** saves explicit reusable guidance when checked.

Communications details show **Original messages**, collapsed by default, above the summary, with full text, sender and date for the latest five messages or emails, newest first. Collapse or expand the conversation using its heading; collapse buttons are also available at the top and bottom of the expanded thread. Older email jobs load their source text when opened; use **Load original messages** to retry a failed read or load the latest five Teams messages. Collected messages and full source reads are retained separately from summaries. A preview stays labeled as a preview until the full email is available.

Email body reads use the same immutable-ID client as monitoring and mail actions. Successful reads link the requested ID to Outlook's returned ID, so alternate IDs share full-body evidence without duplicate source cards or optional mark-read controls. Newly collected emails retain their Internet Message ID for a bounded recovery when an item ID becomes invalid or the message moves; recovery requires one exact match and never merges messages by subject or preview. Restart after updating to retry existing source-ID questions once. A later successful source load also resumes a resolved source-ID question, while substantive questions and pending or uncertain actions remain waiting.

Full bodies read through mail search also reconcile unread source envelopes when the saved Internet Message ID, sender and received timestamp match a single server-read mailbox identity. This works in either arrival order and repairs existing source caches on recovery. Duplicate mailbox items with the same Internet Message ID remain unresolved. Source-ID recovery recognizes singular and plural IDs and tolerates a subsequent malformed-ID read error without losing the earlier completion-gate failure; unrelated access errors and action approvals remain waiting.

External sends, Confluence writes, application patch application, host commands, browser interactions, imported MCP calls, merges, deployments, publishing, and deletion require review. Routine Jira operations and job-local artifact work follow the server's policy. See `cos/policy.py` for action classification. Approved host commands and MCP processes run with your Windows account's permissions.

Self-Improvement jobs autonomously create application feature branches, run Python pytest/unittest discovery checks, and apply staged application source patches with target hashes and backups. Local tests use the server's running Python interpreter and may run from the application source directory or the job workspace. They execute trusted development code with host permissions; this is not a sandbox. Arbitrary host commands and external actions retain their approval gates. Definite validation and process-launch failures return to the agent for correction without owner verification. On startup, the app also resumes legacy commands blocked by the missing-executable or rejected-working-directory errors and retires pending Self-Improvement test approvals. COS verifies timeouts and interrupted mutations automatically using read-only source checks. Checks run in background workers, retain evidence across restarts, and stop after three attempts. If the outcome remains unknown, COS reports the limitation and continues other work; the server blocks a repeat of the unresolved action. Users are never asked to assert whether an action executed. Imported connections without an authoritative outcome check remain explicitly unconfirmed, rather than being treated as successful. Exact-action approvals for external writes still apply. Restart the server after updating to load the policy and recover those jobs.

Direct requests in the authenticated local chat can create open commitments with `create_owner_commitment`. The tool cites either the server-verified original request or an explicit owner review message in the same root job, retains that message as evidence, and checks existing commitments in every status without reopening closed items. Set optional `self_name` in local configuration to choose the owner display name; otherwise the configured `self_email` is used. Email, Teams, attachments and delegated work cannot authorize this path. Meeting-derived commitments still use transcript evidence and owner acceptance. This records an item in the commitment register; it does not schedule a notification.

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

Only the authenticated owner's messages in the configured self-chat create top-level requests. Meeting processing uses the installed Teams skill and its existing Microsoft sign-in to retrieve accessible transcript files and save source-linked observations; it does not transcribe recording audio or infer content from filenames.

The same self-chat supports a prompted inbox while monitoring is enabled:

- `!cos status` shows a compact summary and an HTML table with **Job** (including a short context description), **Current Status**, and **Next Steps**. Pages contain up to six top-level tasks, with active work first, and keep complete rows together within the Teams message size limit. Next steps use the current question for input/setup stops, the exact-action review path for approvals, or a labeled saved plan/status-based default. Readable UTC timestamps and short, unique lookup commands remain available. Use `!cos status <task name or ID>` to find a task by its displayed name, original title, project, full ID, or unique ID prefix (at least six characters). `!cos status job:<id>` opens that exact top-level task. Ambiguous names or prefixes return choices instead of selecting a task. Completed and cancelled tasks remain searchable. `!cos status find <text>` searches a literal name, including names beginning with `page`; lists provide their next-page command.
- Individual task status includes its current state, latest saved progress and next step with their recorded time, any question or blocker, delegated-work counts and delegated items needing attention, and links to the existing task/review commands. Saved progress is historical context; a returned child or saved draft does not imply the top-level task is complete. Status is read-only and does not acknowledge reviews, resume jobs, approve actions, or call a model.
- `!cos what needs my attention?` (or `!cos attention`) lists questions, pending approvals, blocked/failed jobs, capacity stops, active commitments and monitoring problems in the same **Job** (with context), **Current Status**, and **Next Steps** table. Delegated jobs needing attention appear too.
- `!cos what's ready for review?` (or `!cos reviews`) lists saved deliverables awaiting acknowledgment in that same table, with the deliverable summary as context and a command to open each saved result.
- `!cos show job:<id>`, `!cos show result:<id>`, `!cos show commitment:<id>` and `!cos show monitor` open the references returned by those lists. Job previews include the exact proposed action arguments and saved diffs. Result previews show the saved content and commands to read declared artifact text in chat. Text extraction omits visual layout; use web job details to download original artifacts, including images and unsupported binary formats.
- Each preview ends with commands for its available actions: `approve`, `reject`, `answer`, `revise`, `reviewed`, commitment `accept`/`complete`/`dismiss`, or `retry-capacity`. Copy its reference token into the displayed command. Answers and revisions resume the original request. Approval uses the existing execution gate and records acceptance before background execution; the reply does not claim execution succeeded. `reviewed` acknowledges only that saved result and leaves commitments unchanged.
- Long lists provide a next-page command; long previews use `!cos more <reference>`. Read every preview page before responding. Tokens expire after 24 hours; changed questions, action arguments, review revisions, artifact bytes and commitments require a fresh preview. Opening an item does not acknowledge it.

Use `!cos inbox` for help. Replies use escaped Teams HTML with explicit paragraphs and line breaks, headings, emphasis and copyable commands. Source HTML remains literal; exact action JSON stays in code blocks across pages. Pagination budgets the rendered UTF-8 message size, including escaping. These fixed commands return directly to the owner self-chat without model inference, and use the configured command prefix when it differs from `!cos`. They do not send proactive notifications or accept directions from other conversations. A repeated source message never repeats an action or a reply. An interrupted request or uncertain reply delivery appears in Needs attention; COS does not automatically replay it. Request the current inbox again and inspect the original item's state. Restart COS after updating to load this route. Existing queued Teams commands keep their original handling.

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

## KB-driven communication intake

Before scheduling agents, intake batches up to 12 new email conversations for one tool-free call to the configured triage model. It checks all active owner-authored or owner-reviewed KB preferences, preserving their scope, conditions and exceptions. Senders, projects and rule wording are not hardcoded. KB edits and forgotten rules apply on the next intake pass without a restart. Suggested knowledge and task outcomes cannot justify dismissal.

The scheduler also checks untouched queued email jobs, including the backlog from earlier versions, before launching an agent. It evaluates all recoverable source envelopes together and revalidates KB and job state before recording a no-action closure. Ambiguous legacy envelopes, existing agent work, owner feedback, approvals, attachments already staged as job files, and delegated tasks continue through their existing workflow. Completed history is retained. Unchanged queue checks are cached in memory; KB or source changes invalidate that cache.

Clear ignore matches close with explicit no-response/no-action decisions and an audit reason containing the supporting KB IDs and revision. Ambiguous or conflicting guidance, insufficient preview evidence, invalid model output and classifier failures use normal triage. The classifier uses one request attempt with an eight-second network timeout and cannot send or delete mail. The ingestion transaction rechecks the KB revision to reject stale decisions. Already-seen mail is not classified again; owner feedback and existing work take precedence. Polling still controls detection time; the shortcut avoids the multi-step agent queue while retaining one lightweight classification pass.

An intake ignore now also queues automatic cleanup without an approval: email messages move to Deleted Items (never permanent purge), and Teams chats are marked read. A separate bounded, tool-free Teams classifier runs after topic routing against current owner-approved preferences, exact topic/sender bindings, and retained topic context. Images, attachments, recording events, uncertain evidence and classifier failures retain normal analysis. Existing analyzed jobs are not eligible. New ignores, including untouched email backlog closed at queue admission, schedule cleanup; historical completed ignores are not swept.

The server saves cleanup in the same transaction as the intake closure, rechecks the KB revision and untouched job before acting, and records the outcome in the job's Automatic intake cleanup panel and activity timeline. Teams marking affects the entire chat: it waits for every known topic to finish and for successful collection, including after partial scan failures or topic migration. Cleanup runs in a separate monitor worker, respects queue pause, and resumes pending work after restart. An interrupted or ambiguous external operation is recorded as uncertain and is never automatically repeated; definite failures remain recorded without reopening the informational job or requesting approval. This exception belongs only to the server's intake path. Agent-proposed deletion/read actions, including ignores reached after analysis, still require exact-action approval. Restart COS after updating to load this behavior.

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

`create_feature_branch` proposes `prefix + ticket + descriptive name + job suffix`
in a job-owned Git worktree at `workspace/checkout`, pinned to the source checkout's
HEAD at creation. Parallel features have separate files, indexes and branches;
the running application's checkout stays on its current branch. A repeated call
resumes that job's same feature; a new feature requires a new job. Worktree identity
and the base commit are saved outside the agent-writable workspace and survive restart.
It never resets or overwrites an existing branch. Configured
branch requirements are checked for each target project before patch approval and
again before application. Jira creation accepts an explicit parent or uses the
configured default, checking live create metadata, hierarchy levels and same-project
subtask restrictions. Unverifiable hierarchy blocks creation rather than guessing.
An empty workflow configuration imposes no new project requirements.

Self-Improvement must apply staged changes in its isolated checkout **before**
running tests there. Autonomous patches and test commands cannot target the live
application checkout. Applying an improvement prepares a feature; it no longer
updates the running source. Publishing, integration into the release branch, and
deployment retain their separate exact-action approvals.

`prepare_publication` captures a self-contained ZIP with the base commit, original
and proposed files (including additions/deletions/binaries), a binary Git patch,
checksums and actual test receipts. A package is marked validated only when recorded
checks passed against those same source bytes and the source stayed unchanged during
testing. Finishing an implementation automatically retains and selects a current
package if the agent omitted it, with an explicit validation status; an unvalidated
draft cannot pass the publication gate. Pass the entire package through explicit
`file_ids` inputs and outputs.
`validate_publication` checks its server-owned identity and integration base. Native
Git write commands require `publication_file_id`, execute only in the job's isolated
checkout, and recheck tested bytes and base at proposal and execution time. This
does not grant publication approval; opaque scripts retain effects review and
exact-action approval.

When concurrent work advances the base, preserve the package, create a fresh
integration job/worktree, and use `apply_publication` for three-way application.
Resolve reported conflicts in that isolated checkout, rerun checks, and prepare a
new package for fresh publication approval. Integration cannot silently replace
another feature or inherit the earlier package's validation. Worktrees and packages
are retained; this workflow does not automatically delete unfinished features.

Artifact registration retains checksum-addressed copies outside agent-writable
workspaces, with version history when an artifact is regenerated. Transfers verify
those copies and recover original bytes after source edits or deletion. Recovery
archives keep their existing compressed storage instead of being duplicated.
`recover_inputs` discovers historical links cited in a job or its attached text
documents and attaches verified copies within the same owner task tree. Supply
`expected_sha256` when a handoff specifies an older version. Links never grant
access to unrelated jobs. Older artifacts without retained copies can be recovered
only if their existing bytes match recorded checksums; otherwise COS must rebuild,
test and prepare a fresh proposal, rather than ask the owner to recreate its
intermediate files. This is an additive migration and does not replay old actions.

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

Repeated extraction of the same owner, deliverable, scope, meeting source and
assignment timestamp is idempotent even when a recap quotes a longer excerpt.
Additional verified excerpts are retained as evidence on the existing record;
legacy fingerprints are recognized without merging existing records automatically.
Related records for the same owner
and scope appear as possible duplicates. Compare both records before merging;
merging retains the target's fields, combines source evidence, preserves the original
record as a redirect and writes an audit entry. Neither repeated extraction nor new
evidence resurrects a dismissed or completed item. Concurrent review uses versions
and rejects stale changes instead of overwriting them.

`teams_attachments` downloads images and SharePoint documents from the topic's
saved messages. `meeting_artifacts` discovers shared meeting files and returns
downloaded document IDs for `read_file`, including PDFs that were previously
metadata-only. Each file is limited to 25 MB; discovery reports incomplete scans
and access failures. Downloaded files are evidence, never execution instructions.

`read_file` reads text/code/CSV/JSON/XML, HTML, RTF, EML, PDF, DOCX, PPTX tables
and grouped text, XLSX/XLSM cached values, OpenDocument, EPUB, and ZIP listings.
Use `member` for one exact ZIP entry, and `page` (one-based) to inspect a PDF page
visually, including diagrams and scanned text. PDF text extraction alone does not
inspect visuals. PNG/JPEG/GIF/WebP and single-frame BMP/TIFF images are supported.
Encrypted/protected files, legacy binary Office formats, unsupported binaries,
oversized files and multi-frame TIFFs report explicit limitations. Convert those
to an accessible modern document, PDF or individual page images. Audio/video
transcription is unavailable; meeting speech uses the separate transcript tool.

The additive database tables are created on server restart. Existing summaries and
planning files are retained; no bulk conversion or historical assignment is inferred
on startup. To populate older meetings, ask COS to reread their registered transcripts
and propose commitments for review. Each new meeting proposal has a linked execution task waiting for confirmation.
Confirming an assignment queues that task and preserves your direction. The scheduler
also recovers confirmed legacy commitments without an execution task. Execution
reuses available source documents and prior deliverables, performs preparation, and
brings concrete external actions for exact approval. The register shows execution
status and the next action. A plan alone cannot finish an execution task; a specific
dependency remains visible until resolved. Delivery and owner verification of
fulfillment are separate. Dismissal, completion and merging stop unfinished linked
work; cancellation and already delivered work are never automatically replayed.

## Source map

Open a job and select **Rename** to save your own name (up to 120 characters).
Automatic names preserve descriptive phrases rather than applying a seven-word cutoff. Names are bounded to 120 characters, using word boundaries where practical and an ellipsis whenever meaningful text is omitted. Versions, filenames and qualifiers stay intact. Older clipped names are expanded only from matching retained task text; commitment execution names use their original recorded request. This changes display names without rewriting task requests or owner-entered names.
Your name appears in request lists, job details, and delegated work, and persists
across agent updates and restarts. **Use automatic name** restores the generated
label. Renaming leaves the original request and job execution unchanged. Restart
the server and refresh the browser after installing this update.

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

### Research delegation capabilities

Delegation accepts an optional `required_tools` list for each task. COS checks the entire batch before creating workers and rejects assignments whose organization or source restrictions cannot provide those tools. Restricted Technology source readers can use `fetch_url`, `browser_read`, and `jira_read` to retrieve evidence; the configured web research policy, redirect validation, organization routing, depth limits, and external-action approval policy still apply. Source readers cannot delegate or perform browser interactions or external writes. Existing source-reader sessions receive the same read capabilities on resumption.

### Sender-scoped guidance

Owner feedback can explicitly remember guidance for one source sender across conversations. Choose a recorded identity in the review form; COS previews its source, label, and exact email address or Teams contact ID. Sender scope is separate from conversation scope, and is editable in Knowledge base. Display names, quoted headers, and message bodies never establish sender identity. Existing rules and pending suggestions keep their current scope. Older jobs without a stable identity require source refresh before sender scope is available. Sender bindings preserve the guidance text, conditions and exceptions; they do not authorize sends or deletes. Mixed-sender email batches always fall back to normal triage when a sender rule is cited.


### Preparation and external-change approvals

COS autonomously performs task-related reads, searches, analysis, delegation,
validation and preparation of drafts and artifacts inside COS. Approval belongs
at the point an operation would change something outside COS, after the concrete
proposal has been prepared. This boundary applies to every organization and
nested worker, regardless of service or tool. A task with multiple external
changes needs approval for each concrete change; approval of preparation or a
parent task never grants blanket permission to its children.

All Jira creates, edits and comments require approval, as do calendar changes,
invitations, messages, mark-read, deletion, Confluence writes, project branches
and patches, publishing, merging and deployment. COS-local artifacts and native
Self-Improvement changes to COS retain their local allowances.

`approval_mode` defaults to `auto_review`. Native reads and delegation run
immediately. Flexible commands, installed-skill Python scripts, imported MCP
calls and browser interactions can receive independent effects review. The
reviewer inspects exact arguments and available complete Python sources/local
dependencies or the actual imported tool contract. Read-only operations can use
query POSTs; the decision concerns effects rather than HTTP method or tool name.
A worker's description or MCP read-only hint cannot grant authorization. Reviewed
operations receive a job-scoped single-use token; changed arguments, dependencies
or tool contracts invalidate it. Skill scripts run copies of inspected sources
with the server interpreter. This review is a model-based control, not a sandbox;
it is not a guarantee that arbitrary programs have no side effects. Unknown or
opaque effects and unavailable/malformed reviews return a replanning error; COS
uses another verifiable read route or reports the specific blocker. Uncertainty
alone does not request owner approval. A classified external change does.
Automatically reviewed browser navigation also blocks non-read HTTP methods and
WebSockets, so some interactive search pages may still need manual review.

In Connections, **Review preparation automatically** enables this effects review;
**Review flexible tools manually** retains manual review of flexible operations.
Native read and delegation allowances apply in both modes. Findings and review
reasons remain in job history. On restart, provably unstarted legacy delegation
approvals and eligible pending flexible actions are queued for reassessment;
executing and uncertain actions are never replayed. A previous preparation-review
stop is not repeatedly resumed automatically. Pending external-write approvals
remain pending. Restart the server and refresh the browser after updating.

### Jira comment reads

Communications and other Jira readers can request `jira-issue` with args
`["EX-123","--comments"]`, or `jira-comments` with args `["EX-123"]`.
The latter supports `--limit` and `--order`; match the requested comment ID in
the returned comments. Issue reads omit comments unless explicitly requested.
Malformed positional lookups such as `["EX-123","comments","12345"]` are
rejected locally with the correct syntax before any Jira request. A comment
lookup failure does not mean an earlier successful issue read failed.
Restart COS after updating, then resume a blocked job to retry its comment read.

### Original message formatting

Original Teams messages display paragraphs, emphasis, lists, quotes, code,
tables, and safe web/email links. Mentions and image/attachment labels stay
readable. Source scripts, styles, active elements, and remote images are not
loaded. Email bodies keep their existing plain-text presentation.

Formatting is generated for display from saved messages, so existing history
benefits without reimporting it. The original source bodies and approval text
are preserved. Restart COS and refresh the browser after updating.

### Optional Teams mark-as-read authentication

A Teams summary can finish even if optional mark-as-read is rejected. The job keeps its summary and a cleanup warning; rejected actions remain recorded as not executed. On restart, COS repairs old informational jobs stuck solely on a definitive read rejection. New source activity, incomplete sources, unfinished delegated work, and uncertain action outcomes retain their existing handling. Nothing is replayed during repair.

The Graph `markChatReadForUser` endpoint requires delegated `Chat.ReadWrite`; `ChatMessage.Send` does not cover it. The installed shared Microsoft skill sign-in may lack this permission. Repeating that sign-in does not add tenant consent.

To enable the optional separate connection:

1. Have your tenant administrator provide an organization-approved, single-tenant Entra app registration. Add Microsoft Graph **delegated** permissions `Chat.ReadWrite` and `User.Read`, grant consent according to tenant policy, and enable **Authentication → Allow public client flows**. This uses MSAL device code authentication; no client secret is needed. Application-only authentication is not supported by the mark-as-read API.
2. Install updated dependencies with `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`, then restart COS.
3. In **Connections → Teams mark-as-read**, save the app's Application (client) ID and Directory (tenant) ID. Select **Sign in to Teams app** and follow the instructions in the Microsoft 365 panel. Sign in with the same account as the shared Microsoft connection.
4. Request and approve a fresh, exact mark-as-read action. Saving configuration or signing in never approves or retries an action.

The separate MSAL cache is encrypted by Windows DPAPI under `%LOCALAPPDATA%/AMD-ChiefOfStaff/teams-auth`, outside the OneDrive workspace. On other supported hosts, MSAL Extensions requires an available encrypted OS credential store; COS does not fall back to plaintext. Cache files are isolated by tenant and client ID. Clearing both IDs restores the shared connection. Outlook, source reads, and message sends continue using the installed skills.

References: [Graph markChatReadForUser permissions](https://learn.microsoft.com/en-us/graph/api/chat-markchatreadforuser?view=graph-rest-1.0), [register a public desktop client](https://learn.microsoft.com/en-us/entra/identity-platform/scenario-desktop-app-registration), [MSAL token acquisition](https://learn.microsoft.com/en-us/entra/msal/python/getting-started/acquiring-tokens).
