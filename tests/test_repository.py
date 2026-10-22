"""Publishing guardrails must reject private files even when force-staged."""
import importlib.util
from pathlib import Path
import subprocess
import pytest

spec = importlib.util.spec_from_file_location('repository_check', Path(__file__).resolve().parents[1] / 'scripts/check_repository.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def test_source_allowlist():
    for path in ['cos/server.py', 'tests/test_repository.py', 'config.example.json']:
        assert checker.allowed(path)
    for path in ['config.local.json', 'data/history.sqlite3', 'cos/config.local.json', 'docs/preview.png', '.env', 'credentials.json', 'tests/private.txt']:
        assert not checker.allowed(path)


def test_patterns_detect_private_values():
    assert checker.PATTERNS['absolute user profile path'].search('/'.join(['C:', 'Users', 'private-person', 'Documents']))
    assert checker.PATTERNS['non-example personal email'].search(('first.' + 'last@company.invalid'))
    assert not checker.PATTERNS['non-example personal email'].search('first.last@example.com')
    assert checker.PATTERNS['access token'].search('ghp_' + 'a' * 36)
    assert checker.PATTERNS['credential assignment'].search('api_key=' + 'a' * 32)


def test_staged_check_reads_index_and_rejects_force_added_data(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    def git(*args):
        subprocess.run(['git', *args], check=True, capture_output=True)
    git('init')
    (tmp_path / 'README.md').write_text('Safe source')
    git('add', 'README.md')
    # Unstaged private content cannot replace the staged blob being checked.
    (tmp_path / 'README.md').write_text(('first.' + 'last@company.invalid'))
    checker.check(staged=True)
    git('add', 'README.md')
    with pytest.raises(SystemExit):
        checker.check(staged=True)
    assert ('first.' + 'last@company.invalid') not in capsys.readouterr().out
    (tmp_path / 'README.md').write_text('Safe source')
    (tmp_path / 'config.local.json').write_text('{}')
    git('add', 'README.md', 'config.local.json')
    with pytest.raises(SystemExit):
        checker.check(staged=True)
