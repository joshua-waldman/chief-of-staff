import json

import pytest

from cos import config
from cos.db import Store, now, uid
from cos.monitor import Monitor
from cos.teams_inbox import TeamsInbox
from cos.task_status import respond


@pytest.fixture
def inbox(tmp_path):
    store = Store(tmp_path/'status.sqlite3')
    replies = []
    box = TeamsInbox(store, sender=lambda chat, body: replies.append(body))
    box.replies = replies
    return box


def task(box, name, status='running', parent=None):
    job = box.store.create_job(name, parent_id=parent)
    box.store.update(job['id'], status=status)
    return job['id']


def ask(box, query, mid=None, **overrides):
    return box.handle({'id': mid or uid(), 'chat_id': '48:notes', 'owner': True, **overrides}, query)


def test_overview_counts_only_top_level_tasks_and_prioritizes_active(inbox):
    active = task(inbox, 'Release planning', 'waiting_children')
    task(inbox, 'Child failed', 'failed', active)
    completed = task(inbox, 'Earlier report', 'completed')
    task(inbox, 'Cancelled report', 'cancelled')
    inbox.store.write('INSERT INTO result_reviews(job_id,revision,summary,file_ids,completed,eligible) VALUES(?,?,?,?,?,1)',
                      (completed, uid(), '# Report', '[]', now()))
    ask(inbox, 'status')
    body = inbox.replies[-1]
    assert '3 total' in body and 'Active: 1' in body
    assert 'Completed: 1' in body and 'Cancelled: 1' in body and 'Ready for review: 1' in body
    assert 'Child failed' not in body
    assert body.index(active) < body.index(completed)
    assert 'Queue: enabled · Monitoring: paused' in body


def test_empty_overview_and_configured_monitoring_state(inbox, monkeypatch):
    monkeypatch.setitem(config.DEFAULTS, 'monitor_enabled', True)
    monkeypatch.setitem(config.DEFAULTS, 'queue_paused', True)
    inbox.store.set_state('monitor_status', {'state': 'needs_attention'})
    ask(inbox, 'STATUS')
    assert '0 total' in inbox.replies[-1] and 'No tasks on this page' in inbox.replies[-1]
    assert 'Queue: paused · Monitoring: needs_attention' in inbox.replies[-1]


@pytest.mark.parametrize('reference', ['id', 'prefix', 'job', 'task', 'name', 'quoted'])
def test_identifies_task_by_name_full_id_or_unique_prefix(inbox, reference):
    identity = task(inbox, 'Release planning milestone')
    queries = {'id': identity, 'prefix': identity[:8], 'job': 'job:'+identity,
               'task': 'task:'+identity, 'name': 'release milestone', 'quoted': '"Release planning"'}
    ask(inbox, 'status '+queries[reference])
    assert 'Task: job:'+identity in inbox.replies[-1]
    assert 'Status: Running' in inbox.replies[-1]
    assert 'Refresh: !cos status job:'+identity in inbox.replies[-1]


def test_search_uses_user_names_original_titles_and_projects(inbox):
    identity = task(inbox, 'Release planning milestone')
    inbox.store.write('INSERT INTO job_names VALUES(?,?)', (identity, 'Launch readiness'))
    inbox.store.update(identity, project='Aurora')
    for query in ['Launch readiness', 'release milestone', 'Aurora readiness']:
        ask(inbox, 'status '+query)
        assert 'Task: job:'+identity in inbox.replies[-1]
        assert 'Launch readiness' in inbox.replies[-1]


def test_ambiguous_names_and_id_prefixes_return_choices(inbox):
    first = task(inbox, 'Launch plan hardware')
    second = task(inbox, 'Launch plan software')
    inbox.store.write('UPDATE jobs SET id=? WHERE id=?', ('abcdef'+first[6:], first))
    first = 'abcdef'+first[6:]
    inbox.store.write('UPDATE jobs SET id=? WHERE id=?', ('abcdef'+second[6:], second))
    second = 'abcdef'+second[6:]
    for query in ['launch', 'abcdef']:
        ask(inbox, 'status '+query)
        body = inbox.replies[-1]
        assert 'Choose a task by its ID' in body
        assert first in body and second in body
        assert 'Task: job:' not in body


def test_child_ids_never_resolve_as_top_level_tasks_and_search_is_literal(inbox):
    parent = task(inbox, 'Top level report')
    child = task(inbox, 'Delegated compiler investigation', parent=parent)
    for query in ['job:'+child, child, 'compiler', "%' OR 1=1 --", 'job:not-an-id']:
        ask(inbox, 'status '+query)
        assert 'No top-level task matches' in inbox.replies[-1]
    assert len(inbox.store.rows('SELECT * FROM jobs')) == 2


def test_detail_explains_saved_progress_nested_blockers_and_review_state(inbox):
    identity = task(inbox, 'Release plan', 'waiting_children')
    child = task(inbox, 'Compiler research', 'waiting_children', identity)
    grandchild = task(inbox, 'Verify toolchain', 'awaiting_approval', child)
    inbox.store.update(grandchild, question='Approve the exact toolchain change.')
    task(inbox, 'Completed research', 'completed', child)
    inbox.store.write('INSERT INTO job_briefs VALUES(?,?,?)',
                      (identity, json.dumps({'status_summary': 'Research gathered', 'next_step': 'Synthesize the final plan'}), now()))
    inbox.store.write('INSERT INTO result_reviews(job_id,revision,summary,file_ids,completed,eligible) VALUES(?,?,?,?,?,1)',
                      (identity, uid(), '# Saved draft', '[]', now()))
    ask(inbox, 'status '+identity)
    body = inbox.replies[-1]
    assert 'Status: Waiting for delegated work' in body
    assert 'Latest saved progress (recorded ' in body and 'Research gathered' in body
    assert 'Synthesize the final plan' in body
    assert 'Delegated work: 3 task(s)' in body and 'Completed: 1' in body
    assert 'Approve the exact toolchain change.' in body
    assert '!cos show job:'+grandchild in body
    assert '!cos show result:'+identity in body
    assert inbox.store.result_review(identity)['reviewed'] is None
    assert inbox.store.job(identity)['status'] == 'waiting_children'


def test_completed_and_cancelled_tasks_remain_discoverable(inbox):
    completed = task(inbox, 'Completed launch report', 'completed')
    inbox.store.update(completed, result='The final report identifies three dependencies.')
    cancelled = task(inbox, 'Cancelled launch report', 'cancelled')
    ask(inbox, 'status '+completed)
    assert 'Status: Completed' in inbox.replies[-1] and 'three dependencies' in inbox.replies[-1]
    ask(inbox, 'status '+cancelled)
    assert 'Status: Cancelled' in inbox.replies[-1]
    assert 'available actions' not in inbox.replies[-1]


def test_status_is_read_only_deduplicated_owner_only_and_uses_latest_state(inbox):
    identity = task(inbox, 'Launch plan')
    for overrides in ({'owner': False}, {'chat_id': 'another-chat'}):
        assert not ask(inbox, 'status '+identity, **overrides)
    assert not inbox.replies
    ask(inbox, 'status '+identity, mid='first')
    ask(inbox, 'status '+identity, mid='first')
    assert len(inbox.replies) == 1
    inbox.store.update(identity, status='failed', question='Source unavailable')
    ask(inbox, 'status '+identity)
    assert 'Status: Failed' in inbox.replies[-1] and 'Source unavailable' in inbox.replies[-1]
    assert len(inbox.store.rows('SELECT * FROM jobs')) == 1
    assert not inbox.store.rows('SELECT * FROM approvals')
    assert not inbox.store.rows("SELECT * FROM state WHERE value LIKE '%\"kind\": \"preview\"%'")


def test_status_list_and_search_have_reusable_pagination_commands(inbox):
    identities = [task(inbox, 'Launch report '+str(n)) for n in range(17)]
    ask(inbox, 'status')
    assert 'Next: !cos status page 15' in inbox.replies[-1]
    ask(inbox, 'status page 15')
    assert 'Next:' not in inbox.replies[-1]
    assert sum(identity in inbox.replies[-1] for identity in identities) == 2
    ask(inbox, 'status launch')
    assert 'Next: !cos status page 15 find launch' in inbox.replies[-1]
    ask(inbox, 'status page 15 find launch')
    assert sum(identity in inbox.replies[-1] for identity in identities) == 2
    ask(inbox, 'status page 1000001')
    assert 'offset up to' in inbox.replies[-1]


def test_reserved_page_names_can_be_found_literally_and_long_detail_uses_more(inbox):
    identity = task(inbox, 'page 15 launch')
    ask(inbox, 'status find page 15')
    assert 'Task: job:'+identity in inbox.replies[-1]
    inbox.store.write('INSERT INTO job_briefs VALUES(?,?,?)',
                      (identity, json.dumps({'status_summary': 'Verified source '*700}), now()))
    ask(inbox, 'status '+identity)
    assert 'Continued: !cos more ' in inbox.replies[-1]
    token = inbox.replies[-1].rsplit('Continued: !cos more ', 1)[1]
    ask(inbox, 'more '+token)
    assert 'Refresh: !cos status job:'+identity in inbox.replies[-1]


def test_monitor_routes_status_directly_with_configured_prefix(inbox, monkeypatch):
    monkeypatch.setitem(config.DEFAULTS, 'command_prefix', '/chief')
    identity = task(inbox, 'Launch plan')
    monitor = Monitor(inbox.store)
    monitor.inbox = inbox
    assert monitor.process({'emails': [], 'messages': [
        {'id': uid(), 'chat_id': '48:notes', 'owner': True, 'text': '/chief status '+identity}]}) == 0
    assert 'Refresh: /chief status job:'+identity in inbox.replies[-1]
    assert len(inbox.store.rows('SELECT * FROM jobs')) == 1
    ask(inbox, 'inbox')
    assert '/chief status <task name or ID>' in inbox.replies[-1]


def test_discovery_does_not_load_agent_history(inbox, monkeypatch):
    identity = task(inbox, 'Launch report')
    def forbidden(*args, **kwargs):
        pytest.fail('Status should not load full jobs or agent messages')
    monkeypatch.setattr(inbox.store, 'job', forbidden)
    assert 'Task: job:'+identity in respond(inbox.store, identity)
