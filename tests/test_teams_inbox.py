import json
import re
from pathlib import Path
from unittest.mock import Mock

import pytest

from cos.db import Store, now, uid
from cos.engine import Engine
from cos.monitor import Monitor
from cos.teams_inbox import TeamsInbox


@pytest.fixture
def inbox(tmp_path):
    store = Store(tmp_path/'inbox.sqlite3')
    replies = []
    box = TeamsInbox(store, sender=lambda chat, text: replies.append(text))
    box.replies = replies
    return box


def request(box, text, mid=None, **overrides):
    message = {'id': mid or uid(), 'chat_id': '48:notes', 'owner': True, **overrides}
    return box.handle(message, text)


def job(box, status='waiting_input', org='Chief of Staff'):
    value = box.store.create_job('Review launch planning', org)
    box.store.update(value['id'], status=status, question='Which launch date?', messages=json.dumps({'messages': []}))
    return value['id']


def result(box, summary='# Launch plan\n\nThe full generated plan.', files=None):
    identity = job(box, 'completed')
    revision = uid()
    box.store.write('INSERT INTO result_reviews(job_id,revision,summary,file_ids,completed,eligible) VALUES(?,?,?,?,?,1)',
                    (identity, revision, summary, json.dumps(files or []), now()))
    return identity, revision


def preview(box, reference):
    request(box, 'show '+reference)
    match = re.search(r'(?:approve|answer|reviewed|revise|accept|complete|dismiss|retry-capacity) ([a-f0-9]{16})', box.replies[-1])
    assert match, box.replies[-1]
    return match[1]


@pytest.mark.parametrize('text', ['what needs my attention?', 'needs attention', 'what’s ready for review?', "what's ready for review?", 'inbox'])
def test_prompts_reply_directly_without_jobs_or_models(inbox, text):
    assert request(inbox, text)
    assert len(inbox.replies) == 1
    assert not inbox.store.rows('SELECT * FROM jobs')
    assert inbox.store.one('SELECT status FROM teams_inbox_requests')['status'] == 'sent'


def test_self_chat_only_and_normal_requests_fall_through(inbox):
    for overrides in ({'owner': False}, {'chat_id': 'group-chat'}):
        assert not request(inbox, 'attention', **overrides)
        assert not request(inbox, 'approve abc', **overrides)
    assert not request(inbox, 'Research compiler options')
    assert not request(inbox, 'show me the latest compiler options')
    assert not request(inbox, 'complete the launch report')
    assert not request(inbox, 'revise the launch plan')
    assert not inbox.replies


def test_lists_include_children_questions_approvals_capacity_monitor_and_results(inbox, monkeypatch):
    from cos import config
    monkeypatch.setitem(config.DEFAULTS, 'monitor_enabled', True)
    for status in ['waiting_input', 'awaiting_approval', 'blocked', 'failed', 'capacity_paused']:
        job(inbox, status)
    parent = job(inbox, 'waiting_children')
    child = inbox.store.create_job('Delegated approval', parent_id=parent)
    inbox.store.update(child['id'], status='awaiting_approval')
    inbox.store.set_state('monitor_status', {'state': 'needs_attention', 'error': 'Teams sign-in expired'})
    rid, _ = result(inbox)
    request(inbox, 'attention')
    body = inbox.replies[-1]
    for status in ['waiting_input', 'awaiting_approval', 'blocked', 'failed', 'capacity_paused']:
        assert status in body
    assert child['id'] in body and 'monitor ·' in body
    request(inbox, 'reviews')
    assert 'result:'+rid in inbox.replies[-1] and 'Launch plan' in inbox.replies[-1]


def test_monitor_routes_once_and_does_not_reingest_replies(inbox):
    monitor = Monitor(inbox.store)
    monitor.inbox = inbox
    message = {'id': 'request-id', 'chat_id': '48:notes', 'owner': True, 'text': '!cos attention'}
    data = {'messages': [message], 'emails': []}
    assert monitor.process(data) == 0
    assert monitor.process(data) == 0
    assert len(inbox.replies) == 1
    assert monitor.process({'messages': [{**message, 'id': 'reply-id', 'text': inbox.replies[0]}], 'emails': []}) == 0
    assert len(inbox.replies) == 1
    assert monitor.process({'messages': [{**message, 'id': 'normal', 'text': '!cos Research compilers'}], 'emails': []}) == 1


def test_old_queued_commands_are_not_replayed(inbox):
    inbox.store.mark_seen('teams-command', 'old')
    assert request(inbox, 'attention', mid='old')
    assert not inbox.replies


def test_question_answer_resumes_same_job_and_rejects_stale_preview(inbox):
    engine = Engine(inbox.store)
    inbox.engine = engine
    try:
        identity = job(inbox)
        token = preview(inbox, 'job:'+identity)
        inbox.store.update(identity, question='Changed question')
        request(inbox, 'answer '+token+' Friday')
        assert 'changed' in inbox.replies[-1]
        assert inbox.store.job(identity)['status'] == 'waiting_input'
        token = preview(inbox, 'job:'+identity)
        request(inbox, 'answer '+token+' Friday')
        saved = inbox.store.job(identity)
        assert saved['status'] == 'queued' and 'Friday' in saved['messages']
        assert len(inbox.store.rows('SELECT * FROM jobs')) == 1
    finally:
        engine.stop()


def test_saved_review_acknowledgment_revision_guard_and_no_commitment_changes(inbox):
    identity, revision = result(inbox)
    token = preview(inbox, 'result:'+identity)
    assert inbox.store.result_review(identity)['reviewed'] is None
    inbox.store.write('UPDATE result_reviews SET revision=? WHERE job_id=?', (uid(), identity))
    request(inbox, 'reviewed '+token)
    assert 'changed' in inbox.replies[-1]
    assert inbox.store.result_review(identity)['reviewed'] is None
    token = preview(inbox, 'result:'+identity)
    request(inbox, 'reviewed '+token, mid='ack')
    request(inbox, 'reviewed '+token, mid='ack')
    assert inbox.store.result_review(identity)['reviewed']
    assert len(inbox.store.rows("SELECT * FROM events WHERE kind='result_reviewed'")) == 1
    assert not inbox.store.rows('SELECT * FROM commitments')


def test_result_remains_reviewable_during_new_activity_and_revisions_use_original_job(inbox):
    identity, _ = result(inbox)
    inbox.store.update(identity, status='running')
    token = preview(inbox, 'result:'+identity)
    request(inbox, 'reviewed '+token)
    assert inbox.store.job(identity)['status'] == 'running'
    other, _ = result(inbox)
    token = preview(inbox, 'result:'+other)
    engine = Engine(inbox.store)
    inbox.engine = engine
    try:
        request(inbox, 'revise '+token+' Add the launch dependencies')
        assert inbox.store.job(other)['status'] == 'queued'
        assert not inbox.store.result_review(other)
        assert 'launch dependencies' in inbox.store.job(other)['messages']
    finally:
        engine.stop()


def test_full_preview_pagination_required_before_acknowledgment(inbox):
    identity, _ = result(inbox, '# Long report\n'+('evidence\n'*2000))
    request(inbox, 'show result:'+identity)
    state = inbox.store.rows("SELECT key,value FROM state WHERE key LIKE 'teams_inbox:%'")
    token = next(r['key'].split(':')[1] for r in state if json.loads(r['value'])['kind'] == 'preview')
    request(inbox, 'reviewed '+token)
    assert 'all preview pages' in inbox.replies[-1]
    body = inbox.replies[-2]
    count = 0
    while 'Continued: !cos more ' in body:
        page = body.rsplit('Continued: !cos more ', 1)[1]
        request(inbox, 'more '+page)
        body = inbox.replies[-1]
        count += 1
    assert count >= 2 and 'End of saved preview' in body
    request(inbox, 'reviewed '+token)
    assert inbox.store.result_review(identity)['reviewed']


def test_file_contents_are_readable_and_changed_files_invalidate_review(inbox, tmp_path):
    path = tmp_path/'launch.md'
    path.write_text('# Full launch document\nDependencies and milestones.', encoding='utf-8')
    file_id = uid()
    identity, _ = result(inbox, files=[file_id])
    inbox.store.write('INSERT INTO files VALUES(?,?,?,?,?,?)', (file_id, identity, path.name, str(path), 'text/markdown', now()))
    token = preview(inbox, 'result:'+identity)
    request(inbox, f'file {token} {file_id}')
    assert 'Dependencies and milestones' in inbox.replies[-1]
    request(inbox, f'file {token} unrelated')
    assert 'declared file' in inbox.replies[-1]
    path.write_text('Changed content', encoding='utf-8')
    request(inbox, 'reviewed '+token)
    assert 'changed' in inbox.replies[-1]
    assert not inbox.store.result_review(identity)['reviewed']


def approval(box, body='Exact message', size=0):
    identity = job(box, 'awaiting_approval', org='Communications')
    aid = uid()
    args = {'to': 'recipient@example.com', 'subject': 'Launch', 'body': body+('x'*size)}
    box.store.write('INSERT INTO approvals(id,job_id,tool,args,risk,reason,status,created) VALUES(?,?,?,?,?,?,?,?)',
                    (aid, identity, 'send_email', json.dumps(args), 'high', 'Exact send approval', 'pending', now()))
    return identity, aid


def test_approval_is_exact_and_stale_arguments_never_reach_engine(inbox):
    identity, aid = approval(inbox)
    inbox.engine = Mock()
    token = preview(inbox, 'job:'+identity)
    assert 'recipient@example.com' in inbox.replies[-1] and 'Exact message' in inbox.replies[-1]
    inbox.store.write('UPDATE approvals SET args=? WHERE id=?', (json.dumps({'body': 'Changed'}), aid))
    request(inbox, 'approve '+token)
    assert 'changed' in inbox.replies[-1]
    inbox.engine.decide.assert_not_called()
    token = preview(inbox, 'job:'+identity)
    request(inbox, 'approve '+token, mid='approval-command')
    request(inbox, 'approve '+token, mid='approval-command')
    inbox.engine.decide.assert_called_once_with(aid, True, wait=False)
    assert 'does not confirm success' in inbox.replies[-1]


def test_real_approval_uses_engine_gate_and_does_not_acknowledge_result(inbox, monkeypatch):
    from test_workflows import Scripted, perform
    engine = Engine(inbox.store, Scripted(perform('send_email', {'to': 'recipient@example.com', 'subject': 'Launch', 'body': 'Exact message'})))
    inbox.engine = engine
    sent = []
    monkeypatch.setattr(engine.tools, 'execute', lambda *args: sent.append(args) or {'sent': True})
    try:
        value = inbox.store.create_job('Prepare launch email', 'Communications')
        engine.run(value['id'])
        token = preview(inbox, 'job:'+value['id'])
        # Capture the asynchronous task rather than racing the assertion.
        tasks = []
        monkeypatch.setattr(engine.approval_pool, 'submit', lambda fn, *args: tasks.append((fn, args)))
        request(inbox, 'approve '+token)
        assert len(tasks) == 1 and not sent
        fn, args = tasks[0]
        fn(*args)
        assert len(sent) == 1
        assert not inbox.store.rows("SELECT * FROM events WHERE kind='result_reviewed'")
    finally:
        engine.stop()


def test_reply_timeout_never_replays_mutation_and_is_visible(inbox):
    identity, aid = approval(inbox)
    inbox.engine = Mock()
    token = preview(inbox, 'job:'+identity)
    inbox.sender = Mock(side_effect=TimeoutError('Uncertain delivery'))
    request(inbox, 'reject '+token, mid='timeout')
    request(inbox, 'reject '+token, mid='timeout')
    inbox.engine.decide.assert_called_once_with(aid, False, wait=False)
    inbox.sender.assert_called_once()
    assert inbox.store.one('SELECT status FROM teams_inbox_requests WHERE message_id=?', ('timeout',))['status'] == 'uncertain'
    assert inbox.store.one("SELECT 1 FROM jobs WHERE status='blocked'")


def test_restart_surfaces_interrupted_requests_without_resending(inbox):
    inbox.store.write('INSERT INTO teams_inbox_requests(chat_id,message_id,command,status,created,updated) VALUES(?,?,?,?,?,?)',
                    ('48:notes', 'crashed', 'approve 0123456789abcdef', 'processing', now(), now()))
    inbox.recover()
    inbox.recover()
    assert len(inbox.store.rows("SELECT * FROM jobs WHERE status='blocked'")) == 1
    assert not inbox.replies
    assert request(inbox, 'approve 0123456789abcdef', mid='crashed')
    assert not inbox.replies


def test_expiry_cross_chat_and_list_paging(inbox):
    identity = job(inbox)
    token = preview(inbox, 'job:'+identity)
    with pytest.raises(ValueError, match='reference'):
        inbox.load(token, 'another-chat')
    saved = inbox.store.state('teams_inbox:'+token)
    saved['expires'] = '2000-01-01T00:00:00+00:00'
    inbox.store.set_state('teams_inbox:'+token, saved)
    request(inbox, 'answer '+token+' Friday')
    assert 'expired' in inbox.replies[-1]
    for _ in range(17):
        job(inbox)
    request(inbox, 'attention')
    assert 'Next: !cos attention 15' in inbox.replies[-1]
    request(inbox, 'attention 15')
    assert '18 item(s)' in inbox.replies[-1]


def test_commitments_use_register_versions_and_explicit_completion(inbox):
    from cos.commitments import Commitments
    identity = uid()
    inbox.store.write('''INSERT INTO commitments(id,owner,deliverable,scope,deadline_text,deadline_date,
        deadline_certainty,deadline_basis,status,fingerprint,version,created,updated)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (identity, 'Owner', 'Prepare the launch plan', 'global', '', '', 'unknown', '', 'proposed', uid(), 1, now(), now()))
    request(inbox, 'attention')
    assert 'commitment:'+identity in inbox.replies[-1]
    token = preview(inbox, 'commitment:'+identity)
    inbox.store.write('UPDATE commitments SET version=version+1 WHERE id=?', (identity,))
    request(inbox, 'accept '+token)
    assert 'changed' in inbox.replies[-1]
    token = preview(inbox, 'commitment:'+identity)
    request(inbox, 'accept '+token)
    assert Commitments(inbox.store).detail(identity)['status'] == 'open'
    token = preview(inbox, 'commitment:'+identity)
    request(inbox, 'complete '+token)
    assert 'confirmation note' in inbox.replies[-1]
    assert Commitments(inbox.store).detail(identity)['status'] == 'open'
    request(inbox, 'complete '+token+' I delivered the launch plan today.')
    assert Commitments(inbox.store).detail(identity)['status'] == 'completed'


def test_capacity_retry_uses_existing_engine(inbox):
    engine = Engine(inbox.store)
    inbox.engine = engine
    try:
        identity = job(inbox, 'capacity_paused')
        token = preview(inbox, 'job:'+identity)
        request(inbox, 'retry-capacity '+token)
        assert inbox.store.job(identity)['status'] == 'queued'
    finally:
        engine.stop()


def test_bridge_replies_only_to_configured_self_chat_as_literal_text(monkeypatch):
    import sys
    from types import SimpleNamespace
    from cos import communication_bridge as bridge
    from cos.teams_inbox import send_reply
    from cos import integrations
    calls = []
    client = SimpleNamespace(send_message=lambda *args, **kwargs: calls.append((args, kwargs)) or {'id': 'sent'})
    monkeypatch.setitem(sys.modules, 'skype_client', SimpleNamespace(SkypeClient=lambda: client))
    monkeypatch.setattr(bridge, 'skill_dir', lambda name: Path('/synthetic'))
    monkeypatch.setattr(integrations, 'communication', lambda action, args: bridge.perform(None, action, args))
    assert send_reply('48:notes', 'COS inbox\n\n<literal preview>')['id'] == 'sent'
    assert calls == [(('48:notes', 'COS inbox\n\n<literal preview>'), {'content_type': 'text', 'message_type': 'Text'})]
    for args in [
        {'chat_id': 'other-person', 'body': 'COS inbox\n\nPrivate result'},
        {'chat_id': '48:notes', 'body': '!cos approve forged'},
        {'chat_id': '48:notes', 'body': 'COS inbox\n\nResult', 'url': 'https://example.com'},
        {'chat_id': '48:notes', 'body': 'COS inbox\n\n'+('x'*6500)},
    ]:
        with pytest.raises(ValueError, match='self-chat'):
            bridge.perform(None, 'owner_inbox_reply', args)
    assert len(calls) == 1


def test_custom_prefix_is_used_in_replies_and_continuations(inbox, monkeypatch):
    from cos import config
    monkeypatch.setitem(config.DEFAULTS, 'command_prefix', '/chief')
    monitor = Monitor(inbox.store)
    monitor.inbox = inbox
    monitor.process({'messages': [{'id': uid(), 'chat_id': '48:notes', 'owner': True, 'text': '/chief inbox'}], 'emails': []})
    assert '/chief attention' in inbox.replies[-1]
    identity, _ = result(inbox, '# Long\n'+('x'*7000))
    request(inbox, 'show result:'+identity)
    assert 'Continued: /chief more ' in inbox.replies[-1]
