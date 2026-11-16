<#
.SYNOPSIS
First-time Windows COS setup, with optional desktop-host access setup.
.DESCRIPTION
Use on the single COS host, not on a laptop used only as an RDP client.
Existing configuration is preserved. Run -WhatIf for a nonmutating preview.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateRange(1,65535)][int]$Port = 8765,
    [string]$SelfEmail,
    [ValidateSet('Ask','Enable','Skip')][string]$DesktopAccess = 'Ask',
    [ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9.-]*$')][string]$RemoteComputer,
    [ValidateRange(1,65535)][int]$RemotePort,
    [string]$LaptopKitDirectory = (Join-Path $env:LOCALAPPDATA 'AMD-ChiefOfStaff\LaptopSetup'),
    [string]$PythonExe = 'python',
    [switch]$NonInteractive,
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
$appRoot = $PSScriptRoot
$configPath = Join-Path $appRoot 'config.local.json'
$pythonPath = Join-Path $appRoot '.venv\Scripts\python.exe'

foreach ($syncRoot in @($env:OneDrive, $env:OneDriveCommercial, $env:OneDriveConsumer)) {
    if ($syncRoot) {
        $syncPath = [IO.Path]::GetFullPath($syncRoot).TrimEnd('\')
        if ($appRoot -eq $syncPath -or $appRoot.StartsWith($syncPath + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Install the COS host outside synchronized OneDrive folders so its live database stays on one machine.'
        }
    }
}

if ($NonInteractive -and $DesktopAccess -eq 'Ask' -and -not $WhatIfPreference) {
    throw 'Specify -DesktopAccess Enable or Skip when using -NonInteractive.'
}
# Check deployment ownership before creating files or replacing a task.
$taskName = 'ChiefOfStaff-' + [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$task = Get-ScheduledTask -ErrorAction Stop | Where-Object TaskName -eq $taskName
if ($task) {
    if (@($task.Actions).Count -ne 1) { throw 'The registered COS task has an unexpected action layout. Inspect it before changing setup.' }
    foreach ($action in $task.Actions) {
        if (-not $action.WorkingDirectory -or [IO.Path]::GetFullPath($action.WorkingDirectory).TrimEnd('\') -ne $appRoot.TrimEnd('\')) {
            throw 'COS is registered from another installation. Use that installation; setup will not replace its task.'
        }
    }
    if ([string]$task.State -eq 'Running' -and -not $WhatIfPreference) {
        throw 'COS is already running. Use the existing app, or stop it before changing its environment.'
    }
}
if (@(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object LocalPort -eq $Port).Count -and -not $WhatIfPreference) {
    throw 'The selected port is already in use. Identify that listener before starting another COS server.'
}

$existingConfig = Test-Path -LiteralPath $configPath
if ($existingConfig) {
    $saved = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    if ($SelfEmail -and $SelfEmail -ne $saved.self_email) {
        throw 'Existing owner configuration differs. Update it deliberately; setup will not replace it.'
    }
    Write-Host 'Preserving existing configuration.'
}
else {
    if (-not $SelfEmail -and -not $WhatIfPreference) {
        if ($NonInteractive) { throw '-SelfEmail is required for a new noninteractive installation.' }
        $SelfEmail = Read-Host 'Owner organizational email (not a password or API key)'
    }
    if ((-not $SelfEmail -and -not $WhatIfPreference) -or ($SelfEmail -and $SelfEmail -notmatch '^[^\s@]+@[^\s@]+\.[^\s@]+$')) { throw 'Enter a valid owner email address.' }
}
if ($DesktopAccess -eq 'Ask') {
    if ($WhatIfPreference) { Write-Host 'Interactive setup would ask whether to prepare desktop access.' }
    else {
        do { $answer = Read-Host 'Use this desktop as the COS host and prepare laptop access? [y/n]' }
        until ($answer -match '^(?i:y|yes|n|no)$')
        $DesktopAccess = if ($answer -match '^(?i:y|yes)$') { 'Enable' } else { 'Skip' }
    }
}
if ($DesktopAccess -eq 'Enable') {
    $kitArgs = @{ Destination = $LaptopKitDirectory }
    if ($RemotePort) { $kitArgs.RemotePort = $RemotePort }
    if ($RemoteComputer) { $kitArgs.RemoteComputer = $RemoteComputer }
    # Resolve the host and check kit conflicts before starting installation.
    & (Join-Path $appRoot 'scripts\New-CosLaptopKit.ps1') @kitArgs -WhatIf
}
if (-not $PSCmdlet.ShouldProcess($appRoot, 'Install dependencies, preserve/create owner settings, and start the per-user COS task')) { return }

Push-Location -LiteralPath $appRoot
try {
    $interpreter = if (Test-Path -LiteralPath $pythonPath -PathType Leaf) { $pythonPath } else { $PythonExe }
    $version = & $interpreter --version
    if ($LASTEXITCODE -ne 0 -or $version -notmatch '^Python 3\.11\.') { throw 'Python 3.11 is required. Provision it through your approved software process, or supply -PythonExe.' }
    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
        & $PythonExe -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
    }
    & $pythonPath -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Setup did not start COS.' }
    if (-not $existingConfig) {
        # Start disconnected: integrations and monitoring need deliberate setup.
        $initial = @{ self_email = $SelfEmail; projects = @{}; monitor_enabled = $false; queue_paused = $true }
        [IO.File]::WriteAllText($configPath, ($initial | ConvertTo-Json -Depth 4), [Text.UTF8Encoding]::new($false))
    }
    & (Join-Path $appRoot 'Start-ChiefOfStaff.ps1') -Port $Port
    $ready = $false
    for ($attempt = 0; $attempt -lt 15; $attempt++) {
        try {
            $page = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/" -TimeoutSec 2
            if ($page.StatusCode -eq 200 -and $page.Content -like '*Chief of Staff*') { $ready = $true; break }
        }
        catch { }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) { throw 'The task was started but the COS page was not ready. Inspect data/launcher logs before retrying; do not start a duplicate server.' }
    if ($DesktopAccess -eq 'Enable') {
        & (Join-Path $appRoot 'scripts\Install-CosShortcuts.ps1') -Port $Port
        & (Join-Path $appRoot 'scripts\New-CosLaptopKit.ps1') @kitArgs
    }
    Write-Host 'COS is ready locally. Open Connections to enter the gateway key securely, choose available models and authenticate selected services.'
    Write-Host 'New installations start disconnected with monitoring disabled. Connect only after completing service setup.'
    if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$Port/" }
}
finally { Pop-Location }
