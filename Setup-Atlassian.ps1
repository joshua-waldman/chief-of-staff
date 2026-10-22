param([string]$Cli, [string]$Python)
$ErrorActionPreference = 'Stop'
Write-Host 'Chief of Staff: Atlassian setup'
Write-Host 'Credentials stay in your local user profile, outside OneDrive and chat.'
$defaults = @{}
$configPath = Join-Path $env:USERPROFILE '.atlassian/config.json'
if (Test-Path -LiteralPath $configPath) { $defaults = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json }
$site = Read-Host "Atlassian Cloud site [$($defaults.site)]"
if (-not $site) { $site = $defaults.site }
$email = Read-Host "Atlassian account email [$($defaults.email)]"
if (-not $email) { $email = $defaults.email }
$secureToken = Read-Host 'Atlassian API token' -AsSecureString
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
try {
    $plainToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    $plainToken | & $Python $Cli setup --site $site --email $email --token-stdin
    if ($LASTEXITCODE -ne 0) { Write-Host 'Setup did not pass verification. Review the output above.' }
} finally {
    $plainToken = $null
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    $secureToken.Dispose()
}
Read-Host 'Press Enter to close'
