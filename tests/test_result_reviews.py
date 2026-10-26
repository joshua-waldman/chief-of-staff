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


def finish(store, engine, job, summary='Finished response', file_ids=None):
    engine.provider = Scripted(perform('finish', {'summary': summary, 'file_ids': file_ids or []}))
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
