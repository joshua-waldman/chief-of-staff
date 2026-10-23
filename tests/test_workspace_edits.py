import hashlib
from pathlib import Path

import pytest

from cos import config, tools
from cos.db import Store
from cos.engine import Engine
from cos.policy import classify
from cos.tools import Tools, workspace
from tests.test_workflows import Scripted, perform


@pytest.fixture
def edit_env(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, 'DATA', tmp_path / 'data')
    monkeypatch.setattr(config, 'DATA', tmp_path / 'data')
    store = Store(tmp_path / 'test.db')
    job = store.create_job('Stage a narrow source edit', 'Engineering')
    path = workspace(job['id']) / 'module.py'
    path.write_bytes(b'def choose():\r\n    return "old"\r\n')
    args = {'path': 'module.py', 'expected_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'old_text': '    return "old"\n', 'new_text': '    return "new"\n'}
    return store, job, path, args


def test_engine_executes_small_edit_and_keeps_external_approval(edit_env):
    store, job, path, args = edit_env
    original = path.read_bytes()
    engine = Engine(store, Scripted(perform('edit_file', args),
        perform('run_command', {'command': ['python', '-m', 'pytest']})))
    try:
        engine.run(job['id'])
        assert path.read_bytes() == b'def choose():\r\n    return "new"\r\n'
        assert store.job(job['id'])['status'] == 'awaiting_approval'
        receipt = store.one("SELECT result FROM approvals WHERE job_id=? AND tool='edit_file' AND status='executed'", (job['id'],))
        import json
        result = json.loads(receipt['result'])
        assert result['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert Path(result['backup']).read_bytes() == original
        assert store.one("SELECT status FROM approvals WHERE tool='run_command'")['status'] == 'pending'
        assert not classify('edit_file', {}).approval
    finally:
        engine.stop()


@pytest.mark.parametrize('change', ['stale', 'ambiguous', 'absent', 'empty', 'same'])
def test_invalid_edit_never_changes_file(edit_env, change):
    store, job, path, args = edit_env
    if change == 'stale':
        path.write_bytes(path.read_bytes() + b'# concurrent change\r\n')
    elif change == 'ambiguous':
        path.write_bytes(path.read_bytes() * 2)
        args['expected_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    elif change == 'absent':
        args['old_text'] = 'not present'
    elif change == 'empty':
        args['old_text'] = ''
    else:
        args['new_text'] = args['old_text']
    original = path.read_bytes()
    with pytest.raises(ValueError):
        Tools(store).execute(job, 'edit_file', args)
    assert path.read_bytes() == original
    assert not list(path.parent.glob('*.bak'))


def test_edit_cannot_change_project_or_other_job(edit_env, tmp_path):
    store, job, path, args = edit_env
    outside = tmp_path / 'live.py'
    outside.write_text('original', encoding='utf-8')
    other = store.create_job('Other private job', 'Engineering')
    private = workspace(other['id']) / 'private.py'
    private.write_text('original', encoding='utf-8')
    for target in (outside, private):
        with pytest.raises(ValueError):
            Tools(store).execute(job, 'edit_file', {**args, 'path': str(target),
                'expected_sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
                'old_text': 'original', 'new_text': 'modified'})
        assert target.read_text(encoding='utf-8') == 'original'
