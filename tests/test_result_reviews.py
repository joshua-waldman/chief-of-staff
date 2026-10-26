import json
import pytest
from fastapi.testclient import TestClient
from cos import config, server, tools
from cos.db import Store
from cos.engine import Engine, session
from cos.reviews import submit
from test_workflows import Scripted, perform


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, 'DATA', tmp_path/'data')
    monkeypatch.setattr(config, 'DATA', tmp_path/'data')
    store = Store(tmp_path/'results.db')
    return store, Engine(store)


def finish(store, engine, job, summary='Finished response', file_ids=None, result_kind=None):
    args = {'summary': summary, 'file_ids': file_ids or []}
    if result_kind is not None:
        args['result_kind'] = result_kind
    engine.provider = Scripted(perform('finish', args))
    engine.run(job['id'])
    assert store.job(job['id'])['status'] == 'completed'
    return store.result_review(job['id'])


def test_finish_queues_response_and_selected_deliverables_durably(setup):
    store, engine = setup
    job = store.create_job('Prepare a brief', 'Technology')
    output = engine.tools.execute(job, 'write_file', {'path': 'brief.txt', 'content': 'Returned brief'})
    review = finish(store, engine, job, file_ids=[output['file_id']])
    assert review['file_ids'] == [output['file_id']]
    assert store.pending_results()[0]['revision'] == review['revision']
    restored = Store(store.path)
    detail = restored.detail(job['id'])
    assert detail['result_review']['summary'] == 'Finished response'
    assert detail['files'][0]['id'] == output['file_id']
    # Opening, reloading and restarting never count as owner review.
    assert len(restored.pending_results()) == 1
    restored.review_result(job['id'], review['revision'])
    restored.review_result(job['id'], review['revision'])
    assert not Store(store.path).pending_results()
    assert len(store.rows("SELECT * FROM events WHERE kind='result_reviewed'")) == 1
    assert store.job(job['id'])['result'] == 'Finished response'


def test_child_results_and_intake_closures_do_not_notify_owner(setup):
    store, engine = setup
    parent = store.create_job('Coordinate research', 'Technology')
    child = store.create_job('Research one source', 'Technology', parent_id=parent['id'])
    finish(store, engine, child)
    ignored = store.create_job('Incoming notification', 'Communications')
    store.update(ignored['id'], status='completed', result='Ignored by current guidance')
    assert not Store(store.path).pending_results()
    finish(store, engine, parent, 'Combined research')
    assert [r['id'] for r in store.pending_results()] == [parent['id']]


def test_revision_feedback_requeues_and_old_review_cannot_clear_new_result(setup):
    store, engine = setup
    job = store.create_job('Prepare a plan', 'Strategy')
    original = finish(store, engine, job, 'First plan')
    submit(engine, job['id'], 'Add the deadlines')
    assert store.job(job['id'])['status'] == 'queued'
    assert not store.pending_results()
    assert session(store.job(job['id']))['messages'][-1]['content'].endswith('Add the deadlines')
    with pytest.raises(ValueError):
        store.review_result(job['id'], original['revision'])
    revised = finish(store, engine, job, 'Revised plan with deadlines')
    assert revised['revision'] != original['revision']
    with pytest.raises(ValueError):
        store.review_result(job['id'], original['revision'])
    assert len(store.pending_results()) == 1
    assert store.detail(job['id'])['review_messages'][-1]['body'] == 'Revised plan with deadlines'
    assert not store.rows('SELECT * FROM approvals')


def test_result_and_review_commit_atomically_and_cancelled_job_stays_cancelled(setup):
    store, engine = setup
    job = store.create_job('Return a result', 'Technology')
    store.write("CREATE TRIGGER reject_review BEFORE INSERT ON result_reviews BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    with pytest.raises(Exception, match='synthetic failure'):
        store.complete_result(job['id'], {'messages': []}, 'Result', 1)
    assert store.job(job['id'])['status'] == 'queued'
    assert not store.job(job['id'])['result']
    store.write('DROP TRIGGER reject_review')
    engine.cancel(job['id'])
    store.complete_result(job['id'], {'messages': []}, 'Late result', 1)
    assert store.job(job['id'])['status'] == 'cancelled'
    assert not store.pending_results()


def test_legacy_explicit_finishes_recovered_once_without_reopening_reviewed_work(setup):
    store, engine = setup
    job = store.create_job('Existing answer', 'Technology')
    store.update(job['id'], status='completed', result='Previous response', messages=json.dumps({'messages': [], 'deliverable_file_ids': []}))
    restored = Store(store.path)
    assert restored.pending_results()[0]['summary'] == 'Previous response'
    review = restored.result_review(job['id'])
    restored.review_result(job['id'], review['revision'])
    assert not Store(store.path).pending_results()


def test_result_endpoints_require_session_token_and_current_revision(setup, monkeypatch):
    store, engine = setup
    monkeypatch.setattr(server, 'store', store)
    job = store.create_job('Create a response', 'Technology')
    review = finish(store, engine, job)
    client = TestClient(server.app)
    path = '/api/jobs/'+job['id']+'/result-review'
    assert client.get('/api/results').status_code == 401
    client.get('/')
    assert client.get('/api/results').json()[0]['revision'] == review['revision']
    assert client.get('/api/status').json()['ready_for_review'] == 1
    assert client.post(path, json={'revision': review['revision']}).status_code == 403
    headers = {'x-cos-token': server.TOKEN}
    assert client.post(path, json={'revision': 'stale'}, headers=headers).status_code == 400
    assert client.post(path, json={'revision': review['revision']}, headers=headers).status_code == 200
    assert client.get('/api/results').json() == []
    assert client.get('/api/status').json()['ready_for_review'] == 0


@pytest.mark.parametrize('kind', [None, 'triage', 'action_receipt'])
def test_routine_message_finishes_stay_in_history_not_review(setup, kind):
    store, engine = setup
    job = store.create_job('Handle an incoming FYI', 'Communications')
    assert finish(store, engine, job, 'No reply or owner action needed.', result_kind=kind) is None
    assert not store.pending_results()
    assert store.detail(job['id'])['result'] == 'No reply or owner action needed.'
    assert store.detail(job['id'])['result_review'] is None


@pytest.mark.parametrize('kind', ['strategy', 'generated_content'])
def test_explicit_work_returns_text_without_requiring_an_output_file(setup, kind):
    store, engine = setup
    job = store.create_job('Prepare original text for review', 'Communications')
    review = finish(store, engine, job, 'New draft or analysis', result_kind=kind)
    assert review['summary'] == 'New draft or analysis'
    assert review['file_ids'] == []
    assert len(Store(store.path).pending_results()) == 1


def test_action_receipt_overrides_previous_work_and_preserves_execution_history(setup):
    from cos.db import uid, now
    store, engine = setup
    job = store.create_job('Handle a conversation', 'Communications')
    original = finish(store, engine, job, 'An original draft', result_kind='generated_content')
    store.update(job['id'], status='queued')
    store.write('INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?,?)',
                (uid(), job['id'], 'delete_email', '{"id":"synthetic-message"}', 'high', 'Owner approved', 'executed', now(), '{"executed":true}'))
    assert finish(store, engine, job, 'Email deleted.', result_kind='action_receipt') is None
    assert not store.pending_results()
    assert store.one("SELECT status FROM approvals WHERE job_id=?", (job['id'],))['status'] == 'executed'
    assert store.job(job['id'])['result'] == 'Email deleted.'
    with pytest.raises(ValueError):
        store.review_result(job['id'], original['revision'])


def test_legacy_queue_reconciliation_hides_triage_and_deletion_without_owner_review(setup):
    from cos.db import uid, now
    store, engine = setup
    routine = store.create_job('Message triage', 'Communications')
    deletion = store.create_job('Delete an email', 'Communications')
    plan = store.create_job('Prepare a strategy', 'Strategy')
    originals = {}
    for job in (routine, deletion, plan):
        store.update(job['id'], status='completed', result='Saved result', messages=json.dumps({'messages': [], 'deliverable_file_ids': []}))
        revision = uid()
        originals[job['id']] = revision
        store.write('INSERT INTO result_reviews(job_id,revision,summary,file_ids,completed) VALUES(?,?,?,?,?)',
                    (job['id'], revision, 'Saved result', '[]', now()))
    restored = Store(store.path)
    assert [r['id'] for r in restored.pending_results()] == [plan['id']]
    assert restored.result_review(plan['id'])['revision'] == originals[plan['id']]
    for job in (routine, deletion):
        assert restored.job(job['id'])['result'] == 'Saved result'
        record = restored.one('SELECT * FROM result_reviews WHERE job_id=?', (job['id'],))
        assert record['eligible'] == 0 and record['reviewed'] is None
    assert not restored.rows("SELECT * FROM events WHERE kind='result_reviewed'")
    assert len(Store(store.path).pending_results()) == 1


def test_delegated_content_returns_once_but_delegated_receipts_do_not(setup):
    store, engine = setup
    parent = store.create_job('Handle a request from a conversation', 'Communications')
    child = store.create_job('Draft strategy', 'Strategy', parent_id=parent['id'])
    finish(store, engine, child, 'A completed strategy', result_kind='strategy')
    assert not store.pending_results()
    assert finish(store, engine, parent, 'Here is the delegated strategy')
    other = store.create_job('Perform an operation', 'Communications')
    action_child = store.create_job('Update an external record', 'Strategy', parent_id=other['id'])
    finish(store, engine, action_child, 'Record updated', result_kind='action_receipt')
    assert finish(store, engine, other, 'The operation finished') is None
    assert [r['id'] for r in store.pending_results()] == [parent['id']]


def test_receipts_do_not_queue_even_for_strategy_organization(setup):
    store, engine = setup
    job = store.create_job('Update a ticket', 'Strategy')
    assert finish(store, engine, job, 'Ticket updated', result_kind='action_receipt') is None
    assert not Store(store.path).pending_results()


def test_generated_communication_document_is_reviewable_for_legacy_finish(setup):
    store, engine = setup
    job = store.create_job('Prepare a document', 'Communications')
    engine.provider = Scripted(perform('write_file', {'path': 'draft.txt', 'content': 'Newly drafted text'}),
                               perform('ask_user', {'question': 'Confirm a detail'}))
    engine.run(job['id'])
    file_id = store.one("SELECT id FROM files WHERE job_id=? AND name='draft.txt'", (job['id'],))['id']
    engine.answer(job['id'], 'Use the original detail')
    assert finish(store, engine, job, 'Draft ready', [file_id])
    assert len(store.pending_results()) == 1


def test_filtered_status_count_matches_inbox(setup, monkeypatch):
    store, engine = setup
    monkeypatch.setattr(server, 'store', store)
    routine = store.create_job('Summarize an FYI', 'Communications')
    finish(store, engine, routine, result_kind='triage')
    work = store.create_job('Prepare a plan', 'Strategy')
    finish(store, engine, work, result_kind='strategy')
    client = TestClient(server.app)
    client.get('/')
    assert client.get('/api/status').json()['ready_for_review'] == 1
    assert [r['id'] for r in client.get('/api/results').json()] == [work['id']]


def test_legacy_routed_operation_without_generated_output_is_not_reviewable(setup):
    from cos.db import uid, now
    store, engine = setup
    job = store.create_job('Update a ticket', 'Strategy')
    store.write('INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?,?)',
                (uid(), job['id'], 'jira_write', '{}', 'low', 'Routine update', 'executed', now(), '{}'))
    assert finish(store, engine, job, 'Ticket updated') is None
    assert not Store(store.path).pending_results()
