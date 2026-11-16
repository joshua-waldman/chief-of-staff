"""Exercise Windows setup without touching the owner's task, listeners or config."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys

import pytest


pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Windows setup and shortcuts')
ROOT = Path(__file__).resolve().parents[1]


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def ps(code, *, check=True):
    env = {**os.environ, 'PIP_NO_INDEX': '1', 'PIP_DISABLE_PIP_VERSION_CHECK': '1'}
    # Test locally authored scripts under a process-only RemoteSigned policy.
    # Domain policy still takes precedence; no machine/user policy is changed.
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'RemoteSigned', '-Command',
        "$ErrorActionPreference='Stop'; " + code], capture_output=True, text=True, env=env, timeout=90)
    if check:
        assert result.returncode == 0, result.stdout + result.stderr
    return result


@pytest.fixture
def setup_root(tmp_path):
    shutil.copy2(ROOT / 'Setup-ChiefOfStaff.ps1', tmp_path)
    (tmp_path / 'requirements.txt').write_text('', encoding='utf-8')
    (tmp_path / 'Start-ChiefOfStaff.ps1').write_text(
        "param([int]$Port)\n[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'started.txt'), [string]$Port)",
        encoding='utf-8')
    (tmp_path / 'scripts').mkdir()
    for name in ['New-CosLaptopKit.ps1', 'Install-CosShortcuts.ps1']:
        shutil.copy2(ROOT / 'scripts' / name, tmp_path / 'scripts' / name)
    return tmp_path


MOCK_HOST = """
function Get-ScheduledTask { @() }
function Get-NetTCPConnection { @() }
function Invoke-WebRequest { [pscustomobject]@{ StatusCode=200; Content='Chief of Staff' } }
"""


def test_new_install_is_paused_has_valid_utf8_config_and_preserves_existing_settings(setup_root):
    script = quote(setup_root / 'Setup-ChiefOfStaff.ps1')
    args = f' -DesktopAccess Skip -NonInteractive -NoBrowser -PythonExe {quote(sys.executable)}'
    ps(MOCK_HOST + f'& {script} -SelfEmail owner@example.com -Port 18765' + args)
    config_path = setup_root / 'config.local.json'
    config = json.loads(config_path.read_text(encoding='utf-8'))
    assert config == {'self_email': 'owner@example.com', 'projects': {},
                      'monitor_enabled': False, 'queue_paused': True}
    assert (setup_root / 'started.txt').read_text() == '18765'
    config['projects'] = {'Existing': 'C:/Projects/Existing'}
    config['queue_paused'] = False
    saved = json.dumps(config).encode('utf-8')
    config_path.write_bytes(saved)
    ps(MOCK_HOST + f'& {script}' + args)
    assert config_path.read_bytes() == saved


def test_setup_whatif_never_installs_starts_or_creates_a_kit(setup_root):
    kit = setup_root / 'laptop'
    ps(MOCK_HOST + f"& {quote(setup_root / 'Setup-ChiefOfStaff.ps1')} -DesktopAccess Enable "
       f'-RemoteComputer desktop.example.com -RemotePort 12389 -LaptopKitDirectory {quote(kit)} -WhatIf -NonInteractive')
    for name in ['.venv', 'config.local.json', 'started.txt', 'laptop']:
        assert not (setup_root / name).exists()


@pytest.mark.parametrize('mock,error', [
    ("function Get-ScheduledTask { [pscustomobject]@{ TaskName='ChiefOfStaff-'+[Security.Principal.WindowsIdentity]::GetCurrent().User.Value; State='Ready'; Actions=@([pscustomobject]@{ WorkingDirectory='C:\\OtherCOS' }) } }", 'another installation'),
    ("function Get-NetTCPConnection { [pscustomobject]@{ LocalPort=8765 } }", 'already in use'),
    ("function Get-ScheduledTask { [pscustomobject]@{ TaskName='ChiefOfStaff-'+[Security.Principal.WindowsIdentity]::GetCurrent().User.Value; State='Running'; Actions=@([pscustomobject]@{ WorkingDirectory=$testRoot }) } }", 'already running'),
])
def test_setup_refuses_task_takeover_running_host_and_port_conflicts(setup_root, mock, error):
    result = ps(MOCK_HOST + f'$testRoot={quote(setup_root)}; ' + mock +
        f"; & {quote(setup_root / 'Setup-ChiefOfStaff.ps1')} -SelfEmail owner@example.com -DesktopAccess Skip -NonInteractive -NoBrowser", check=False)
    assert result.returncode != 0
    assert error in result.stderr
    assert not (setup_root / 'config.local.json').exists()
    assert not (setup_root / '.venv').exists()
    assert not (setup_root / 'started.txt').exists()


def test_noninteractive_setup_requires_explicit_choices_before_mutation(setup_root):
    script = quote(setup_root / 'Setup-ChiefOfStaff.ps1')
    result = ps(MOCK_HOST + f'& {script} -NonInteractive', check=False)
    assert result.returncode != 0 and 'Specify -DesktopAccess' in result.stderr
    result = ps(MOCK_HOST + f'& {script} -NonInteractive -DesktopAccess Skip', check=False)
    assert result.returncode != 0 and '-SelfEmail is required' in result.stderr
    assert not (setup_root / 'config.local.json').exists()


def test_existing_owner_mismatch_is_not_overwritten(setup_root):
    config = setup_root / 'config.local.json'
    saved = b'{"self_email":"owner@example.com","projects":{}}'
    config.write_bytes(saved)
    result = ps(MOCK_HOST + f"& {quote(setup_root / 'Setup-ChiefOfStaff.ps1')} "
        '-NonInteractive -DesktopAccess Skip -SelfEmail someone@example.org', check=False)
    assert result.returncode != 0 and 'owner configuration differs' in result.stderr
    assert config.read_bytes() == saved
    assert not (setup_root / 'started.txt').exists()


def test_setup_refuses_detected_onedrive_installation_before_writing(setup_root):
    result = ps(MOCK_HOST + f'$env:OneDrive={quote(setup_root.parent)}; '
        f"& {quote(setup_root / 'Setup-ChiefOfStaff.ps1')} -NonInteractive -DesktopAccess Skip -SelfEmail owner@example.com", check=False)
    assert result.returncode != 0 and 'outside synchronized OneDrive' in result.stderr
    assert not (setup_root / 'config.local.json').exists()
    assert not (setup_root / '.venv').exists()


def test_laptop_kit_uses_selected_host_and_cannot_overwrite_existing_kit(tmp_path):
    kit = tmp_path / 'private laptop kit'
    script = quote(ROOT / 'scripts' / 'New-CosLaptopKit.ps1')
    ps(f'& {script} -Destination {quote(kit)} -RemoteComputer other-desktop.example.com -RemotePort 12389')
    assert {p.name for p in kit.iterdir()} == {
        'COS Remote Desktop.lnk', 'Install-CosShortcuts.ps1', 'READ-ME-FIRST.txt'}
    readme = (kit / 'READ-ME-FIRST.txt').read_text(encoding='utf-8')
    assert 'other-desktop.example.com:12389' in readme
    assert (kit / 'Install-CosShortcuts.ps1').read_bytes() == (ROOT / 'scripts' / 'Install-CosShortcuts.ps1').read_bytes()
    link = kit / 'COS Remote Desktop.lnk'
    cmd = ps(f'$s=New-Object -ComObject WScript.Shell; ($s.CreateShortcut({quote(link)})).Arguments')
    assert cmd.stdout.strip() == '/v:other-desktop.example.com:12389 /prompt'
    saved = {p.name: p.read_bytes() for p in kit.iterdir()}
    result = ps(f'& {script} -Destination {quote(kit)} -RemoteComputer wrong.example.com', check=False)
    assert result.returncode != 0 and 'already exists' in result.stderr
    assert {p.name: p.read_bytes() for p in kit.iterdir()} == saved


def test_laptop_kit_discovers_current_host_and_configured_rdp_port(tmp_path):
    kit = tmp_path / 'auto kit'
    ps("function Get-ItemProperty { [pscustomobject]@{ PortNumber=12389 } }; "
       f"& {quote(ROOT / 'scripts' / 'New-CosLaptopKit.ps1')} -Destination {quote(kit)}")
    link = kit / 'COS Remote Desktop.lnk'
    cmd = ps(f'$s=New-Object -ComObject WScript.Shell; ($s.CreateShortcut({quote(link)})).Arguments')
    args = cmd.stdout.strip().lower()
    assert args.startswith('/v:' + socket.gethostname().split('.')[0].lower())
    assert args.endswith(':12389 /prompt')
