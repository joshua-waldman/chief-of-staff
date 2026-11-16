<#
.SYNOPSIS
Read-only COS host inventory, or a single-target TCP check from the laptop.
.DESCRIPTION
Does not enable services, change policy/firewalls, read credentials, or start COS.
Output contains private machine/path information; keep it out of source control.
TCP success is reachability evidence, not proof of authorization or RDP login.
#>
param(
    [ValidateRange(1,65535)][int]$Port = 8765,
    [ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9.-]*$')][string]$Target,
    [ValidateRange(1,65535)][int]$TargetPort = 3389
)
$ErrorActionPreference = 'Stop'

function Observe([scriptblock]$Probe) {
    try { @{ status = 'observed'; value = (& $Probe) } }
    catch { @{ status = 'unknown'; error_type = $_.Exception.GetType().Name } }
}

if ($Target) {
    $client = New-Object Net.Sockets.TcpClient
    try {
        $pending = $client.ConnectAsync($Target, $TargetPort)
        $connected = $pending.Wait(5000) -and $client.Connected
        @{ target = $Target; port = $TargetPort; tcp_connected = $connected;
           limitation = 'Does not verify owner login, corporate permission, MFA, or COS UI.' } | ConvertTo-Json
    }
    catch {
        @{ target = $Target; port = $TargetPort; tcp_connected = $false;
           error_type = $_.Exception.GetType().Name } | ConvertTo-Json
    }
    finally { $client.Dispose() }
    return
}

$report = [ordered]@{
    collected_at = (Get-Date -Format o)
    computer_name = $env:COMPUTERNAME
    dns_suffix = (Observe { (Get-CimInstance Win32_ComputerSystem).Domain })
    os = (Observe { Get-CimInstance Win32_OperatingSystem | Select-Object Caption,BuildNumber })
    rdp = (Observe {
        $ts = Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server'
        $tcp = Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp'
        @{ enabled = ($ts.fDenyTSConnections -eq 0); nla_required = ($tcp.UserAuthentication -eq 1); port = $tcp.PortNumber }
    })
    listeners = (Observe {
        @(Get-NetTCPConnection -State Listen | Where-Object LocalPort -in $Port,3389,22 |
            Select-Object LocalAddress,LocalPort,OwningProcess)
    })
    scheduled_tasks = (Observe {
        @(Get-ScheduledTask | Where-Object TaskName -like 'ChiefOfStaff-*' | ForEach-Object {
            @{ name = $_.TaskName; state = [string]$_.State; logon_type = [string]$_.Principal.LogonType;
               actions = @($_.Actions | Select-Object Execute,WorkingDirectory) }
        })
    })
    connectivity_software = (Observe {
        @(Get-ChildItem 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
            'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall' |
            Get-ItemProperty | Where-Object DisplayName -match 'GlobalProtect|Zscaler|AnyConnect|Cisco Secure Client|Ivanti|Citrix' |
            Select-Object DisplayName,DisplayVersion)
    })
    ssh_server = (Observe { @(Get-Service | Where-Object Name -eq 'sshd' | Select-Object Name,Status) })
    limitations = @(
        'Installation and registry state do not establish AMD authorization or off-network reachability.',
        'Active server data directory, writer exclusivity, backup integrity and session/power policies remain unverified.',
        'Scheduled-task working directory identifies deployment configuration; it does not prove the running source revision.',
        'Use -Target <desktop-private-name> on the laptop over the approved private connection for a TCP-only check.'
    )
}
$report | ConvertTo-Json -Depth 7
