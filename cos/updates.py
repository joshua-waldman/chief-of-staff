"""Stable GitHub releases, staged downloads and an explicitly requested update."""
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile

import httpx

from .config import ROOT, DATA
from .processes import background_options
from . import update_gate

REPOSITORY = 'joshua-waldman/chief-of-staff'
INTERVAL = 12 * 60 * 60
ROOT_FILES = {'README.md', 'requirements.txt', 'run.py', 'manage.py', 'pytest.ini',
              'acceptance.json', 'config.example.json', 'Start-ChiefOfStaff.cmd',
              'Start-ChiefOfStaff.ps1', 'Setup-ChiefOfStaff.cmd', 'Setup-ChiefOfStaff.ps1',
              'Setup-Atlassian.ps1', '.gitignore', '.gitattributes'}


def version(tag):
    match = re.fullmatch(r'v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', str(tag))
    return tuple(map(int, match.groups())) if match else None


def git(*args, root=None):
    root = root or ROOT
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                            timeout=120, **background_options())
    if result.returncode:
        raise ValueError('Git could not complete the update check. Check repository access and local changes.')
    return result.stdout.decode('utf-8').strip()


def installed():
    marker = ROOT / '.cos-release.json'
    saved = json.loads(marker.read_text(encoding='utf-8')) if marker.exists() else {}
    if (ROOT / '.git').exists():
        sha = git('rev-parse', 'HEAD')
        tags = git('tag', '--points-at', 'HEAD').splitlines()
        stable = sorted((t for t in tags if version(t)), key=version)
        return {'tag': stable[-1] if stable else None, 'sha': sha, 'kind': 'git'}
    return {'tag': saved.get('tag'), 'sha': saved.get('sha'), 'kind': 'zip'}


def headers():
    from .github import routes, _token
    login = next((login for owner, login in routes().items()
                  if owner.casefold() == REPOSITORY.split('/')[0].casefold()), None)
    result = {'Accept': 'application/vnd.github+json'}
    if login:
        result['Authorization'] = 'Bearer ' + _token(login)
    return result


def get_json(endpoint):
    try:
        response = httpx.get('https://api.github.com/repos/' + REPOSITORY + endpoint,
                             headers=headers(), timeout=30)
        if response.status_code == 404:
            raise ValueError('No published stable release was found, or repository access is missing. For a private repository, configure its GitHub account in Connections.')
        if response.status_code != 200:
            raise ValueError('GitHub update check failed. Check repository access or try again later.')
        return response.json()
    except httpx.HTTPError:
        raise ValueError('Could not reach GitHub. COS will try again later.') from None


def source_path(name):
    """An update may replace application source only, never private runtime files."""
    p = PurePosixPath(name)
    if '\\' in name or ':' in name or p.is_absolute() or '..' in p.parts:
        return False
    return name in ROOT_FILES or (
        len(p.parts) == 2 and (
            p.parts[0] == 'cos' and p.suffix == '.py' or
            p.parts[0] == 'web' and p.suffix in {'.js', '.html', '.css'} or
            p.parts[0] == 'scripts' and p.suffix in {'.py', '.ps1'}))


def stage_archive(content, target):
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        entries = archive.infolist()
        if sum(i.file_size for i in entries) > 100 * 1024 * 1024:
            raise ValueError('Release archive is too large.')
        names = set()
        for item in entries:
            parts = item.filename.split('/', 1)
            if len(parts) != 2 or item.is_dir():
                continue
            name = parts[1]
            if not source_path(name):
                continue
            if name.casefold() in names or (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Release archive contains duplicate paths or links.')
            names.add(name.casefold())
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.read(item))
    for required in ('run.py', 'requirements.txt', 'cos/server.py', 'Start-ChiefOfStaff.ps1'):
        if not (target / required).is_file():
            raise ValueError('Release archive is missing required application files.')


def tree_digest():
    paths = [ROOT / name for name in ROOT_FILES]
    for directory in ('cos', 'web', 'scripts'):
        paths.extend((ROOT / directory).glob('*'))
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in paths if p.is_file() and source_path(p.relative_to(ROOT).as_posix())}


class Updates:
    def __init__(self):
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.state = {'state': 'idle', 'release': None, 'error': '', 'checked_at': None}
        self.dismissed_until = 0
        self.work = None
        self.helper = None
        self.preferences = DATA / 'updates' / 'preferences.json'
        try:
            self.dismissed_until = float(json.loads(self.preferences.read_text(encoding='utf-8')).get('dismissed_until', 0))
        except (OSError, ValueError, TypeError):
            pass
        self.seen_result = None

    def snapshot(self):
        with self.lock:
            # Recover a detached helper's failure after a successful rollback/restart.
            results = sorted((DATA / 'updates').glob('*/result.json'))
            if self.work and (self.work / 'result.json').exists() and self.work / 'result.json' not in results:
                results.append(self.work / 'result.json')
            if results:
                if results[-1] != self.seen_result:
                    try:
                        result = json.loads(results[-1].read_text(encoding='utf-8'))
                        if result.get('state') == 'error':
                            self.state.update(state='error', attention=True, error=result['error'] + ' See the configured data directory under updates for recovery logs and backups.')
                            if results[-1].parent == self.work:
                                with update_gate.lock:
                                    update_gate.pending = False
                        self.seen_result = results[-1]
                    except (OSError, ValueError, KeyError):
                        pass
            if self.helper and self.helper.poll() is not None and self.state['state'] == 'installing':
                self.state.update(state='error', attention=True, error='Update helper exited before restarting COS. Check the local update logs.')
                with update_gate.lock:
                    update_gate.pending = False
            return {**self.state, 'reminded': time.time() < self.dismissed_until}

    def check(self):
        with self.lock:
            if self.state['state'] in {'checking', 'preparing', 'waiting', 'installing'}:
                return self.snapshot()
            self.state.update(state='checking', error='', attention=False)
        try:
            current = installed()
            release = get_json('/releases/latest')
            tag = release.get('tag_name')
            if not version(tag) or release.get('draft') or release.get('prerelease'):
                raise ValueError('Latest release must be a stable version such as v1.2.3.')
            # Resolve the tag to a commit, including annotated tags. Pin the download to it.
            commit = get_json('/commits/' + tag)['sha']
            if not re.fullmatch('[0-9a-f]{40}', commit):
                raise ValueError('GitHub returned an invalid release commit.')
            available = commit != current['sha'] and (
                not version(current['tag']) or version(tag) > version(current['tag']))
            with self.lock:
                self.state.update(state='available' if available else 'current', current=current,
                                  release={'tag': tag, 'sha': commit, 'notes': str(release.get('body') or '')[:30000]},
                                  checked_at=time.time(), error='')
        except Exception as error:
            with self.lock:
                self.state.update(state='error', error=str(error) if isinstance(error, ValueError) else 'Update check failed. Try again later.', checked_at=time.time())
        return self.snapshot()

    def start(self):
        def loop():
            while not self.stop_event.is_set():
                self.check()
                self.stop_event.wait(INTERVAL)
        threading.Thread(target=loop, daemon=True, name='cos-release-check').start()

    def remind(self):
        with self.lock:
            self.dismissed_until = time.time() + 86400
            self.preferences.parent.mkdir(parents=True, exist_ok=True)
            self.preferences.write_text(json.dumps({'dismissed_until': self.dismissed_until}), encoding='utf-8')
        return self.snapshot()

    def request(self, tag, sha, engine, port):
        with self.lock:
            release = self.state.get('release')
            if self.state['state'] != 'available' or not release or (tag, sha) != (release['tag'], release['sha']):
                raise ValueError('Check for updates again before installing this release.')
            if os.name != 'nt':
                raise ValueError('Automatic installation is supported on Windows hosts only.')
            current = installed()
            if current['kind'] == 'git':
                remote = git('remote', 'get-url', 'origin')
                if remote.removesuffix('.git') not in {'https://github.com/' + REPOSITORY, 'git@github.com:' + REPOSITORY}:
                    raise ValueError('Automatic updates require the official COS repository.')
                branch = git('branch', '--show-current')
                if branch not in {'', 'main', 'master'} or git('status', '--porcelain'):
                    raise ValueError('Automatic updates require a clean main/release checkout. Preserve local changes and leave the feature branch first.')
            self.state.update(state='preparing', error='')
            threading.Thread(target=self.prepare, args=(dict(release), current, engine, port), daemon=True, name='cos-update-prepare').start()
        return self.snapshot()

    def prepare(self, release, current, engine, port):
        try:
            work = DATA / 'updates' / (str(time.time_ns()))
            self.work = work
            stage = work / 'source'
            stage.mkdir(parents=True)
            baseline = tree_digest()
            try:
                with httpx.stream('GET', 'https://api.github.com/repos/' + REPOSITORY + '/zipball/' + release['sha'],
                                  headers=headers(), follow_redirects=True, timeout=90) as response:
                    response.raise_for_status()
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        content.extend(chunk)
                        if len(content) > 30 * 1024 * 1024:
                            raise ValueError('Release download is too large.')
                stage_archive(content, stage)
            except httpx.HTTPError:
                raise ValueError('Release download failed. Check GitHub access and retry.') from None
            # Copy the stdlib-only helper outside files that it will replace.
            shutil.copy2(ROOT / 'cos/update_worker.py', work / 'worker.py')
            plan = {'root': str(ROOT), 'data': str(DATA), 'database': str(engine.store.path),
                    'stage': str(stage), 'current': current, 'release': release,
                    'baseline': baseline, 'port': port, 'python': sys.executable}
            (work / 'plan.json').write_text(json.dumps(plan), encoding='utf-8')
            with update_gate.lock:
                update_gate.pending = True
            with self.lock:
                self.state['state'] = 'waiting'
            deadline = time.monotonic() + 1800
            while True:
                with update_gate.lock, engine.lock:
                    quiet = not update_gate.active and not engine.active and not engine.verifying
                if quiet and not engine.store.one("SELECT 1 FROM approvals WHERE status='executing' LIMIT 1"):
                    break
                if self.stop_event.wait(1) or time.monotonic() > deadline:
                    raise ValueError('Update deferred because work is still active. Try again when COS is idle.')
            if tree_digest() != baseline:
                raise ValueError('Application files changed while preparing the update. Check again after saving your work.')
            with self.lock:
                self.state['state'] = 'installing'
            # CREATE_BREAKAWAY_FROM_JOB allows the helper to survive stopping the scheduled task.
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_BREAKAWAY_FROM_JOB
            with (work / 'worker.log').open('wb') as log:
                self.helper = subprocess.Popen([sys.executable, str(work / 'worker.py'), str(work / 'plan.json')],
                                 cwd=str(work), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 creationflags=flags, close_fds=True)
        except Exception as error:
            with update_gate.lock:
                update_gate.pending = False
            with self.lock:
                self.state.update(state='error', attention=True, error=str(error) if isinstance(error, ValueError) else 'Update preparation failed; no application files were replaced. Check local update logs.')
