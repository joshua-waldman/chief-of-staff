<#
.SYNOPSIS
Create desktop icons for the local COS browser UI and/or a remote Windows host.
.DESCRIPTION
Run Local on the COS host and Remote on the laptop. Remote mode needs no COS
installation, Python or administrator rights. No credentials are stored.
Existing shortcuts are left unchanged unless -Force is supplied.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateSet('Local','Remote','Both')][string]$Mode = 'Local',
    [ValidateRange(1,65535)][int]$Port = 8765,
    [ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9.-]*$')][string]$RemoteComputer,
    [ValidateRange(1,65535)][int]$RemotePort = 3389,
    [string]$DesktopDirectory = [Environment]::GetFolderPath('DesktopDirectory'),
    [switch]$Force
)
$ErrorActionPreference = 'Stop'
if ($Mode -in @('Remote','Both') -and -not $RemoteComputer) {
    throw '-RemoteComputer is required for Remote or Both mode.'
}
if (-not $DesktopDirectory -or -not (Test-Path -LiteralPath $DesktopDirectory -PathType Container)) {
    throw 'Desktop directory does not exist. Supply an existing folder with -DesktopDirectory.'
}
if ($Mode -in @('Local','Both')) {
    $urlPath = Join-Path $DesktopDirectory 'Chief of Staff.url'
    if ((Test-Path -LiteralPath $urlPath) -and -not $Force) {
        Write-Host "Left existing shortcut unchanged: $urlPath (use -Force to replace)."
    }
    elseif ($PSCmdlet.ShouldProcess($urlPath, 'Create COS browser shortcut')) {
        [IO.File]::WriteAllText($urlPath, "[InternetShortcut]`r`nURL=http://127.0.0.1:$Port/`r`n", [Text.Encoding]::ASCII)
        Write-Host "Created $urlPath"
    }
}
if ($Mode -in @('Remote','Both')) {
    $linkPath = Join-Path $DesktopDirectory 'COS Remote Desktop.lnk'
    if ((Test-Path -LiteralPath $linkPath) -and -not $Force) {
        Write-Host "Left existing shortcut unchanged: $linkPath (use -Force to replace)."
    }
    elseif ($PSCmdlet.ShouldProcess($linkPath, 'Create Remote Desktop shortcut')) {
        $shell = New-Object -ComObject WScript.Shell
        try {
            $shortcut = $shell.CreateShortcut($linkPath)
            $shortcut.TargetPath = Join-Path $env:SystemRoot 'System32\mstsc.exe'
            $shortcut.Arguments = "/v:${RemoteComputer}:$RemotePort /prompt"
            $shortcut.Description = 'Connect through the approved private route, then open Chief of Staff on the remote desktop.'
            $shortcut.IconLocation = "$($shortcut.TargetPath),0"
            $shortcut.Save()
            Write-Host "Created $linkPath"
        }
        finally {
            if ($shortcut) { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($shortcut) }
            [void][Runtime.InteropServices.Marshal]::ReleaseComObject($shell)
        }
    }
}
