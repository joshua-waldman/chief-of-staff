import json

import pytest

from cos import skill_commands
from cos.engine import Engine
from tests.test_preparation_review import env
from tests.test_automatic_review import Reviewer
from tests.test_workflows import Scripted, perform


@pytest.mark.parametrize('mode', ['auto_review', 'manual'])
def test_teams_default_is_in_review_and_exact_approval(env, mode):
    store, job, script, options = env
    options['approval_mode'] = mode
    calendar = script.parent.parent / 'm365-calendar' / 'scripts'
    calendar.mkdir(parents=True)
    (calendar.parent / 'SKILL.md').write_text('Calendar skill.', encoding='utf-8')
    script = calendar / 'create_event.py'
    script.write_text('print("calendar creation placeholder")', encoding='utf-8')
    reviewer = Reviewer('{"effect":"external_change","reason":"Creates a meeting with Teams joining details."}')
    engine = Engine(store, Scripted(perform('run_command', {
        'command': ['py', '-3', str(script), '--subject', 'Design review']})), review_provider=reviewer)
    try:
        engine.run(job['id'])
        receipt = store.one("SELECT * FROM approvals WHERE tool='run_command' AND status='pending'")
        command = json.loads(receipt['args'])['command']
        assert command == ['python', str(script), '--subject', 'Design review', '--teams']
        if mode == 'auto_review':
            operation = json.loads(reviewer.calls[-1][0][1]['content'])['operation']
            assert operation['arguments']['command'] == command
            assert operation['sources'][str(script)] == script.read_text(encoding='utf-8')
    finally:
        engine.stop()


@pytest.mark.parametrize('tail', [['--teams'], ['--help'], ['-h']])
def test_explicit_teams_is_not_duplicated_and_help_remains_read_only(env, tail):
    _, job, script, _ = env
    calendar = script.parent.parent / 'm365-calendar' / 'scripts'
    calendar.mkdir(parents=True)
    (calendar.parent / 'SKILL.md').write_text('Calendar skill.', encoding='utf-8')
    script = calendar / 'create_event.py'
    script.write_text('print(\"calendar\")', encoding='utf-8')
    result = skill_commands.prepare(job, {'command': ['py', '-3', str(script), *tail]})
    assert result['command'] == ['python', str(script), *tail]


def test_other_scripts_do_not_receive_calendar_flags(env):
    _, job, script, _ = env
    result = skill_commands.prepare(job, {'command': ['py', '-3', '-B', str(script)]})
    assert result['command'] == ['python', '-B', str(script)]
    # A matching filename outside the installed calendar skill is unrelated code.
    script = script.with_name('create_event.py')
    result = skill_commands.prepare(job, {'command': ['python', str(script)]})
    assert result['command'] == ['python', str(script)]


def test_non_script_commands_remain_unchanged():
    args = {'command': ['python', '-c', 'print("read")']}
    assert skill_commands.prepare({}, args) is args
