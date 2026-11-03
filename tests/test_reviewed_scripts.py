import json
import sys
from pathlib import Path

import pytest

from cos import automatic_review, reviewed_scripts, tools
from cos.engine import Engine, session
from cos.local_checks import ActionNotExecuted
from tests.test_automatic_review import env, Reviewer, decide
from tests.test_workflows import Scripted, perform


def script_job(env, source, helpers=None):
    store, project, options = env
    job = store.create_job('Create and validate the requested workbook in this job workspace.', 'Engineering')
    root = tools.workspace(job['id'])
    (root / 'build.py').write_text(source, encoding='utf-8')
    for name, content in (helpers or {}).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
    args = {'command': ['python', 'build.py'], 'cwd': '.'}
    return store, job, root, args


def approve(store, job, args):
    reviewer = Reviewer()
    decision = decide(store, job, args, reviewer)
    assert not decision.approval, decision.reason
    assert args.get('_workspace_script_review')
    return reviewer


def test_openpyxl_builder_is_reviewed_and_round_trips_with_registered_output(env):
    source = """from pathlib import Path
from openpyxl import Workbook, load_workbook
out = Path(__file__).with_name('review.xlsx')
book = Workbook()
book.active['A1'] = 'Review artifact'
book.create_sheet('Legend')
book.save(out)
check = load_workbook(out, read_only=True)
assert check.active['A1'].value == 'Review artifact'
assert len(check.sheetnames) == 2
check.close()
print('VALIDATED')
"""
    store, job, root, args = script_job(env, source)
    reviewer = approve(store, job, args)
    payload = json.loads(reviewer.calls[0][0][1]['content'])
    assert payload['reviewed_script']['modules']['__main__']['source'] == (root / 'build.py').read_bytes().decode('utf-8-sig')
    assert payload['reviewed_script']['packages']['openpyxl']['version']
    result = tools.Tools(store).execute(job, 'run_command', args)
    assert result['exit_code'] == 0, result['stderr']
    assert 'VALIDATED' in result['stdout']
    assert result['execution'] == 'reviewed-workspace-script'
    assert (root / 'review.xlsx').is_file()
    assert [item['name'] for item in result['outputs']] == ['review.xlsx']
    with pytest.raises(ActionNotExecuted, match='expired'):
        tools.Tools(store).execute(job, 'run_command', args)


@pytest.mark.parametrize('changed', ['build.py', 'helper.py'])
def test_source_and_dependency_changes_require_fresh_review_before_launch(env, monkeypatch, changed):
    store, job, root, args = script_job(env, 'import helper\nprint(helper.VALUE)\n', {'helper.py': 'VALUE = 1\n'})
    approve(store, job, args)
    (root / changed).write_text('print("changed")\n', encoding='utf-8')
    monkeypatch.setattr(tools, 'run_host', lambda *a, **k: pytest.fail('Changed code cannot launch'))
    with pytest.raises(ActionNotExecuted, match='changed'):
        tools.Tools(store).execute(job, 'run_command', args)


def test_execution_uses_captured_entry_and_imports_after_launch_race(env, monkeypatch):
    store, job, root, args = script_job(env,
        "from pathlib import Path\nfrom helpers import value\nPath('result.txt').write_text(value.TEXT)\n",
        {'helpers/__init__.py': '', 'helpers/value.py': "TEXT = 'reviewed'\n"})
    reviewer = approve(store, job, args)
    modules = json.loads(reviewer.calls[0][0][1]['content'])['reviewed_script']['modules']
    assert {'helpers', 'helpers.value'} <= set(modules)
    original = tools.run_host
    def race(command, **kwargs):
        (root / 'build.py').write_text("raise RuntimeError('unreviewed entry')\n", encoding='utf-8')
        (root / 'helpers/value.py').write_text("TEXT = 'unreviewed dependency'\n", encoding='utf-8')
        return original(command, **kwargs)
    monkeypatch.setattr(tools, 'run_host', race)
    result = tools.Tools(store).execute(job, 'run_command', args)
    assert result['exit_code'] == 0, result['stderr']
    assert (root / 'result.txt').read_text() == 'reviewed'


@pytest.mark.parametrize('source,helpers', [
    ('import subprocess\n', {}), ('import socket\n', {}),
    ('import sys as harmless\n', {'sys.py': ''}),
    ('import builtins as harmless\n', {'builtins.py': ''}),
    ('import __main__ as harmless\n', {'__main__.py': ''}),
    ('from pathlib import Path\nprint(Path.__class__)\n', {}),
    ('from pathlib import sys\nprint(sys.modules)\n', {}),
    ('import pathlib\npathlib.os.system(\"never\")\n', {}),
    ('eval("1+1")\n', {}),
    ('import pathlib\n', {'pathlib.py': ''}),
    ('from unknown_package import thing\n', {}),
])
def test_opaque_imports_introspection_and_library_shadowing_remain_manual(env, source, helpers):
    store, job, root, args = script_job(env, source, helpers)
    reviewer = Reviewer()
    assert decide(store, job, args, reviewer).approval
    assert not reviewer.calls
    assert '_workspace_script_review' not in args


@pytest.mark.parametrize('operation', ['read_text()', "write_text('bad')", "rename('local.txt')", "symlink_to('local.txt')"])
def test_runtime_guard_blocks_outside_paths_even_if_reviewer_allows(env, operation):
    outside = env[1].parent / 'private.txt'
    outside.write_text('private', encoding='utf-8')
    source = 'from pathlib import Path\nPath(' + repr(str(outside)) + ').' + operation + '\n'
    store, job, root, args = script_job(env, source)
    approve(store, job, args)
    result = tools.Tools(store).execute(job, 'run_command', args)
    assert result['exit_code'] != 0
    assert 'PermissionError' in result['stderr']
    assert outside.read_text() == 'private'
    assert not (root / 'local.txt').exists()


@pytest.mark.parametrize('name', ['.env', '.ENV', 'token.json', 'credentials.json', 'config.local.json', 'context/notes.txt', 'CONTEXT/notes.txt'])
def test_runtime_guard_blocks_reserved_workspace_reads(env, name):
    store, job, root, args = script_job(env, 'from pathlib import Path\nprint(Path(' + repr(name) + ').read_text())\n')
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('private', encoding='utf-8')
    approve(store, job, args)
    result = tools.Tools(store).execute(job, 'run_command', args)
    assert result['exit_code'] != 0
    assert 'private' not in result['stdout']


def test_runtime_guard_blocks_executable_outputs(env):
    store, job, root, args = script_job(env, "from pathlib import Path\nPath('new.py').write_text('print(1)')\n")
    approve(store, job, args)
    result = tools.Tools(store).execute(job, 'run_command', args)
    assert result['exit_code'] != 0
    assert not (root / 'new.py').exists()


def test_tampered_bundle_never_executes(env, monkeypatch):
    store, job, root, args = script_job(env, "from pathlib import Path\nPath('result.txt').write_text('ok')\n")
    approve(store, job, args)
    original = tools.run_host
    def tamper(command, **kwargs):
        bundle = Path(command[-3])
        bundle.write_text('{}', encoding='utf-8')
        return original(command, **kwargs)
    monkeypatch.setattr(tools, 'run_host', tamper)
    result = tools.Tools(store).execute(job, 'run_command', args)
    assert result['exit_code'] != 0
    assert 'bundle changed' in result['stderr']
    assert not (root / 'result.txt').exists()


def test_forged_binding_is_not_authority_and_command_changes_invalidate_binding(env):
    store, job, root, args = script_job(env, 'print("ok")\n')
    args['_workspace_script_review'] = 'invented'
    with pytest.raises(ActionNotExecuted):
        tools.Tools(store).execute(job, 'run_command', args)
    args.pop('_workspace_script_review')
    approve(store, job, args)
    args['command'] = ['python', 'other.py']
    with pytest.raises(ActionNotExecuted):
        tools.Tools(store).execute(job, 'run_command', args)


def test_script_and_source_limits_are_explicit(env):
    store, job, root, args = script_job(env, '#' * 40001)
    assert decide(store, job, args, Reviewer()).approval
    assert reviewed_scripts.candidate(['python', 'build.py', '--anything'])
    assert not reviewed_scripts.candidate(['python', '-c', 'print(1)'])
    args['cwd'] = str(env[1])
    assert decide(store, job, args, Reviewer()).approval


def test_research_allows_guarded_local_artifacts_but_source_readers_do_not(env):
    store, job, root, args = script_job(env, 'print("local artifact")\n')
    assert not decide(store, job, args, Reviewer(), state={'research_only': True}).approval
    for restriction in ('source_only', 'historical_import'):
        assert decide(store, job, args, Reviewer(), state={restriction: True}).approval


@pytest.mark.parametrize('reason,expected', [
    ('Approval is required for unrestricted host commands.', 'queued'),
    ('Automatic review: unrelated action.', 'awaiting_approval'),
    ('Automatic review could not establish authorization.', 'awaiting_approval'),
])
def test_pending_script_is_reconsidered_once_without_bypassing_review(env, reason, expected):
    store, job, root, args = script_job(env, 'print("local artifact")\n')
    engine = Engine(store, Scripted(perform('run_command', args)))
    try:
        engine.run(job['id'])
        receipt = store.one("SELECT * FROM approvals WHERE tool='run_command'")
        store.write('UPDATE approvals SET reason=? WHERE id=?', (reason, receipt['id']))
        engine.recover_local_actions()
        assert store.job(job['id'])['status'] == expected
        if expected == 'queued':
            assert store.one('SELECT status FROM approvals WHERE id=?', (receipt['id'],))['status'] == 'superseded'
            result = json.loads(session(store.job(job['id']))['messages'][-1]['content'])
            assert result['not_executed']
            assert 'review' in result['instruction']
            engine.recover_local_actions()
            assert store.job(job['id'])['status'] == 'queued'
    finally:
        engine.stop()


def test_uncertain_script_is_never_requeued_as_unstarted(env):
    store, job, root, args = script_job(env, 'print("local artifact")\n')
    engine = Engine(store, Scripted(perform('run_command', args)))
    try:
        engine.run(job['id'])
        store.write("UPDATE approvals SET status='uncertain',result='Timed out after launch'")
        store.update(job['id'], status='blocked')
        engine.recover_local_actions()
        assert store.job(job['id'])['status'] == 'blocked'
        assert store.one('SELECT status FROM approvals')['status'] == 'uncertain'
    finally:
        engine.stop()



def test_installed_library_change_invalidates_review(env, monkeypatch):
    store, job, root, args = script_job(env, 'from openpyxl import Workbook\nprint(Workbook)\n')
    approve(store, job, args)
    monkeypatch.setattr(reviewed_scripts.importlib.metadata, 'version', lambda name: 'changed')
    monkeypatch.setattr(tools, 'run_host', lambda *a, **k: pytest.fail('Changed library cannot launch'))
    with pytest.raises(ActionNotExecuted, match='changed'):
        tools.Tools(store).execute(job, 'run_command', args)


def test_runtime_guard_blocks_permission_changes(env):
    store, job, root, args = script_job(env, "from pathlib import Path\nPath('result.txt').chmod(511)\n")
    (root / 'result.txt').write_text('unchanged', encoding='utf-8')
    approve(store, job, args)
    result = tools.Tools(store).execute(job, 'run_command', args)
    assert result['exit_code'] != 0
    assert 'PermissionError' in result['stderr']
    assert (root / 'result.txt').read_text() == 'unchanged'


def test_local_dependency_cannot_replace_installed_library_module(env):
    store, job, root, args = script_job(env, 'import openpyxl\nimport et_xmlfile\n', {'et_xmlfile.py': 'print("local fake")\n'})
    reviewer = Reviewer()
    assert decide(store, job, args, reviewer).approval
    assert not reviewer.calls


@pytest.mark.parametrize('code', ["import os\nos.system('never-run')", "import socket\nsocket.socket()", "import subprocess\nsubprocess.run(['never-run'])"])
def test_runner_runtime_defense_blocks_network_and_processes_if_static_check_is_missed(env, code):
    store, job, root, args = script_job(env, 'print("safe")\n')
    snapshot = reviewed_scripts.capture(job, args)
    snapshot['modules']['__main__']['source'] = code
    private = root.parent / 'runtime-test'
    private.mkdir()
    bundle = private / 'bundle.json'
    bundle.write_bytes(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode())
    result = tools.run_host([sys.executable, '-I', '-B', str(tools.ROOT / 'cos/script_runner.py'), str(bundle), reviewed_scripts.digest(snapshot), str(private / 'receipt.json')], cwd=root, env={})
    assert result.returncode != 0
    assert 'PermissionError' in result.stderr


def test_engine_removes_worker_supplied_binding_and_issues_its_own(env, monkeypatch):
    store, job, root, args = script_job(env, 'print("artifact")\n')
    args['_workspace_script_review'] = 'worker-supplied'
    engine = Engine(store, Scripted(perform('run_command', args), perform('finish', {'summary': 'Done.'})), review_provider=Reviewer())
    calls = []
    def execute(job, action, args):
        calls.append(args)
        assert args['_workspace_script_review'] != 'worker-supplied'
        assert store.state('reviewed-script:' + job['id'] + ':' + args['_workspace_script_review'])
        return {'exit_code': 0}
    monkeypatch.setattr(engine.tools, 'execute', execute)
    try:
        engine.run(job['id'])
        assert store.job(job['id'])['status'] == 'completed'
        assert len(calls) == 1
    finally:
        engine.stop()



def test_prelaunch_bundle_failure_is_definitely_not_executed(env, monkeypatch):
    store, job, root, args = script_job(env, 'print("artifact")\n')
    approve(store, job, args)
    def fail(*args, **kwargs):
        raise OSError('Cannot create execution bundle')
    monkeypatch.setattr(reviewed_scripts.tempfile, 'mkdtemp', fail)
    monkeypatch.setattr(tools, 'run_host', lambda *a, **k: pytest.fail('Bundle preparation failed before launch'))
    with pytest.raises(ActionNotExecuted, match='Cannot prepare'):
        tools.Tools(store).execute(job, 'run_command', args)



def test_builder_arguments_are_reviewed_bound_and_passed_as_data(env):
    source = "from pathlib import Path\nimport argparse\np = argparse.ArgumentParser()\np.add_argument('--out')\nargs = p.parse_args()\nPath(args.out).write_text('artifact')\n"
    store, job, root, args = script_job(env, source)
    args['command'] = ['python', '-B', 'build.py', '--out', 'result.txt']
    reviewer = approve(store, job, args)
    payload = json.loads(reviewer.calls[0][0][1]['content'])
    assert payload['reviewed_script']['argv'][1:] == ['--out', 'result.txt']
    original = list(args['command'])
    args['command'][-1] = 'different.txt'
    with pytest.raises(ActionNotExecuted):
        tools.Tools(store).execute(job, 'run_command', args)
    args['command'] = original
    result = tools.Tools(store).execute(job, 'run_command', args)
    assert result['exit_code'] == 0, result['stderr']
    assert (root / 'result.txt').read_text() == 'artifact'



def test_private_local_helpers_are_included_in_review_and_can_run(env):
    store, job, root, args = script_job(env, 'import _helpers\nprint(_helpers.label())\n', {'_helpers.py': 'def _value():\n    return "reviewed"\ndef label():\n    return _value()\n'})
    reviewer = approve(store, job, args)
    payload = json.loads(reviewer.calls[0][0][1]['content'])
    assert '_helpers' in payload['reviewed_script']['modules']
    result = tools.Tools(store).execute(job, 'run_command', args)
    assert result['exit_code'] == 0, result['stderr']
    assert 'reviewed' in result['stdout']


def test_recovery_archives_cannot_be_script_entry_points(env):
    store, job, root, args = script_job(env, 'print("artifact")\n', {'context/archive.py': 'print("archived")\n'})
    args['command'] = ['python', 'context/archive.py']
    reviewer = Reviewer()
    assert decide(store, job, args, reviewer).approval
    assert not reviewer.calls
