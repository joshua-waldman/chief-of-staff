"""Check Git blobs without reading or printing private runtime data.

This is a guardrail, not a guarantee: review every diff before committing.
"""
import argparse
import re
import subprocess
from pathlib import PurePosixPath

ROOT_FILES = {
    '.gitignore', '.gitattributes', 'README.md', 'requirements.txt', 'run.py', 'manage.py',
    'pytest.ini', 'acceptance.json', 'Start-ChiefOfStaff.cmd',
    'Start-ChiefOfStaff.ps1', 'Setup-Atlassian.ps1', 'config.example.json',
    'scripts/check_repository.py', 'scripts/install_hooks.py',
    '.githooks/pre-commit', '.githooks/pre-push', '.github/workflows/checks.yml',
}
PATTERNS = {
    'credential assignment': re.compile(r"(?i)(?:api[_-]?key|api[_-]?token|client[_-]?secret|password)\s*[=:]\s*['\"]?[a-z0-9_+/=-]{24,}"),
    'absolute user profile path': re.compile(r'(?:(?i:[a-z]:[\\/]+Users[\\/]+)|/(?:Users|home)/)(?!<|\{)[a-zA-Z0-9_.-]+'),
    'non-example personal email': re.compile(r'(?i)\b[a-z0-9_%+-]+(?:\.[a-z0-9_%+-]+)+@(?!(?:example\.(?:com|org|net))\b)[a-z0-9.-]+\.[a-z]{2,}'),
    'private key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'access token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9_-]{30,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,})\b'),
}


def git(*args):
    return subprocess.check_output(['git', *args])


def allowed(name):
    p = PurePosixPath(name)
    return name in ROOT_FILES or (
        len(p.parts) == 2 and (
            (p.parts[0] == 'cos' and p.suffix == '.py') or
            (p.parts[0] == 'web' and p.suffix in {'.html', '.js', '.css'}) or
            (p.parts[0] == 'tests' and p.name.startswith('test_') and p.suffix in {'.py', '.cjs'})
        )
    )


def check(staged=False):
    names = git('ls-files', '-z').decode('utf-8').split('\0') if staged else git('ls-tree', '-r', '--name-only', '-z', 'HEAD').decode('utf-8').split('\0')
    errors = []
    for name in filter(None, names):
        if not allowed(name):
            errors.append(f'{name}: outside the reviewed source allowlist')
            continue
        blob = git('show', ':' + name) if staged else git('show', 'HEAD:' + name)
        try:
            content = blob.decode('utf-8')
        except UnicodeDecodeError:
            errors.append(f'{name}: binary files are not allowed')
            continue
        for label, pattern in PATTERNS.items():
            if pattern.search(content):
                errors.append(f'{name}: possible {label}')
    if errors:
        print('\n'.join(errors))
        raise SystemExit('Repository privacy check failed; matched values were not printed.')
    print('Repository privacy check passed.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--staged', action='store_true')
    check(parser.parse_args().staged)
