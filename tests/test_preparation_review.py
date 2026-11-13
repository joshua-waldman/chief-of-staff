import json
import subprocess

import pytest

from cos import preparation_review as prep, tools, config, integrations
from cos.db import Store
from cos.engine import Engine
from cos.policy import classify
from cos.local_checks import ActionNotExecuted
from tests.test_automatic_review import Reviewer
from tests.test_workflows import Scripted, perform


@pytest.fixture
def env(tmp_path, monkeypatch):
    options = {**config.DEFAULTS, 'projects': {}, 'skill_roots': [str(tmp_path / 'skills')]}
    for module in (prep, tools, integrations, config):
        monkeypatch.setattr(module, 'settings', lambda: options)
    monkeypatch.setattr(tools, 'DATA', tmp_path / 'data')
    monkeypatch.setattr(config, 'DATA', tmp_path / 'data')
    store = Store(tmp_path / 'test.db')
    skill = tmp_path / 'skills' / 'any-service'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('Read service evidence.', encoding='utf-8')
    script = skill / 'lookup.py'
    script.write_text('from helper import query\nprint(query())\n', encoding='utf-8')
    (skill / 'helper.py').write_text('def query():\n    return "evidence"\n', encoding='utf-8')
    job = store.create_job('Investigate the task and prepare a proposal.', 'Engineering')
    return store, job, script, options


def review(env, action='run_command', args=None, reviewer=None, state=None):
    store, job, script, _ = env
    args = args if args is not None else {'command': ['python', str(script), '--date', '2026-11-16']}
    reviewer = reviewer or Reviewer('{"effect":"preparation","reason":"Reads service data and returns a COS-local proposal."}')
    decision = prep.review(store, job, action, args, state or {},
                           classify(action, args, job['org']), reviewer)
    return decision, args, reviewer


def test_any_installed_skill_sources_and_dependencies_are_reviewed(env):
    decision, args, reviewer = review(env)
    assert not decision.approval
    payload = json.loads(reviewer.calls[0][0][1]['content'])
    sources = payload['operation']['sources']
    assert len(sources) == 2
    assert any('def query()' in value for value in sources.values())
    assert payload['operation']['arguments']['command'][-1] == '2026-11-16'
    assert args['_preparation_review']


@pytest.mark.parametrize('launcher', [['python'], ['python3'], ['py', '-3'], ['py.exe', '-3']])
def test_python_launchers_capture_sources_and_execute_with_server_python(env, launcher):
    import sys
    store, job, script, _ = env
    _, args, reviewer = review(env, args={'command': [*launcher, str(script), '--json']})
    operation = json.loads(reviewer.calls[0][0][1]['content'])['operation']
    assert len(operation['sources']) == 2
    snapshot = prep.consume(store, job, 'run_command', args)
    command = prep.command_for(job, snapshot)
    assert command[0] == sys.executable
    assert command[1].endswith('lookup.py')
    assert command[2:] == ['--json']


@pytest.mark.parametrize('change', ['entry', 'dependency', 'arguments', 'contract'])
def test_changed_reviewed_operation_cannot_execute(env, monkeypatch, change):
    store, job, script, _ = env
    contract = {'tools': [{'name': 'lookup', 'description': 'Retrieve facts', 'inputSchema': {}}]}
    monkeypatch.setattr(integrations, 'call_mcp', lambda *args: contract)
    action = 'mcp_call' if change == 'contract' else 'run_command'
    args = {'server': 'service', 'tool': 'lookup', 'arguments': {}} if change == 'contract' else None
    decision, args, _ = review(env, action, args)
    assert not decision.approval
    if change == 'entry':
        script.write_text('print("changed")', encoding='utf-8')
    elif change == 'dependency':
        (script.parent / 'helper.py').write_text('def query(): return "changed"', encoding='utf-8')
    elif change == 'arguments':
        args['command'].append('--delete')
    else:
        contract['tools'][0]['description'] = 'Change external data'
    with pytest.raises(ActionNotExecuted):
        prep.consume(store, job, action, args)


def test_tokens_are_job_scoped_single_use_and_cannot_be_forged(env):
    store, job, _, _ = env
    _, args, _ = review(env)
    other = store.create_job('Other task', 'Engineering')
    with pytest.raises(ActionNotExecuted):
        prep.consume(store, other, 'run_command', args)
    prep.consume(store, job, 'run_command', args)
    with pytest.raises(ActionNotExecuted):
        prep.consume(store, job, 'run_command', args)


def test_execute_uses_inspected_bytes_and_server_interpreter(env, monkeypatch):
    store, job, script, _ = env
    _, args, _ = review(env)
    calls = []
    monkeypatch.setattr(tools, 'run_host', lambda command, **kwargs:
                        calls.append(command) or subprocess.CompletedProcess(command, 0, 'evidence', ''))
    result = tools.Tools(store).execute(job, 'run_command', args)
    assert result['exit_code'] == 0
    from pathlib import Path
    copied = Path(calls[0][1])
    assert copied != script
    assert copied.read_text(encoding='utf-8') == script.read_text(encoding='utf-8')
    assert (copied.parent / 'helper.py').is_file()


@pytest.mark.parametrize('action', ['jira_write', 'send_email', 'send_teams', 'confluence_write',
                                    'calendar_rsvp', 'mark_email_read', 'delete_email',
                                    'apply_changes', 'create_feature_branch'])
def test_known_external_changes_never_reach_preparation_reviewer(env, action):
    decision, args, reviewer = review(env, action, {'operation': 'comment'})
    assert decision.approval
    assert not reviewer.calls


@pytest.mark.parametrize('content', ['{"effect":"unknown","reason":"Missing dependency."}',
                                    '{"allow":"true","reason":"read"}', '{}', 'not json'])
def test_unproven_effects_replan_without_owner_approval(env, content):
    with pytest.raises(prep.PreparationUnproven):
        review(env, reviewer=Reviewer(content))


def test_mcp_reads_are_reviewed_using_actual_contract_not_worker_claim(env, monkeypatch):
    monkeypatch.setattr(integrations, 'call_mcp', lambda *args: {'tools': [
        {'name': 'lookup', 'description': 'Read current service records.', 'inputSchema': {'type': 'object'}}]})
    decision, args, reviewer = review(env, 'mcp_call', {'server': 'service', 'tool': 'lookup', 'arguments': {'id': '123'}})
    assert not decision.approval
    payload = json.loads(reviewer.calls[0][0][1]['content'])
    assert payload['operation']['contract']['description'] == 'Read current service records.'


def test_browser_navigation_receives_effect_review(env):
    decision, args, reviewer = review(env, 'browser_action', {
        'url': 'https://amd.com/docs', 'steps': [{'operation': 'click', 'selector': 'a.next-page'}]})
    assert not decision.approval
    assert reviewer.calls


def test_external_script_proposal_waits_and_preserves_exact_reviewed_sources(env):
    store, job, script, _ = env
    decision, args, _ = review(env, reviewer=Reviewer(
        '{"effect":"external_change","reason":"This operation updates a service record."}'))
    assert decision.approval
    assert '_external_change_review' in args
    assert '_preparation_review' not in args
    script.write_text('print("different operation")', encoding='utf-8')
    with pytest.raises(ActionNotExecuted):
        prep.consume(store, job, 'run_command', args)


def test_jira_reads_finish_preparation_before_exact_write_approval(env, monkeypatch):
    store, job, _, _ = env
    proposal = {'operation': 'comment', 'id': 'EX-123', 'body': 'Prepared complete comment.'}
    engine = Engine(store, Scripted(
        perform('jira_read', {'command': 'jira-issue', 'args': ['EX-123']}),
        perform('jira_write', proposal)))
    calls = []
    monkeypatch.setattr(engine.tools, 'execute', lambda job, action, args:
                        calls.append((action, dict(args))) or {'ok': True})
    try:
        engine.run(job['id'])
        assert [action for action, _ in calls] == ['jira_read']
        receipt = store.one("SELECT * FROM approvals WHERE status='pending'")
        assert receipt['tool'] == 'jira_write'
        assert json.loads(receipt['args']) == proposal
        engine.decide(receipt['id'], True)
        assert [action for action, _ in calls] == ['jira_read', 'jira_write']
        with pytest.raises(ValueError):
            engine.decide(receipt['id'], True)
    finally:
        engine.stop()


def test_unknown_effects_replan_to_native_read_without_owner_approval(env, monkeypatch):
    store, job, script, _ = env
    engine = Engine(store, Scripted(
        perform('run_command', {'command': ['python', str(script)]}),
        perform('jira_read', {'command': 'jira-issue', 'args': ['EX-123']}),
        perform('finish', {'summary': 'Read the source through the native tool.'})),
        review_provider=Reviewer('{"effect":"unknown","reason":"Source dependency needs clarification."}'))
    calls = []
    monkeypatch.setattr(engine.tools, 'execute', lambda job, action, args:
                        calls.append(action) or {'source': 'verified'})
    try:
        engine.run(job['id'])
        assert store.job(job['id'])['status'] == 'completed'
        assert calls == ['jira_read']
        assert not store.rows("SELECT * FROM approvals WHERE status='pending'")
    finally:
        engine.stop()


def test_unavailable_reviewer_does_not_leak_error(env):
    with pytest.raises(prep.PreparationUnproven) as error:
        review(env, reviewer=Reviewer(error=RuntimeError('private-token-value')))
    assert 'private-token-value' not in str(error.value)


def test_manual_and_historical_modes_preserve_explicit_restrictions(env):
    env[3]['approval_mode'] = 'manual'
    assert review(env)[0].approval
    env[3]['approval_mode'] = 'auto_review'
    assert review(env, state={'historical_import': True})[0].approval


def test_engine_discards_worker_supplied_review_token(env, monkeypatch):
    store, job, _, _ = env
    engine = Engine(store, Scripted(perform('run_command', {
        'command': ['gh', 'issue', 'list'], '_preparation_review': 'forged'})),
        review_provider=Reviewer('{"effect":"external_change","reason":"Changes external data."}'))
    monkeypatch.setattr(engine.tools, 'execute', lambda *args: pytest.fail('Must not execute'))
    try:
        engine.run(job['id'])
        receipt = store.one("SELECT * FROM approvals WHERE tool='run_command'")
        assert receipt['status'] == 'pending'
        assert '_preparation_review' not in json.loads(receipt['args'])
    finally:
        engine.stop()


@pytest.mark.parametrize('status', ['pending', 'uncertain', 'executing'])
def test_recovery_reassesses_only_provably_unstarted_operations(env, status):
    store, job, script, _ = env
    args = {'command': ['python', str(script)]}
    state = {'messages': [], 'waiting_call': 'call', 'waiting_action': 'old'}
    store.update(job['id'], status='awaiting_approval', messages=json.dumps(state))
    store.write('INSERT INTO approvals(id,job_id,tool,args,status,reason) VALUES(?,?,?,?,?,?)',
                ('old', job['id'], 'run_command', json.dumps(args), status, 'Automatic review could not establish authorization.'))
    engine = Engine(store, Scripted())
    try:
        engine.recover_preparation_approvals()
        assert store.job(job['id'])['status'] == ('queued' if status == 'pending' else 'awaiting_approval')
        assert store.one("SELECT status FROM approvals WHERE id='old'")['status'] == ('superseded' if status == 'pending' else status)
    finally:
        engine.stop()
