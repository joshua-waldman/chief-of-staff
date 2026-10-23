import hashlib
import json
import subprocess

import pytest

from cos import config, engineering, tools, local_checks
from cos.db import Store
from cos.engine import Engine, session
from cos.local_checks import test_command as is_test_command
from cos.policy import classify
from tests.test_workflows import Scripted, perform


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / 'app'
    root.mkdir()
    monkeypatch.setattr(tools, 'ROOT', root)
    monkeypatch.setattr(engineering, 'ROOT', root)
    monkeypatch.setattr(tools, 'DATA', root / 'data')
    monkeypatch.setattr(config, 'DATA', root / 'data')
    monkeypatch.setattr(tools, 'settings', lambda: {'projects': {}})
    store = Store(tmp_path / 'jobs.db')
    engines = []
    def build(*answers, org='Self-Improvement'):
        job = store.create_job('Improve local workflow', org)
        engine = Engine(store, Scripted(*answers))
        engines.append(engine)
        return engine, job
    yield root, store, build
    for engine in engines:
        engine.stop()


@pytest.mark.parametrize('command', [
    ['python', '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_reviews.py', '-v'],
    [r'C:\missing\.venv\Scripts\python.exe', '-m', 'unittest', 'discover', '-s', 'staged/tests', '-p', 'test_bridge.py', '-v'],
    ['python', '-B', '-m', 'pytest', 'tests/test_reviews.py', '-q'],
])
def test_only_self_improvement_local_tests_are_autonomous(command):
    assert is_test_command(command)
    assert not classify('run_command', {'command': command}, 'Self-Improvement').approval
    assert classify('run_command', {'command': command}, 'Engineering').approval


@pytest.mark.parametrize('command', [
    ['python', '-c', 'print(1)'], ['cmd', '/c', 'anything'],
    ['python', '-m', 'pytest', '--override-ini=addopts=anything'],
    ['python', '-m', 'pytest', '../other'],
    ['python', '-m', 'unittest', 'discover', '-s', r'C:\private'],
    ['python', '-m', 'pytest', 'tests/../private'],
])
def test_arbitrary_host_execution_keeps_review(command):
    assert not is_test_command(command)
    assert classify('run_command', {'command': command}, 'Self-Improvement').approval


def test_external_gates_remain():
    for action in ['send_email', 'send_teams', 'confluence_write', 'publish', 'merge_code', 'deploy_code', 'delete_file', 'mcp_call']:
        assert classify(action, {}, 'Self-Improvement').approval


def test_real_test_run_uses_running_python_and_app_cwd(env):
    root, store, build = env
    (root / 'tests').mkdir()
    (root / 'tests' / 'test_sample.py').write_text('import unittest\nclass Check(unittest.TestCase):\n    def test_ok(self): self.assertEqual(2 + 2, 4)\n', encoding='utf-8')
    engine, job = build(
        perform('run_command', {'command': [r'C:\missing\python.exe', '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-v'], 'cwd': str(root)}),
        perform('finish', {'summary': 'Local checks passed.'}))
    engine.run(job['id'])
    assert store.job(job['id'])['status'] == 'completed'
    receipt = store.one("SELECT * FROM approvals WHERE tool='run_command'")
    result = json.loads(receipt['result'])
    assert receipt['status'] == 'executed' and result['exit_code'] == 0
    assert 'Ran 1 test' in result['stderr']


@pytest.mark.parametrize('failure', ['missing', 'outside'])
def test_approved_prelaunch_failure_queues_correction_without_verification(env, failure):
    root, store, build = env
    args = {'command': [str(root / 'missing.exe')], 'cwd': str(root if failure == 'missing' else root.parent / 'outside')}
    engine, job = build(perform('run_command', args), org='Engineering')
    engine.run(job['id'])
    receipt = store.one('SELECT * FROM approvals')
    engine.decide(receipt['id'], True)
    assert store.job(job['id'])['status'] == 'queued'
    assert store.one('SELECT status FROM approvals')['status'] == 'failed_not_executed'
    assert json.loads(session(store.job(job['id']))['messages'][-1]['content'])['not_executed']
    assert not session(store.job(job['id'])).get('waiting_call')


def test_local_directory_rejection_recovers_in_same_run(env):
    root, store, build = env
    engine, job = build(
        perform('run_command', {'command': ['python', '-m', 'pytest', 'tests'], 'cwd': str(root.parent / 'outside')}),
        perform('finish', {'summary': 'Recovered from path validation.'}))
    engine.run(job['id'])
    assert store.job(job['id'])['status'] == 'completed'
    assert store.one('SELECT status FROM approvals')['status'] == 'failed_not_executed'


@pytest.mark.parametrize('error', ['[WinError 2] The system cannot find the file specified', 'Path is outside the job workspace and registered projects.'])
def test_legacy_false_uncertainty_is_resumed_once(env, error):
    root, store, build = env
    engine, job = build(perform('run_command', {'command': ['python', '-m', 'pytest', 'tests']}), org='Engineering')
    engine.run(job['id'])
    store.write("UPDATE approvals SET status='uncertain',result=?", (error,))
    store.update(job['id'], status='blocked')
    engine.recover_local_actions()
    engine.recover_local_actions()
    assert store.job(job['id'])['status'] == 'queued'
    assert store.one('SELECT status FROM approvals')['status'] == 'failed_not_executed'
    messages = session(store.job(job['id']))['messages']
    assert sum(m['role'] == 'tool' for m in messages) == 1


@pytest.mark.parametrize('error', ['timed out after 180 seconds', 'The application stopped during a mutation.'])
def test_unknown_command_outcome_stays_blocked(env, error):
    root, store, build = env
    engine, job = build(perform('run_command', {'command': ['python', '-m', 'pytest', 'tests']}), org='Engineering')
    engine.run(job['id'])
    store.write("UPDATE approvals SET status='uncertain',result=?", (error,))
    store.update(job['id'], status='blocked')
    engine.recover_local_actions()
    assert store.job(job['id'])['status'] == 'blocked'


def test_pending_self_improvement_check_is_retired_and_resumed(env):
    root, store, build = env
    engine, job = build(perform('run_command', {'command': ['python', '-m', 'pytest', 'tests']}), org='Engineering')
    engine.run(job['id'])
    store.write("UPDATE jobs SET org='Self-Improvement' WHERE id=?", (job['id'],))
    engine.recover_local_actions()
    assert store.job(job['id'])['status'] == 'queued'
    assert store.one('SELECT status FROM approvals')['status'] == 'superseded'


def test_staged_app_patch_runs_without_approval_with_backup(env, monkeypatch):
    root, store, build = env
    monkeypatch.setattr(engineering, 'git', lambda *args: 'codex/local-checks')
    engine, job = build()
    target = root / 'module.py'
    target.write_text('old\n', encoding='utf-8')
    source = tools.workspace(job['id']) / 'module.py'
    source.write_text('new\n', encoding='utf-8')
    engine.provider = Scripted(
        perform('apply_changes', {'changes': [{'source': 'module.py', 'target': str(target), 'expected_sha256': hashlib.sha256(target.read_bytes()).hexdigest()}]}),
        perform('finish', {'summary': 'Applied.'}))
    engine.run(job['id'])
    assert store.job(job['id'])['status'] == 'completed'
    assert target.read_text() == 'new\n'
    assert next((tools.workspace(job['id']) / 'backups').glob('*/*')).read_text() == 'old\n'


def test_self_improvement_requires_feature_branch(env, monkeypatch):
    root, store, build = env
    engine, job = build()
    monkeypatch.setattr(engineering, 'git', lambda *args: 'main')
    with pytest.raises(ValueError, match='feature branch'):
        engineering.verify_branch(job)


def test_approved_timeout_remains_uncertain(env, monkeypatch):
    root, store, build = env
    engine, job = build(perform('run_command', {'command': ['python', '-c', 'anything']}), org='Engineering')
    engine.run(job['id'])
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('python', 180)
    monkeypatch.setattr(tools, 'run_host', timeout)
    engine.decide(store.one('SELECT id FROM approvals')['id'], True)
    assert store.job(job['id'])['status'] == 'blocked'
    assert store.one('SELECT status FROM approvals')['status'] == 'uncertain'


def test_unittest_zero_tests_is_not_success(env):
    root, store, build = env
    (root / 'tests').mkdir()
    (root / 'tests' / 'test_sample.py').write_text('def test_pytest_only(): assert True\n', encoding='utf-8')
    engine, job = build()
    result = engine.tools.execute(job, 'run_command', {'command': ['python', '-m', 'unittest', 'discover', '-s', 'tests'], 'cwd': str(root)})
    assert result['exit_code'] != 0 and result['no_tests']


def test_self_improvement_branch_creation_uses_app_root(env, monkeypatch):
    root, store, build = env
    engine, job = build(perform('create_feature_branch', {'name': 'Local checks'}), perform('finish', {'summary': 'Created branch.'}))
    calls = []
    def git(path, *args):
        calls.append((path, args))
        return 'codex/local-checks'
    monkeypatch.setattr(engineering, 'git', git)
    engine.run(job['id'])
    assert store.job(job['id'])['status'] == 'completed'
    assert calls[0] == (root, ('switch', '-c', 'codex/local-checks'))


def test_stale_source_hash_cannot_overwrite_app(env, monkeypatch):
    root, store, build = env
    monkeypatch.setattr(engineering, 'git', lambda *args: 'codex/local-checks')
    engine, job = build()
    target = root / 'module.py'
    target.write_text('concurrent change', encoding='utf-8')
    (tools.workspace(job['id']) / 'module.py').write_text('new', encoding='utf-8')
    with pytest.raises(ValueError, match='Target changed'):
        tools.prepare_changes(job, {'changes': [{'source': 'module.py', 'target': str(target), 'expected_sha256': 'stale'}]})
    assert target.read_text() == 'concurrent change'


def test_self_improvement_patch_cannot_touch_registered_external_project(env, tmp_path, monkeypatch):
    root, store, build = env
    external = tmp_path / 'external'
    external.mkdir()
    target = external / 'module.py'
    target.write_text('original', encoding='utf-8')
    monkeypatch.setattr(tools, 'settings', lambda: {'projects': {'external': str(external)}})
    engine, job = build()
    (tools.workspace(job['id']) / 'module.py').write_text('new', encoding='utf-8')
    with pytest.raises(ValueError, match='application source'):
        tools.prepare_changes(job, {'changes': [{'source': 'module.py', 'target': str(target), 'expected_sha256': hashlib.sha256(target.read_bytes()).hexdigest()}]})
    assert target.read_text() == 'original'


@pytest.mark.parametrize('action', ['apply_changes', 'create_feature_branch'])
def test_pending_self_improvement_local_action_resumes_but_uncertain_does_not(env, action):
    root, store, build = env
    engine, job = build(perform('run_command', {'command': ['python', '-m', 'pytest', 'tests']}), org='Engineering')
    engine.run(job['id'])
    store.write("UPDATE jobs SET org='Self-Improvement' WHERE id=?", (job['id'],))
    store.write("UPDATE approvals SET tool=?,status='uncertain',result='Path is outside the job workspace and registered projects.'", (action,))
    store.update(job['id'], status='blocked')
    engine.recover_local_actions()
    assert store.job(job['id'])['status'] == 'blocked'
    store.write("UPDATE approvals SET status='pending'")
    store.update(job['id'], status='awaiting_approval')
    engine.recover_local_actions()
    assert store.job(job['id'])['status'] == 'queued'
    assert store.one('SELECT status FROM approvals')['status'] == 'superseded'


def test_post_launch_io_error_is_not_classified_as_unstarted(monkeypatch):
    class StartedProcess:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def communicate(self, **kwargs): raise OSError('pipe failed after launch')
        def kill(self): pass
    monkeypatch.setattr(local_checks.subprocess, 'Popen', lambda *args, **kwargs: StartedProcess())
    with pytest.raises(OSError, match='after launch'):
        local_checks.run_host(['python'], cwd='.', env={})
