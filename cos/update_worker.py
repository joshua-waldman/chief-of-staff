"""Detached Windows updater. Stdlib only: copied outside the installation before use."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request


def run(args, **kwargs):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=600, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), **kwargs)
    if result.returncode:
        raise RuntimeError('Update command failed: ' + Path(str(args[0])).name)
    return result.stdout.decode('utf-8').strip()


def task(root, action):
    # Use the existing per-user task only, and verify it belongs to this installation.
    script = "$ErrorActionPreference='Stop'; $n='ChiefOfStaff-'+[Security.Principal.WindowsIdentity]::GetCurrent().User.Value; $t=Get-ScheduledTask -TaskName $n; if ($t.Actions.Count -ne 1 -or $t.Actions[0].WorkingDirectory -ne $env:COS_UPDATE_ROOT) { throw 'Task belongs to another installation' }; "
    if action == 'inspect':
        script += "if ((Get-ScheduledTask -TaskName $n).State -ne 'Running') { throw 'COS must be running through its scheduled task' }"
    else:
        script += ('Stop' if action == 'stop' else 'Start') + '-ScheduledTask -TaskName $n'
    run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
        env={**os.environ, 'COS_UPDATE_ROOT': str(root)})


def safe_path(root, name):
    path = root / name
    if not path.resolve().is_relative_to(root.resolve()) or any(p.is_symlink() for p in (path, *path.parents)):
        raise RuntimeError('Update path leaves the installation or contains a link.')
    return path


def backup_database(database, target):
    if database.exists():
        with sqlite3.connect(database) as source, sqlite3.connect(target) as dest:
            source.backup(dest)


def health(port, sha, trial, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen('http://127.0.0.1:' + str(port) + '/healthz', timeout=2) as response:
                status = json.load(response)
                if status.get('sha') == sha and status.get('trial') is trial:
                    return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError('Updated application did not pass its startup health check.')


def wait_stopped(port):
    import socket
    for _ in range(30):
        with socket.socket() as sock:
            sock.settimeout(1)
            if sock.connect_ex(('127.0.0.1', port)) != 0:
                return
        time.sleep(1)
    raise RuntimeError('COS did not stop; release files cannot be safely replaced.')


def install(plan, work):
    root, stage = Path(plan['root']), Path(plan['stage'])
    current, release = plan['current'], plan['release']
    database = Path(plan['database'])
    data = Path(plan.get('data', str(database.parent)))
    marker, trial = root / '.cos-release.json', root / '.cos-update-trial'
    previous_marker = marker.read_bytes() if marker.exists() else None
    previous_trial = trial.exists()
    if previous_trial:
        raise RuntimeError('A previous update requires recovery; inspect the update logs.')
    task(root, 'inspect')
    baseline = plan['baseline']
    for name, digest in baseline.items():
        if hashlib.sha256(safe_path(root, name).read_bytes()).hexdigest() != digest:
            raise RuntimeError('Source files changed after update preparation.')
    def git(*args):
        return run(['git', '-C', str(root), *args])
    branch = None
    if current['kind'] == 'git':
        if git('status', '--porcelain') or git('rev-parse', 'HEAD') != current['sha']:
            raise RuntimeError('Git checkout changed after update preparation.')
        branch = git('branch', '--show-current')
        if branch not in {'', 'main', 'master'}:
            raise RuntimeError('Feature branches cannot be updated automatically.')
        git('fetch', 'origin', 'tag', release['tag'])
        if git('rev-parse', release['tag'] + '^{commit}') != release['sha']:
            raise RuntimeError('The release tag changed after the update check.')
        # Never let a release track private runtime paths or symbolic links.
        for line in git('ls-tree', '-r', release['sha']).splitlines():
            metadata, name = line.split('\t', 1)
            destination = safe_path(root, name).resolve()
            if metadata.split()[0] not in {'100644', '100755'} or name.split('/')[0].casefold() in {
                    'data', '.venv', '.git', 'config.local.json', '.cos-release.json', '.cos-update-trial'} or (
                    destination == database.resolve() or destination.is_relative_to(data.resolve()) and data.resolve() != root.resolve()):
                raise RuntimeError('Release contains a protected runtime path or link.')
    names = sorted(set(baseline) | {p.relative_to(stage).as_posix() for p in stage.rglob('*') if p.is_file()})
    backup = work / 'backup'
    backup.mkdir()
    # Validate every destination before stopping or changing the app.
    for name in names:
        path = safe_path(root, name)
        if path.resolve() == database.resolve() or (
                data.resolve() != root.resolve() and path.resolve().is_relative_to(data.resolve())):
            raise RuntimeError('Release source overlaps the configured runtime data directory.')
        if path.exists():
            target = backup / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    stopped = changed = dependencies = activated = False
    try:
        task(root, 'stop')
        stopped = True
        # Task Scheduler stops the supervisor process tree. Wait for the listener to close.
        wait_stopped(plan['port'])
        backup_database(database, work / 'database.sqlite3')
        trial.write_text('Update validation: background work is disabled.', encoding='utf-8')
        changed = True
        if current['kind'] == 'git':
            git('checkout', '--detach', release['sha'])
        else:
            for name in names:
                target, source = safe_path(root, name), stage / name
                if source.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                elif target.is_file():
                    target.unlink()
        dependencies = True
        run([plan['python'], '-m', 'pip', 'install', '-r', str(root / 'requirements.txt')])
        marker.write_text(json.dumps({'tag': release['tag'], 'sha': release['sha']}), encoding='utf-8')
        task(root, 'start')
        health(plan['port'], release['sha'], True)
        task(root, 'stop')
        wait_stopped(plan['port'])
        trial.unlink()
        # Past this boundary work can resume. Do not restore an older database after actions run.
        activated = True
        task(root, 'start')
        health(plan['port'], release['sha'], False)
        return 'complete'
    except Exception:
        if activated:
            raise RuntimeError('Release installed, but restart needs attention. Background work may have resumed; database was not rolled back.') from None
        if stopped:
            task(root, 'stop')
            wait_stopped(plan['port'])
        if changed:
            if current['kind'] == 'git':
                git('checkout', branch or '--detach', *([] if branch else [current['sha']]))
            else:
                for name in names:
                    target, source = safe_path(root, name), backup / name
                    if source.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, target)
                    elif target.is_file():
                        target.unlink()
            if previous_marker is None:
                marker.unlink(missing_ok=True)
            else:
                marker.write_bytes(previous_marker)
            if (work / 'database.sqlite3').exists():
                # All new-version writers were stopped before restoring the consistent snapshot.
                for suffix in ('-wal', '-shm'):
                    Path(str(database) + suffix).unlink(missing_ok=True)
                shutil.copy2(work / 'database.sqlite3', database)
            if dependencies:
                run([plan['python'], '-m', 'pip', 'install', '-r', str(root / 'requirements.txt')])
            trial.unlink(missing_ok=True)
        if stopped:
            task(root, 'start')
        raise


def main(plan_path):
    work = plan_path.parent
    try:
        plan = json.loads(plan_path.read_text(encoding='utf-8'))
        result = {'state': install(plan, work), 'tag': plan['release']['tag']}
    except Exception as error:
        result = {'state': 'error', 'error': str(error), 'recovery': 'Backups and update logs are in this directory.'}
    temporary = work / 'result.tmp'
    temporary.write_text(json.dumps(result), encoding='utf-8')
    temporary.replace(work / 'result.json')
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main(Path(sys.argv[1]))
