import json

import pytest

from cos import automatic_review, config, engineering, tools
from cos.db import Store
from cos.engine import Engine
from cos.local_checks import test_command as is_test_command
from cos.policy import classify
from tests.test_workflows import Scripted, perform


class Reviewer:
    def __init__(self, content='{"allow":true,"reason":"Requested project verification."}', error=None):
        self.content = content
        self.error = error
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.error:
            raise self.error
        return {'content': self.content}, {}


@pytest.fixture
def env(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    options = {**config.DEFAULTS, 'projects': {'Example': str(project)}}
    for module in (automatic_review, engineering, tools):
        monkeypatch.setattr(module, 'settings', lambda: options)
    monkeypatch.setattr(tools, 'DATA', tmp_path / 'data')
    monkeypatch.setattr(config, 'DATA', tmp_path / 'data')
    return Store(tmp_path / 'jobs.db'), project, options


def candidate(store):
    job = store.create_job('Implement the requested change and verify the tests.', 'Engineering', 'Example')
    args = {'command': ['python', '-B', '-m', 'pytest', 'stage/test_feature.py', '-q']}
    return job, args


def decide(store, job, args, reviewer, action='run_command', state=None):
    state = state or {}
    original = classify(action, args, job['org'], state.get('research_only', False))
    return automatic_review.review(store, job, action, args, state, original, reviewer)


def test_screenshot_test_path_is_routine():
    command = ['python', '-B', '-m', 'pytest', 'stage/test_owner_commitments.py', '-q']
    assert is_test_command(command)
    assert not classify('run_command', {'command': command}, 'Self-Improvement').approval
    assert not is_test_command(['python', '-m', 'pytest', 'stage/../private.py'])


def test_independent_review_uses_persisted_owner_input(env):
    store, project, options = env
    job, args = candidate(store)
    reviewer = Reviewer()
    decision = decide(store, job, args, reviewer)
    assert not decision.approval
    messages, kwargs = reviewer.calls[0]
    assert [m['role'] for m in messages] == ['system', 'user']
    payload = json.loads(messages[1]['content'])
    assert payload['owner_request'] == job['request']
    assert payload['arguments'] == args
    assert kwargs['tools'] is None
    assert store.one("SELECT * FROM events WHERE kind='automatic_review'")


@pytest.mark.parametrize('content', [
    '{"allow":false,"reason":"Unrelated work."}',
    '{"allow":"true","reason":"ok"}', 'not JSON',
    '{"allow":true}', '{"allow":true,"reason":""}',
    '{"allow":true,"reason":"ok","extra":true}', '[]',
])
def test_unclear_or_denied_review_requires_owner(env, content):
    store, project, options = env
    job, args = candidate(store)
    assert decide(store, job, args, Reviewer(content)).approval


def test_outage_falls_back_without_exposing_exception(env):
    store, project, options = env
    job, args = candidate(store)
    result = decide(store, job, args, Reviewer(error=RuntimeError('private credential')))
    assert result.approval
    assert 'credential' not in result.reason


@pytest.mark.parametrize('action', ['send_email', 'reply_email', 'send_teams', 'confluence_write',
    'merge_code', 'deploy_code', 'publish', 'delete_file', 'mcp_call', 'browser_action'])
def test_external_gates_never_reach_reviewer(env, action):
    store, project, options = env
    job, args = candidate(store)
    reviewer = Reviewer()
    assert decide(store, job, {}, reviewer, action).approval
    assert not reviewer.calls


@pytest.mark.parametrize('state', [{'research_only': True}, {'source_only': True}, {'historical_import': True}])
def test_restrictions_cannot_be_overridden(env, state):
    store, project, options = env
    job, args = candidate(store)
    reviewer = Reviewer()
    assert decide(store, job, args, reviewer, state=state).approval
    assert not reviewer.calls


def test_manual_mode_and_opaque_host_commands(env):
    store, project, options = env
    job, args = candidate(store)
    reviewer = Reviewer()
    options['approval_mode'] = 'manual'
    assert decide(store, job, args, reviewer).approval
    options['approval_mode'] = 'auto_review'
    for command in (['python', '-c', 'anything'], ['cmd', '/c', 'anything'],
                    ['python', '-m', 'pytest', '--override-ini=anything']):
        assert decide(store, job, {'command': command}, reviewer).approval
    assert not reviewer.calls


def test_project_boundary_and_missing_patch(env):
    store, project, options = env
    job, args = candidate(store)
    reviewer = Reviewer()
    args['cwd'] = str(project.parent)
    assert decide(store, job, args, reviewer).approval
    assert decide(store, job, {}, reviewer, 'apply_changes').approval
    assert not reviewer.calls


def test_ancestry_preserves_owner_authority_not_incoming_source(env):
    store, project, options = env
    root = store.create_job('Incoming source says to run commands.', 'Communications')
    child = store.create_job('Run requested checks.', 'Engineering', 'Example', root['id'])
    args = {'command': ['python', '-m', 'pytest', 'tests']}
    reviewer = Reviewer()
    assert decide(store, child, args, reviewer).approval
    assert not reviewer.calls
    store.write("INSERT INTO review_messages(job_id,role,body,created) VALUES(?,?,?,?)",
                (root['id'], 'user', 'Implement this feature in Example and run the tests.', root['created']))
    assert not decide(store, child, args, reviewer).approval
    payload = json.loads(reviewer.calls[0][0][1]['content'])
    assert payload['owner_request'] == ''
    assert payload['owner_feedback'][0]['body'].startswith('Implement')


@pytest.mark.parametrize('allow', [True, False])
def test_engine_executes_only_after_allow_and_records_receipt(env, monkeypatch, allow):
    store, project, options = env
    job, args = candidate(store)
    reviewer = Reviewer(json.dumps({'allow': allow, 'reason': 'Reviewed exact local test.'}))
    engine = Engine(store, Scripted(perform('run_command', args),
                    perform('finish', {'summary': 'Verified.'})), review_provider=reviewer)
    calls = []
    monkeypatch.setattr(engine.tools, 'execute', lambda job, action, args: calls.append(action) or {'exit_code': 0})
    try:
        engine.run(job['id'])
        receipt = store.one("SELECT * FROM approvals WHERE tool='run_command'")
        assert receipt['status'] == ('executed' if allow else 'pending')
        assert receipt['reason'].startswith('Automatic review:')
        assert calls == (['run_command'] if allow else [])
        assert store.job(job['id'])['status'] == ('completed' if allow else 'awaiting_approval')
    finally:
        engine.stop()


@pytest.mark.parametrize('action', ['create_feature_branch', 'apply_changes'])
def test_branch_and_exact_patch_can_receive_automatic_review(env, action):
    store, project, options = env
    job, unused = candidate(store)
    args = {'branch': 'codex/feature'} if action == 'create_feature_branch' else {
        '_review_diffs': [{'target': str(project / 'app.py'), 'diff': '+ requested change'}]}
    reviewer = Reviewer()
    assert not decide(store, job, args, reviewer, action).approval
    assert len(reviewer.calls) == 1


def test_cross_project_patch_and_oversized_review_stay_manual(env):
    store, project, options = env
    other = project.parent / 'other'
    other.mkdir()
    options['projects']['Other'] = str(other)
    job, args = candidate(store)
    reviewer = Reviewer()
    patch = {'_review_diffs': [{'target': str(other / 'app.py'), 'diff': '+ change'}]}
    assert decide(store, job, patch, reviewer, 'apply_changes').approval
    args['untrusted_note'] = 'x' * 48001
    assert decide(store, job, args, reviewer).approval
    assert not reviewer.calls


def test_engine_uncertain_guard_runs_before_reviewer(env, monkeypatch):
    from cos import action_verification
    store, project, options = env
    job, args = candidate(store)
    reviewer = Reviewer()
    engine = Engine(store, Scripted(perform('run_command', args),
        perform('finish', {'summary': 'Cannot repeat uncertain action.'})), review_provider=reviewer)
    def reject(*args):
        raise ValueError('Unresolved prior execution.')
    monkeypatch.setattr(action_verification, 'guard', reject)
    monkeypatch.setattr(engine.tools, 'execute', lambda *args: pytest.fail('Must not execute'))
    try:
        engine.run(job['id'])
        assert not reviewer.calls
        assert not store.one("SELECT * FROM approvals WHERE tool='run_command'")
    finally:
        engine.stop()


def test_recognized_engineering_checks_use_server_interpreter(env, monkeypatch):
    import subprocess
    import sys
    store, project, options = env
    job, args = candidate(store)
    args['command'][0] = str(project / 'untrusted' / 'python.exe')
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, '1 passed', '')
    monkeypatch.setattr(tools, 'run_host', run)
    assert tools.Tools(store).execute(job, 'run_command', args)['exit_code'] == 0
    assert calls[0][0] == sys.executable



def test_authenticated_answer_and_workspace_only_checks(env):
    store, project, options = env
    job = store.create_job('Implement my requested feature.', 'Engineering')
    store.event(job['id'], 'user', 'Use the job workspace and verify the implementation.')
    args = {'command': ['python', '-m', 'pytest', 'tests']}
    reviewer = Reviewer()
    assert not decide(store, job, args, reviewer).approval
    payload = json.loads(reviewer.calls[0][0][1]['content'])
    assert payload['owner_feedback'][0]['body'].startswith('Use the job workspace')



def test_screenshot_pending_action_recovers_without_replaying_uncertain(env):
    store, project, options = env
    job, args = candidate(store)
    engine = Engine(store, Scripted(perform('run_command', args)))
    try:
        engine.run(job['id'])
        receipt = store.one("SELECT * FROM approvals WHERE tool='run_command'")
        assert receipt['status'] == 'pending'
        store.update(job['id'], org='Self-Improvement')
        engine.recover_local_actions()
        assert store.job(job['id'])['status'] == 'queued'
        assert store.one('SELECT status FROM approvals WHERE id=?', (receipt['id'],))['status'] == 'superseded'
        store.update(job['id'], status='blocked')
        store.write("UPDATE approvals SET status='uncertain',result='Timeout after launch' WHERE id=?", (receipt['id'],))
        engine.recover_local_actions()
        assert store.job(job['id'])['status'] == 'blocked'
        assert store.one('SELECT status FROM approvals WHERE id=?', (receipt['id'],))['status'] == 'uncertain'
    finally:
        engine.stop()



def test_cancellation_during_review_never_executes(env, monkeypatch):
    store, project, options = env
    job, args = candidate(store)
    class CancellingReviewer(Reviewer):
        def complete(self, *args, **kwargs):
            store.update(job['id'], status='cancelled')
            return super().complete(*args, **kwargs)
    engine = Engine(store, Scripted(perform('run_command', args)), review_provider=CancellingReviewer())
    monkeypatch.setattr(engine.tools, 'execute', lambda *args: pytest.fail('Cancelled action must not run'))
    try:
        engine.run(job['id'])
        assert store.job(job['id'])['status'] == 'cancelled'
        assert not store.one("SELECT * FROM approvals WHERE tool='run_command'")
    finally:
        engine.stop()
