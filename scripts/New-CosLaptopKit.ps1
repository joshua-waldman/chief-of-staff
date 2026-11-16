<#
.SYNOPSIS
On the COS host, prepare a private laptop connection kit for that host.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9.-]*$')][string]$RemoteComputer,
    [ValidateRange(1,65535)][int]$RemotePort,
    [string]$Destination = (Join-Path $env:LOCALAPPDATA 'AMD-ChiefOfStaff\LaptopSetup')
)
$ErrorActionPreference = 'Stop'
if (-not $RemoteComputer) {
    $network = [Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties()
    $RemoteComputer = $network.HostName
    if ($network.DomainName -and $RemoteComputer -notlike '*.*') {
        $RemoteComputer += '.' + $network.DomainName.Trim('.')
    }
}
if (-not $RemoteComputer) { throw 'Cannot determine the host name. Supply -RemoteComputer with the private name approved for this host.' }
if (-not $RemotePort) {
    $RemotePort = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp' -ErrorAction Stop).PortNumber
}
$Destination = [IO.Path]::GetFullPath($Destination)
$installer = Join-Path $PSScriptRoot 'Install-CosShortcuts.ps1'
if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) { throw 'Shortcut installer is missing from this checkout.' }
foreach ($name in @('Install-CosShortcuts.ps1','COS Remote Desktop.lnk','READ-ME-FIRST.txt')) {
    if (Test-Path -LiteralPath (Join-Path $Destination $name)) {
        throw 'This kit already exists. Choose a new -Destination to preserve its existing files.'
    }
}
Write-Host "Laptop connection target: ${RemoteComputer}:$RemotePort"
Write-Host 'Verify this private name and port from the laptop through the approved connection.'
if ($PSCmdlet.ShouldProcess($Destination, 'Create a laptop kit for this COS host')) {
    [void][IO.Directory]::CreateDirectory($Destination)
    Copy-Item -LiteralPath $installer -Destination (Join-Path $Destination 'Install-CosShortcuts.ps1')
    & $installer -Mode Remote -RemoteComputer $RemoteComputer -RemotePort $RemotePort -DesktopDirectory $Destination
    $instructions = @"
COS laptop setup

This private kit connects to ${RemoteComputer}:$RemotePort. It is specific to
the COS host that generated it; other owners must generate their own kit.

1. Copy this folder to your laptop through approved private storage.
2. Connect to the organization's approved private network.
3. Copy COS Remote Desktop.lnk to the laptop desktop and double-click it.
4. Sign in to your host. Open the Chief of Staff icon inside that remote desktop.

To recreate the laptop icon, run this from the extracted kit folder in PowerShell:
.\Install-CosShortcuts.ps1 -Mode Remote -RemoteComputer $RemoteComputer -RemotePort $RemotePort
Use -Force only to replace an existing icon deliberately.

The shortcut prompts for credentials but Windows selects the sign-in method.
If face/PIN sign-in gives a certificate error, choose More choices, then Use a
different account and use your organizational password if permitted. The shortcut
does not force password sign-in. Certificate/policy repair may require IT.

Disconnect the remote session when finished rather than signing out. The host's
physical screen normally locks during RDP. Sleep or sign-out interrupts COS.
This kit contains no credentials, COS database, configuration or job history.
It does not enable RDP, install a VPN, or prove laptop reachability.
"@
    [IO.File]::WriteAllText((Join-Path $Destination 'READ-ME-FIRST.txt'), $instructions, [Text.UTF8Encoding]::new($false))
    Write-Host "Laptop kit created: $Destination"
}
