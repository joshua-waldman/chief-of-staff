"""Intake-only Microsoft writes, using synthetic sources and connector receipts."""
import json
import threading

import pytest

from cos import intake_cleanup as cleanup
from cos.db import Store
from cos.integrations import CommunicationRejected
from cos.knowledge import Knowledge
from cos.message_triage import read_targets
from cos.monitor import Monitor
from cos.policy import classify
from cos.teams_intake import TeamsIntake
from cos.teams_topics import TeamsTopics
from tests.test_intake import setup, process, mail
from tests.test_queue_intake import fixture as queued_email
from tests.test_teams_topics import SemanticProvider, message, topic_job


@pytest.fixture
def writes(monkeypatch):
    calls = []
    def execute(action, args):
        calls.append((action, args))
        return {'executed': True, 'operation': action, 'source_id': next(iter(args.values()))}
    monkeypatch.setattr(cleanup, 'communication', execute)
    return calls


def receipts(store):
    return store.rows('SELECT * FROM intake_cleanup ORDER BY created,id')


def test_email_deletes_only_new_ignored_messages_once(tmp_path, writes):
    store, monitor, _, _ = setup(tmp_path)
    process(monitor, [mail('a'), mail('b')])
    assert len(receipts(store)) == 2
    assert not writes  # Ingestion only commits the outbox; no network in its transaction.
    cleanup.drain(store)
    assert {args['id'] for action, args in writes if action == 'delete_email'} == {'a', 'b'}
    assert all(r['status'] == 'executed' for r in receipts(store))
    process(monitor, [mail('a'), mail('b'), mail('c')])
    cleanup.drain(store)
    cleanup.drain(store)
    assert len(writes) == 3 and writes[-1] == ('delete_email', {'id': 'c'})
    assert not store.rows('SELECT * FROM approvals')
    job = store.rows('SELECT * FROM jobs')[0]
    assert job['status'] == 'completed' and job['step'] == 0
    assert len(store.detail(job['id'], compact=True)['intake_cleanup']) == 3


def test_queued_email_backlog_schedules_cleanup(tmp_path, writes):
    store, job, gate = queued_email(tmp_path)
    assert gate.check([job]) == 1
    cleanup.drain(store)
    assert writes == [('delete_email', {'id': 'mail1'})]


def test_pause_leaves_cleanup_pending_until_resumed(tmp_path, writes, monkeypatch):
    store, monitor, _, _ = setup(tmp_path)
    process(monitor, [mail()])
    monkeypatch.setattr(cleanup, 'settings', lambda: {'queue_paused': True})
    cleanup.drain(store)
    assert not writes and receipts(store)[0]['status'] == 'pending'
    monkeypatch.setattr(cleanup, 'settings', lambda: {'queue_paused': False})
    cleanup.drain(store)
    assert len(writes) == 1


def test_normal_and_post_analysis_ignores_never_schedule(tmp_path, writes):
    store, monitor, _, _ = setup(tmp_path, lambda payload: [])
    process(monitor, [mail()])
    job = store.rows('SELECT * FROM jobs')[0]
    store.update(job['id'], status='completed', step=1, messages=json.dumps({'messages': []}))
    store.write('INSERT INTO job_briefs VALUES(?,?,?)', (job['id'], json.dumps({'triage_category': 'ignore'}), job['created']))
    cleanup.drain(store)
    assert not receipts(store) and not writes
    for tool, args in [('delete_email', {'id': 'm'}), ('mark_teams_read', {'chat_id': 'chat'})]:
        assert classify(tool, args, org='Communications').approval


@pytest.mark.parametrize('change', ['guidance', 'review', 'child', 'source'])
def test_changed_context_cancels_pending_cleanup(tmp_path, writes, change):
    store, monitor, rule, _ = setup(tmp_path)
    process(monitor, [mail()])
    job = store.rows('SELECT * FROM jobs')[0]
    if change == 'guidance':
        Knowledge(store).forget(rule['id'])
    elif change == 'review':
        store.write('INSERT INTO review_messages(job_id,role,body,created) VALUES(?,?,?,?)',
                    (job['id'], 'user', 'Please investigate', job['created']))
    elif change == 'child':
        store.create_job('Investigate', 'Technology', parent_id=job['id'])
    else:
        store.write('INSERT INTO communication_inbox(job_id,body,created) VALUES(?,?,?)', (job['id'], 'New activity', job['created']))
    cleanup.drain(store)
    assert not writes and receipts(store)[0]['status'] == 'cancelled'


@pytest.mark.parametrize('error,expected', [
    (CommunicationRejected('Graph API 403: denied'), 'failed_not_executed'),
    (TimeoutError('response timed out'), 'uncertain'),
    (RuntimeError('network disconnected'), 'uncertain'),
])
def test_failures_are_audited_without_approval_or_replay(tmp_path, monkeypatch, error, expected):
    store, monitor, _, _ = setup(tmp_path)
    process(monitor, [mail()])
    calls = []
    def fail(*args):
        calls.append(args)
        raise error
    monkeypatch.setattr(cleanup, 'communication', fail)
    cleanup.drain(store)
    restarted = Store(store.path)
    cleanup.recover(restarted)
    cleanup.drain(restarted)
    assert len(calls) == 1 and receipts(store)[0]['status'] == expected
    job = store.rows('SELECT * FROM jobs')[0]
    assert job['status'] == 'completed'
    assert not store.rows('SELECT * FROM approvals')
    assert expected in store.rows("SELECT * FROM events WHERE kind='intake_cleanup'")[0]['body']


def test_restart_resumes_pending_and_retains_interrupted_uncertainty(tmp_path, writes):
    store, monitor, _, _ = setup(tmp_path)
    process(monitor, [mail('a'), mail('b')])
    store.write("UPDATE intake_cleanup SET status='executing' WHERE source_id='a'")
    restarted = Store(store.path)
    cleanup.recover(restarted)
    cleanup.drain(restarted)
    assert writes == [('delete_email', {'id': 'b'})]
    assert {r['source_id']: r['status'] for r in receipts(store)} == {'a': 'uncertain', 'b': 'executed'}


def test_concurrent_drains_claim_once(tmp_path, monkeypatch):
    store, monitor, _, _ = setup(tmp_path)
    process(monitor, [mail()])
    entered, release = threading.Event(), threading.Event()
    calls = []
    def execute(*args):
        calls.append(args)
        entered.set()
        assert release.wait(5)
        return {'executed': True}
    monkeypatch.setattr(cleanup, 'communication', execute)
    worker = threading.Thread(target=cleanup.drain, args=(store,))
    worker.start()
    try:
        assert entered.wait(5)
        cleanup.drain(store)
        assert len(calls) == 1
    finally:
        release.set()
        worker.join(5)
    assert receipts(store)[0]['status'] == 'executed'


class TeamsClassifier:
    def __init__(self, choose=lambda t: True):
        self.choose, self.calls = choose, []

    def complete(self, messages, **options):
        payload = json.loads(messages[-1]['content'])
        self.calls.append(payload)
        return {'content': json.dumps({'decisions': [
            {'thread_id': t['thread_id'], 'ignore': True,
             'rule_ids': [payload['owner_preferences'][0]['id']], 'reason': 'Matches owner guidance.'}
            for t in payload['topics'] if self.choose(t)]})}, {}


def teams_setup(tmp_path, choose=lambda t: True, target=None):
    store = Store(tmp_path/'teams.db')
    rule = Knowledge(store).save({'kind': 'preference', 'title': 'Routine Teams notices',
        'content': 'Ignore routine Teams updates; no response or work is needed.', 'target': target or {}}, user=True)
    monitor = Monitor(store)
    monitor.teams_topics = TeamsTopics(store, SemanticProvider())
    classifier = TeamsClassifier(choose)
    monitor.teams_intake = TeamsIntake(store, classifier)
    watching(store)
    return store, monitor, rule, classifier


def watching(store):
    store.set_state('monitor_status', {'state': 'watching', 'sources': {'teams': {'state': 'watching'}}})


def ingest(monitor, *messages):
    return monitor.process({'messages': list(messages), 'emails': []})


def test_teams_early_ignore_uses_transport_chat_and_new_activity_deduplicates(tmp_path, writes):
    store, monitor, _, classifier = teams_setup(tmp_path)
    ingest(monitor, message('1', 'Budget update'))
    job = topic_job(monitor, 'Budget planning')
    assert job['status'] == 'completed' and job['step'] == 0
    assert not read_targets(store, job)  # No competing read approval during automatic cleanup.
    cleanup.drain(store)
    ingest(monitor, message('1', 'Budget update'))
    cleanup.drain(store)
    assert writes == [('mark_teams_read', {'chat_id': 'chat'})]
    assert not read_targets(store, store.job(job['id']))
    ingest(monitor, message('2', 'Budget is unchanged'))
    assert len(classifier.calls[-1]['topics'][0]['messages']) == 2  # Retained full context.
    cleanup.drain(store)
    assert len(writes) == 2
    assert not store.rows('SELECT * FROM approvals')
    assert store.job(job['id'])['status'] == 'completed'


def test_teams_waits_for_unhandled_sibling_then_cleans_up(tmp_path, writes):
    store, monitor, _, _ = teams_setup(tmp_path, lambda t: t['title'] == 'Budget planning')
    ingest(monitor, message('1', 'Budget update'), message('2', 'Release requires my review'))
    cleanup.drain(store)
    assert not writes and receipts(store)[0]['status'] == 'deferred'
    sibling = topic_job(monitor, 'Release readiness')
    store.update(sibling['id'], status='completed', step=1)
    cleanup.drain(store)
    assert writes == [('mark_teams_read', {'chat_id': 'chat'})]


@pytest.mark.parametrize('uncertain', [False, True])
def test_teams_coalesces_ignored_topics_into_one_chat_write(tmp_path, writes, monkeypatch, uncertain):
    store, monitor, _, _ = teams_setup(tmp_path)
    ingest(monitor, message('1', 'Budget update'), message('2', 'Release update'))
    assert len(receipts(store)) == 2
    if uncertain:
        def timeout(action, args):
            writes.append((action, args))
            raise TimeoutError('Response unavailable')
        monkeypatch.setattr(cleanup, 'communication', timeout)
    cleanup.drain(store, limit=1)
    cleanup.drain(store)
    assert writes == [('mark_teams_read', {'chat_id': 'chat'})]
    assert all(r['status'] == ('uncertain' if uncertain else 'executed') for r in receipts(store))


@pytest.mark.parametrize('state', [
    {'state': 'scanning', 'active_source': 'teams', 'sources': {'teams': {'state': 'watching'}}},
    {'state': 'needs_attention', 'sources': {'teams': {'state': 'needs_attention'}}},
    {'state': 'watching', 'sources': {'teams': {'state': 'watching'}}, 'limitations': [{'chat_id': 'chat', 'reason': 'Unavailable context'}]},
    {},
])
def test_teams_waits_for_complete_collection(tmp_path, writes, state):
    store, monitor, _, _ = teams_setup(tmp_path)
    ingest(monitor, message('1', 'Budget update'))
    store.set_state('monitor_status', state)
    cleanup.drain(store)
    assert not writes and receipts(store)[0]['status'] == 'deferred'
    watching(store)
    cleanup.drain(store)
    assert len(writes) == 1


def test_teams_waits_for_topic_migration(tmp_path, writes):
    store, monitor, _, _ = teams_setup(tmp_path)
    ingest(monitor, message('1', 'Budget update'))
    store.write('INSERT INTO teams_topic_migrations(chat_id,job_ids,status,updated) VALUES(?,?,?,?)',
                ('chat', '[]', 'pending', 'now'))
    cleanup.drain(store)
    assert not writes and receipts(store)[0]['status'] == 'deferred'
    store.write("UPDATE teams_topic_migrations SET status='completed'")
    cleanup.drain(store)
    assert len(writes) == 1


def test_teams_restart_requires_fresh_collection_even_with_saved_watching_status(tmp_path, writes, monkeypatch):
    store, monitor, _, _ = teams_setup(tmp_path)
    ingest(monitor, message('1', 'Budget update'))
    # Exercise startup without launching real worker/network threads.
    launched = []
    class Thread:
        def __init__(self, **kwargs):
            launched.append(kwargs)
        def start(self):
            pass
    monkeypatch.setattr('cos.monitor.threading.Thread', Thread)
    monitor.start()
    assert len(launched) == 2
    cleanup.drain(store)
    assert not writes and receipts(store)[0]['status'] == 'deferred'
    store.set_state('intake_cleanup_teams_collected', True)
    cleanup.drain(store)
    assert len(writes) == 1


def test_teams_new_nonignored_activity_cancels_earlier_pending_read(tmp_path, writes):
    store, monitor, _, classifier = teams_setup(tmp_path)
    ingest(monitor, message('1', 'Budget update'))
    classifier.choose = lambda t: False
    ingest(monitor, message('2', 'Budget needs a decision'))
    cleanup.drain(store)
    assert not writes and receipts(store)[0]['status'] == 'cancelled'


def test_teams_never_shortcuts_started_jobs_or_sweeps_old_ignores(tmp_path, writes):
    store, monitor, _, classifier = teams_setup(tmp_path, lambda t: False)
    ingest(monitor, message('1', 'Budget update'))
    job = topic_job(monitor, 'Budget planning')
    store.update(job['id'], status='completed', step=1, messages='{"messages":[]}')
    classifier.choose = lambda t: True
    ingest(monitor, message('2', 'Budget update again'))
    assert len(classifier.calls) == 1
    assert not receipts(store) and not writes


@pytest.mark.parametrize('target', [
    {'source': 'email', 'thread_id': 'chat'},
    {'source': 'teams', 'thread_id': 'chat'},  # Chat transport is not a topic binding.
    {'source': 'teams', 'sender_id': '8:orgid:someone-else'},
])
def test_teams_rejects_wrong_source_topic_or_sender_binding(tmp_path, writes, target):
    store, monitor, _, classifier = teams_setup(tmp_path, target=target)
    ingest(monitor, {**message('1', 'Budget update'), 'sender_id': '8:orgid:actual'})
    assert not classifier.calls and not receipts(store)
    assert topic_job(monitor, 'Budget planning')['status'] == 'queued'


def test_teams_sender_binding_requires_every_incoming_sender(tmp_path, writes):
    store, monitor, _, _ = teams_setup(tmp_path, target={'source': 'teams', 'sender_id': '8:orgid:actual'})
    ingest(monitor, {**message('1', 'Budget update'), 'sender_id': '8:orgid:actual'},
           {**message('2', 'Budget question'), 'sender_id': '8:orgid:other'})
    assert not receipts(store)


@pytest.mark.parametrize('extra', [
    {'text': 'Budget update [Teams image: {}]'},
    {'text': 'Budget update [Teams attachment: {}]'},
    {'meeting_event': True},
    {'attachments': [{'id': 'file'}]},
])
def test_teams_incomplete_visual_and_recording_sources_use_normal_analysis(tmp_path, writes, extra):
    store, monitor, _, classifier = teams_setup(tmp_path)
    ingest(monitor, {**message('1', 'Budget update'), **extra})
    assert not classifier.calls and not receipts(store)


def test_teams_classifier_failure_keeps_normal_triage(tmp_path, writes):
    store, monitor, _, _ = teams_setup(tmp_path)
    class Bad:
        def complete(self, *args, **kwargs):
            raise TimeoutError('No model capacity')
    monitor.teams_intake.provider = Bad()
    ingest(monitor, message('1', 'Budget update'))
    assert topic_job(monitor, 'Budget planning')['status'] == 'queued'
    assert not receipts(store)


@pytest.mark.parametrize('bad', [
    'not-json', {'decisions': 'invalid'},
    {'decisions': [{'thread_id': 'invented', 'ignore': True, 'rule_ids': ['invented'], 'reason': 'Ignore'}]},
])
def test_teams_invalid_classifier_output_keeps_normal_triage(tmp_path, writes, bad):
    store, monitor, _, _ = teams_setup(tmp_path)
    class Bad:
        def complete(self, *args, **kwargs):
            return {'content': bad if isinstance(bad, str) else json.dumps(bad)}, {}
    monitor.teams_intake.provider = Bad()
    ingest(monitor, message('1', 'Budget update'))
    assert topic_job(monitor, 'Budget planning')['status'] == 'queued'
    assert not receipts(store)


def test_teams_wrong_sender_cannot_borrow_a_relevant_rule(tmp_path, writes):
    # The rule is relevant to one topic in the batch, but the model claims it
    # also dismisses another participant's topic. Server binding checks prevail.
    store, monitor, _, _ = teams_setup(tmp_path, target={'source': 'teams', 'sender_id': '8:orgid:actual'})
    ingest(monitor, {**message('1', 'Budget update'), 'sender_id': '8:orgid:actual'},
           {**message('2', 'Release update'), 'sender_id': '8:orgid:other'})
    assert topic_job(monitor, 'Budget planning')['status'] == 'completed'
    assert topic_job(monitor, 'Release readiness')['status'] == 'queued'
    cleanup.drain(store)
    assert not writes


def test_teams_kb_change_during_inference_invalidates_decision(tmp_path, writes):
    store, monitor, rule, classifier = teams_setup(tmp_path)
    original = classifier.complete
    def change(*args, **kwargs):
        result = original(*args, **kwargs)
        Knowledge(store).forget(rule['id'])
        return result
    classifier.complete = change
    ingest(monitor, message('1', 'Budget update'))
    assert topic_job(monitor, 'Budget planning')['status'] == 'queued'
    assert not receipts(store)
