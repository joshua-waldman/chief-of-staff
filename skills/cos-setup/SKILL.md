---
name: cos-setup
description: Install and configure the Windows Chief of Staff (COS) application, including optional desktop-host access and laptop connection setup. Use for a new COS installation or completing its setup; distinguish a COS host from a laptop that only connects to it.
---

# Set up Chief of Staff

Carry out COS setup with the user in Claude or Codex. Inspect the machine first,
ask for missing choices, perform the authorized setup, and verify each completed
part. This file can be attached to a session without any other skills installed.

## Choose the role and preserve existing work

- Determine Windows version, installed Python/Git, existing COS checkout, running
  listener and the current user's `ChiefOfStaff-<SID>` scheduled task. Inspect
  task working directories rather than assuming the current checkout is deployed.
  Read repository `AGENTS.md` and the current README before modifying that checkout.
- Ask whether this machine will **host COS**, host COS on a **desktop with optional
  laptop access**, or act as a **laptop client only**. Reuse an explicit choice.
  Do not infer the desired role merely from the hardware's form factor.
- Reuse an existing installation when appropriate. If it is running and healthy,
  complete only missing setup; do not reinstall dependencies or replace its task.
  Preserve existing settings, data and sign-ins. For updates, follow the README's
  stop/backup procedure and agree a restart when active work would be interrupted.
- Use one authoritative host/store. A client-only laptop needs no COS server,
  database, Python environment, copied credentials or monitoring process.

## Optional: download the repository

This skill is distributable separately from COS. Before installing a new host,
ask whether the user already has the application files or wants them downloaded.
Reuse their existing checkout/extracted folder when supplied. Skip a full repo
download for a laptop-only client; obtain just its connection kit or standalone
shortcut installer instead.

The default source is the private repository
`https://github.com/joshua-waldman/chief-of-staff.git`. Accept an organization-approved
mirror, repository URL or revision supplied by the user; do not assume every hub
user has access to the author's repository. Installing this skill from a skill hub
does not grant repository access. Check access with existing authentication and let
the user complete any sign-in or organization SSO. Never request tokens in chat or
embed credentials in download URLs, shell commands, archives or this skill.

If downloading is selected, use Git clone when Git is available. Choose a local
destination outside OneDrive/other synchronized folders, for example
`Join-Path $env:LOCALAPPDATA 'AMD-ChiefOfStaff\App'`, respecting the user's preferred
location. Inspect an existing destination rather than overwriting it. Example for
the default source and a new destination:

```powershell
$cosRepository = 'https://github.com/joshua-waldman/chief-of-staff.git'
$cosInstallDirectory = Join-Path $env:LOCALAPPDATA 'AMD-ChiefOfStaff\App'
git clone -- $cosRepository $cosInstallDirectory
```

Substitute the user's selected source and destination when different. If clone
fails, inspect the error and retained destination before retrying. Do not erase an
existing folder, reset another checkout or repeatedly retry a permission failure.

If Git is unavailable or a ZIP is preferred, use the repository provider's
authenticated **Download ZIP** action or an available authenticated download tool.
Preserve binary bytes, inspect archive paths before extraction and extract into
a new local folder; never overwrite an existing installation. Do not invent an
anonymous/public download route for a private repository. If neither access method
is available, report the missing repository entitlement or approved provisioning
step and continue independent preparation. ZIP installs have no Git update history
or hooks; Git remains optional until the user chooses a Git-based workflow.

After obtaining files, verify the selected source/revision where available and
locate the actual application root (`run.py`, `cos/`, README and setup scripts),
including any enclosing directory created by ZIP extraction. Read that checkout's
`AGENTS.md` and README. Use an authorized revision containing the setup scripts;
do not claim a feature branch is released on main. If the revision lacks the
scripts below, follow that revision's README/manual install steps instead.

## Check host prerequisites

Host prerequisites are Windows 10/11, Python 3.11 and authorized AMD Unified gateway
access. Git is needed for cloning/updating. Edge supports optional browser tasks;
Docker and Node are optional development/tooling requirements, not basic install
blockers. If Python or other system software is missing, use the organization's
approved provisioning method. Explain any actual prerequisite that needs IT or
administrator action; continue independent setup rather than repeatedly retrying.

## Gather choices, then run host setup

Ask only for information not already available:

- Owner organizational email for a new installation; inspect only that field when
  existing configuration supplies it. Do not dump private config into chat/logs.
- Whether to prepare desktop/laptop access. If selected, explain that RDP locks
  the host's physical console and depends on an approved private route.
- Which optional services are wanted: Microsoft mail/calendar/Teams, Atlassian,
  GitHub projects, browser work and sandbox execution. Do not enable everything.
- Existing project paths when engineering work is requested. Check that they exist.

Run the guided installer from the selected host checkout, passing answers so the
agent does not block on invisible terminal prompts:

```powershell
.\Setup-ChiefOfStaff.ps1 -SelfEmail owner@example.com -DesktopAccess Enable -NonInteractive -NoBrowser
# Use -DesktopAccess Skip when desktop access is not wanted.
# Use -PythonExe <approved-python-executable> if python on PATH is not Python 3.11.
```

Substitute the actual supplied email; the example is not a default owner. For an
existing configuration omit `-SelfEmail` unless it matches. The script creates a
venv, installs requirements, preserves/creates settings, registers the per-user
sign-in task and checks the local page. New installs start with queue paused and
monitoring off. Do not use this script on a client-only laptop. `-WhatIf` previews
without installation or task changes. Respect execution policy; do not disable
managed restrictions to get a script running.

If an existing COS task points to another checkout, a listener owns the chosen
port, or an existing kit would be overwritten, investigate that conflict before
retrying. Do not start another server, change ports blindly, reset a checkout or
delete owner data to make setup pass. A failed readiness check may leave a started
task; inspect its state/logs before taking any further action.

## Complete only the selected connections

Open the host's loopback UI, normally `http://127.0.0.1:8765`. Under **Connections**:

1. Have the user enter the AMD gateway key in the local secure setup UI. It is
   stored in Windows Credential Manager; never put it in this skill, config,
   command arguments, chat, source control or a transfer kit. Run the gateway test
   once and select model IDs their gateway account supports. A ChatGPT/Claude
   subscription does not supply the gateway. Do not add a public-model fallback.
2. Discover the compatible service skills/integrations named by the current
   README. Inspect what is installed; do not assume another agent's tools or skills
   exist here. Use approved distribution sources for missing dependencies. Follow
   their real authentication entrypoints and allow the user to complete browser,
   device-code or MFA steps privately. Test read access for selected services.
3. Configure selected projects/account routes using the current README and UI.
   Do not copy another user's tokens or change unrelated accounts/defaults.
4. Discuss the existing approval mode. Keep the queue paused until the user is
   ready to connect. Enable Microsoft monitoring only when explicitly selected
   after authentication. Desktop access does not change action-approval policy.

An optional integration failure should not prevent delivery of the functioning
base application. Report the specific incomplete service and actual next step.

## Optional desktop host and laptop setup

`-DesktopAccess Enable` creates a host browser icon and a private laptop kit under
`Join-Path $env:LOCALAPPDATA 'AMD-ChiefOfStaff\LaptopSetup'`. The kit generator reads
this host's own DNS name/suffix and configured RDP port. If IT requires an alias,
pass `-RemoteComputer` explicitly. A different owner must generate their own kit;
never ship the author's generated shortcut as a universal address.

For an already installed/running host, prepare access without reinstalling COS:

```powershell
.\scripts\Install-CosShortcuts.ps1
.\scripts\New-CosLaptopKit.ps1 -Destination <new-private-kit-folder>
.\scripts\Test-CosRemoteAccess.ps1
```

Transfer the kit only through the user's chosen approved private storage. The kit
contains a credential-prompting RDP shortcut, its generic installer and instructions;
it must not contain configuration, credentials, databases or job history. Creating
a kit is not permission to upload/share it; use existing user authorization or ask
for the destination before transferring it. No public sharing links are needed.

On a client-only laptop, obtain the user's own kit or their host's private DNS
name and RDP port. If no kit is available, the standalone shortcut installer from
the repository is sufficient:

```powershell
.\Install-CosShortcuts.ps1 -Mode Remote -RemoteComputer desktop.example.com
```

Use the actual COS host, not the laptop's machine name. The laptop must have an
approved private route to that host. Inspect installed connectivity software and
current organizational guidance; a VPN or Zscaler installation alone does not
prove that route or establish policy approval. Test only the named desktop/port,
then owner login and the COS page in the remote session. Do not enable RDP/SSH,
open firewall ports, disable NLA, alter corporate power/session/Hello policy or
install a new VPN/overlay as a shortcut to resolving a missing route.

The RDP icon uses `/prompt`, which requests credentials but does not force the
password provider. If Windows Hello produces a certificate/UPN error, the user can
choose **More choices > Use a different account** and a password when allowed.
Recurring certificate failures require the organization's supported repair path.

## Verify and hand off

Verify the expected single task, loopback-only COS listener and local UI. Check
selected connections individually. Validate history/downloads if existing data is
being reused. With the user's agreement, use one harmless local request to test
the queue; do not send a real message or perform a real external write as a test.
Restore any preexisting queue/monitoring settings you temporarily changed.

For remote use, distinguish TCP reachability, authenticated RDP login and a working
COS page; report which was actually tested. Confirm that normal disconnect leaves
the owner signed in, while sleep/sign-out interrupt processing and reboot requires
sign-in. Do not promise unattended boot-time operation or always-on availability.

Finish with the UI/icon location, authoritative host and data location (mark any
unverified override), selected services that passed, optional work still pending,
and how to start/stop/uninstall using the current README. Do not claim setup is
complete while gateway authentication or a required user sign-in remains pending.
