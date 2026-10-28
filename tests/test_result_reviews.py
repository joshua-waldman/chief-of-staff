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


def test_parent_review_resolves_original_child_file_links(setup, monkeypatch):
    store, engine = setup
    monkeypatch.setattr(server, 'store', store)
    parent = store.create_job('Coordinate plan', 'Strategy')
    child = store.create_job('Draft task list', 'Strategy', parent_id=parent['id'])
    output = engine.tools.execute(child, 'write_file', {'path': 'task-list.txt', 'content': 'Review these tasks'})
    finish(store, engine, parent, 'Review '+output['url'])
    files = store.detail(parent['id'])['result_review']['files']
    assert [(f['id'], f['name']) for f in files] == [(output['file_id'], 'task-list.txt')]
    client = TestClient(server.app)
    client.get('/')
    response = client.get(output['url'])
    assert response.status_code == 200
    assert response.content == b'Review these tasks'


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


def test_delegated_draft_and_files_survive_parent_triage_finish(setup):
    store, engine = setup
    parent = store.create_job('Triage conversation', 'Communications')
    wrapper = store.create_job('Coordinate draft', 'Communications', parent_id=parent['id'])
    child = store.create_job('Write strategy', 'Strategy', parent_id=wrapper['id'])
    output = engine.tools.execute(child, 'write_file', {'path': 'strategy.txt', 'content': 'Full strategy'})
    store.complete_result(child['id'], {'result_kind': 'generated_content', 'deliverable_file_ids': [output['file_id']]}, 'Full strategy draft', 1)
    store.complete_result(wrapper['id'], {'result_kind': 'triage', 'deliverable_file_ids': []}, 'Work returned', 1)
    review = finish(store, engine, parent, 'No further action needed.', result_kind='triage')
    assert 'Full strategy draft' in review['summary']
    assert review['file_ids'] == [output['file_id']]
    assert [r['id'] for r in store.pending_results()] == [parent['id']]
    assert store.detail(parent['id'])['result_review']['files'][0]['name'] == 'strategy.txt'
    assert store.job(parent['id'])['result'] == 'No further action needed.'
    restored = Store(store.path)
    assert restored.result_review(parent['id']) == review
    restored.review_result(parent['id'], review['revision'])
    assert not Store(store.path).pending_results()


def test_startup_repairs_missing_delegated_text_once_and_rejects_stale_ack(setup):
    store, engine = setup
    parent = store.create_job('Handle conversation', 'Communications')
    child = store.create_job('Prepare draft', 'Strategy', parent_id=parent['id'])
    store.complete_result(child['id'], {'result_kind': 'generated_content', 'deliverable_file_ids': []}, 'Draft for owner review', 1)
    review = finish(store, engine, parent, 'Draft completed.')
    # Reproduce the historical record: only the director summary was queued.
    store.write('UPDATE result_reviews SET summary=? WHERE job_id=?', ('Draft completed.', parent['id']))
    restored = Store(store.path)
    repaired = restored.result_review(parent['id'])
    assert 'Draft for owner review' in repaired['summary']
    assert repaired['revision'] != review['revision']
    with pytest.raises(ValueError):
        restored.review_result(parent['id'], review['revision'])
    assert Store(store.path).result_review(parent['id']) == repaired
    assert len(store.rows("SELECT * FROM events WHERE kind='result_review_repaired'")) == 1
    restored.review_result(parent['id'], repaired['revision'])
    assert not Store(store.path).pending_results()


def test_startup_does_not_reopen_already_reviewed_delegated_result(setup):
    store, engine = setup
    parent = store.create_job('Coordinate plan', 'Communications')
    child = store.create_job('Prepare plan', 'Strategy', parent_id=parent['id'])
    store.complete_result(child['id'], {'result_kind': 'strategy', 'deliverable_file_ids': []}, 'Child plan', 1)
    review = finish(store, engine, parent, 'Plan completed.')
    store.write('UPDATE result_reviews SET summary=? WHERE job_id=?', ('Plan completed.', parent['id']))
    store.review_result(parent['id'], review['revision'])
    restored = Store(store.path)
    assert restored.result_review(parent['id'])['summary'] == 'Plan completed.'
    assert not restored.pending_results()


def test_source_readers_and_action_receipts_do_not_leak_into_draft_review(setup):
    store, engine = setup
    parent = store.create_job('Coordinate draft', 'Communications')
    for state, text in [({'source_only': True, 'result_kind': 'generated_content'}, 'Source evidence only'),
                        ({'result_kind': 'action_receipt'}, 'Ticket updated')]:
        child = store.create_job(text, 'Technology', parent_id=parent['id'])
        store.complete_result(child['id'], {**state, 'deliverable_file_ids': []}, text, 1)
    assert finish(store, engine, parent, 'Routine triage', result_kind='triage') is None
    child = store.create_job('Draft', 'Strategy', parent_id=parent['id'])
    store.complete_result(child['id'], {'result_kind': 'generated_content', 'deliverable_file_ids': []}, 'Actual draft', 1)
    store.update(parent['id'], status='queued')
    review = finish(store, engine, parent, 'Draft completed.', result_kind='triage')
    assert 'Actual draft' in review['summary']
    assert 'Source evidence only' not in review['summary']
    assert 'Ticket updated' not in review['summary']


def test_later_receipt_does_not_requeue_reviewed_draft_but_child_revision_does(setup):
    store, engine = setup
    parent = store.create_job('Handle conversation', 'Communications')
    child = store.create_job('Prepare draft', 'Strategy', parent_id=parent['id'])
    state = {'result_kind': 'generated_content', 'deliverable_file_ids': []}
    store.complete_result(child['id'], state, 'First draft', 1)
    review = finish(store, engine, parent, 'Draft ready', result_kind='triage')
    store.review_result(parent['id'], review['revision'])
    store.update(parent['id'], status='queued')
    assert finish(store, engine, parent, 'Message sent', result_kind='action_receipt') is None
    assert not Store(store.path).pending_results()
    store.complete_result(child['id'], state, 'Revised draft', 2)
    store.update(parent['id'], status='queued')
    revised = finish(store, engine, parent, 'Revised draft ready', result_kind='triage')
    assert 'Revised draft' in revised['summary']
    assert 'First draft' not in revised['summary']
    assert len(Store(store.path).pending_results()) == 1


@pytest.mark.parametrize('status', ['queued', 'running', 'waiting_input', 'waiting_children', 'failed', 'blocked'])
def test_source_reassessment_keeps_saved_review_visible_and_acknowledges_only_snapshot(setup, monkeypatch, status):
    from cos.conversations import deliver
    store, engine = setup
    monkeypatch.setattr(server, 'store', store)
    job = store.create_job('Handle incoming conversation', 'Communications')
    child = store.create_job('Prepare strategy', 'Strategy', parent_id=job['id'])
    store.complete_result(child['id'], {'result_kind': 'generated_content', 'deliverable_file_ids': []}, 'Saved strategy draft', 1)
    original = finish(store, engine, job, 'Draft ready', result_kind='triage')
    store.write('INSERT INTO communication_inbox(job_id,body,created) VALUES(?,?,?)', (job['id'], 'New source activity', '2026-10-28T00:00:00+00:00'))
    assert deliver(store, job['id'])
    store.update(job['id'], status=status)
    restored = Store(store.path)
    assert [r['id'] for r in restored.pending_results()] == [job['id']]
    assert restored.detail(job['id'])['result_review'] == original | {'files': []}
    client = TestClient(server.app)
    client.get('/')
    assert client.get('/api/status').json()['ready_for_review'] == 1
    assert client.get('/api/results').json()[0]['revision'] == original['revision']
    store.review_result(job['id'], original['revision'])
    assert store.job(job['id'])['status'] == status
    assert not store.pending_results()


def test_unrelated_triage_completion_keeps_pending_draft_but_cancellation_hides_it(setup):
    store, engine = setup
    job = store.create_job('Draft content', 'Communications')
    original = finish(store, engine, job, 'The original draft', result_kind='generated_content')
    store.update(job['id'], status='queued')
    assert finish(store, engine, job, 'No new action from the new source', result_kind='triage') == original
    assert Store(store.path).result_review(job['id']) == original
    engine.cancel(job['id'])
    assert not store.pending_results()
    assert store.detail(job['id'])['result_review'] is None
    with pytest.raises(ValueError):
        store.review_result(job['id'], original['revision'])
