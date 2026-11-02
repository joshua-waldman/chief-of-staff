param(
    [ValidateRange(1, 65535)][int]$Port = 8765,
    [switch]$Foreground,
    [switch]$Supervise,
    [switch]$Stop
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskName = 'ChiefOfStaff-' + [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
if ($Stop) {
    Stop-ScheduledTask -TaskName $taskName
    Write-Host 'Chief of Staff stopped. It will start again at your next Windows sign-in.'
    return
}
$pythonExe = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
    & $pythonExe -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
}
if ($Foreground) {
    & $pythonExe run.py --port $Port
    exit $LASTEXITCODE
}
if ($Supervise) {
    # The task owns this process tree, independently of the launching terminal.
    # A per-user mutex also prevents separate installations sharing runtime data.
    $mutex = New-Object Threading.Mutex($false, ('Global\' + $taskName))
    $ownsMutex = $false
    try {
        try { $ownsMutex = $mutex.WaitOne(0) }
        catch [Threading.AbandonedMutexException] { $ownsMutex = $true }
        if (-not $ownsMutex) { throw 'Chief of Staff already has a supervisor running.' }
        $logDir = Join-Path $PSScriptRoot 'data/launcher'
        New-Item -ItemType Directory -Path $logDir -Force | Out-Null
        while ($true) {
            # Retain logs from the current and previous server runs.
            foreach ($name in @('stdout', 'stderr')) {
                $log = Join-Path $logDir ($name + '.log')
                if (Test-Path -LiteralPath $log) {
                    Move-Item -LiteralPath $log -Destination ($log + '.previous') -Force
                }
            }
            $server = Start-Process -FilePath $pythonExe -ArgumentList @('-u', 'run.py', '--port', "$Port") -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDir 'stdout.log') -RedirectStandardError (Join-Path $logDir 'stderr.log') -PassThru
            $server.WaitForExit()
            Add-Content -LiteralPath (Join-Path $logDir 'stderr.log') -Value ("Server exited with code {0} at {1}; restarting in 5 seconds." -f $server.ExitCode, (Get-Date -Format o))
            Start-Sleep -Seconds 5
        }
    }
    finally {
        if ($ownsMutex) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
    return
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$powershellExe = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
$arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -Supervise -Port {1}' -f $PSCommandPath, $Port
$action = New-ScheduledTaskAction -Execute $powershellExe -Argument $arguments -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Run Chief of Staff independently of Codex, start at Windows sign-in, and recover from process exits.' -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Host "Chief of Staff runs in the background at http://127.0.0.1:$Port."
Write-Host 'Windows will start it at sign-in. Closing Codex or this window will not stop it.'
Write-Host 'Use Start-ChiefOfStaff.ps1 -Stop to stop it, or -Foreground for development.'
