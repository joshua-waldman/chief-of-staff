"""Recognize local test commands without granting arbitrary host execution."""
from pathlib import PureWindowsPath
import subprocess


class ActionNotExecuted(ValueError):
    """Validation or process creation failed before the action could start."""


def run_host(command, *, cwd, env, timeout=180):
    # Only a failed Popen is proof that no child process started. Errors while
    # waiting for an existing process must retain their uncertain outcome.
    try:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace')
    except OSError as error:
        raise ActionNotExecuted(str(error)) from error
    with process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        except BaseException:
            process.kill()
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def test_command(command):
    if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
        return False
    name = PureWindowsPath(command[0]).name.lower()
    if name not in {'python', 'python.exe', 'python3', 'python3.exe'}:
        return False
    args = command[1:]
    while args and args[0] in {'-B', '-u'}:
        args = args[1:]
    if args[:2] == ['-m', 'pytest']:
        values = args[2:]
        return all(x in {'-q', '-v', '-vv', '-x', '--disable-warnings'} or
                   (not x.startswith('-') and test_path(x)) for x in values)
    if args[:3] != ['-m', 'unittest', 'discover']:
        return False
    values = iter(args[3:])
    for value in values:
        if value in {'-v', '-q'}:
            continue
        if value not in {'-s', '-t', '-p'}:
            return False
        item = next(values, None)
        if not item or (value == '-p' and ('/' in item or '\\' in item or not item.endswith('.py'))) or (value != '-p' and not test_path(item)):
            return False
    return True


def test_path(value):
    path = PureWindowsPath(value.split('::', 1)[0])
    return not path.is_absolute() and not path.drive and '..' not in path.parts and bool(path.parts) and path.parts[0] in {'tests', 'staged', '.'}


def autonomous_check(org, action, args):
    return org == 'Self-Improvement' and action == 'run_command' and test_command(args.get('command'))
