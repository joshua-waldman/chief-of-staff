import hashlib

import pytest
from fastapi.testclient import TestClient

from cos import server
from cos.commitments import Commitments
from cos.db import Store


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path/'review.db')
    meeting = store.create_job('Meeting', 'Communications')
    transcript = '[00:01:00] Alex: I need to contact Casey to locate the evaluation dashboard and learn how to access or run it.'
    path = tmp_path/'meeting.txt'
    path.write_text(transcript, encoding='utf-8')
    store.write('INSERT INTO files VALUES(?,?,?,?,?,?)', ('meeting', meeting['id'], path.name, str(path), 'text/plain', 'now'))
    store.set_state('meeting_sources:'+meeting['id'], {'meeting': {'identity': 'meeting', 'source': 'https://example.com/meeting', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}})
    registry = Commitments(store)
    commitment = registry.record(meeting, {'owner': 'Alex', 'deliverable': 'Contact Casey to locate the evaluation dashboard and learn how to access or run it.', 'source_file_id': 'meeting', 'source_timestamp': '00:01:00', 'meeting_date': '2026-11-02', 'evidence': transcript})
    commitment = registry.review(commitment['id'], {'action': 'accept', 'expected_version': commitment['version']})
    job = store.create_job('Evaluation Hub Training — Casey', 'Communications')
    summary = '# Evaluation hub instructions\nCasey supplied instructions for accessing the evaluation hub, running tests and reading results.'
    store.complete_result(job['id'], {'result_kind': 'generated_content', 'deliverable_file_ids': []}, summary, 1)
    return store, registry, commitment, job


def context(store, job):
    return store.detail(job['id'])['result_review']


def selection(item, relationship='fulfills'):
    return {'commitment_id': item['id'], 'version': item['version'], 'relationship': relationship}


def confirm(store, job, item, relationship='fulfills'):
    review = context(store, job)
    store.review_result(job['id'], review['revision'], [selection(item, relationship)], review['commitments']['snapshot_fingerprint'])
    return review


def test_semantic_candidates_use_saved_content_and_never_close_work(env):
    store, registry, item, job = env
    review = context(store, job)
    match = review['commitments']['matches'][0]
    assert match['id'] == item['id']
    assert 'portal' in match['reason']
    assert 'evaluation' in match['excerpt'].lower()
    store.update(job['id'], result='Unrelated newer conversation text')
    assert context(store, job)['commitments'] == review['commitments']
    store.review_result(job['id'], review['revision'])
    assert registry.detail(item['id'])['status'] == 'open'
    assert not store.rows('SELECT * FROM review_commitment_links')


@pytest.mark.parametrize('relationship,status', [('fulfills', 'completed'), ('progress', 'open'), ('related', 'open')])
def test_owner_confirmation_retains_bidirectional_snapshot_across_restart(env, relationship, status):
    store, registry, item, job = env
    review = confirm(store, job, item, relationship)
    # Retrying the same request has one effect, even with the original version.
    store.review_result(job['id'], review['revision'], [selection(item, relationship)], review['commitments']['snapshot_fingerprint'])
    restored = Store(store.path)
    record = Commitments(restored).detail(item['id'])
    assert record['status'] == status
    assert record['version'] == item['version']+1
    evidence = record['evidence'][-1]
    assert evidence['reviewed']
    assert evidence['review_snapshot']['summary'] == review['summary']
    assert evidence['review_snapshot']['revision'] == review['revision']
    links = context(restored, job)['commitments']['links']
    assert links[0]['commitment_id'] == item['id']
    assert links[0]['relationship'] == relationship
    assert not restored.pending_results()
    assert len(restored.rows("SELECT * FROM events WHERE kind='result_reviewed'")) == 1
    # New review revisions do not rewrite evidence of the prior confirmation.
    restored.complete_result(job['id'], {'result_kind': 'generated_content', 'deliverable_file_ids': []}, 'A different report', 2)
    assert Commitments(restored).detail(item['id'])['evidence'][-1] == evidence


def test_stale_commitment_rolls_back_entire_review(env):
    store, registry, item, job = env
    review = context(store, job)
    registry.review(item['id'], {'action': 'edit', 'expected_version': item['version'], 'fields': {'scope': 'AVA'}})
    with pytest.raises(ValueError, match='Commitment changed'):
        store.review_result(job['id'], review['revision'], [selection(item)], review['commitments']['snapshot_fingerprint'])
    assert not store.result_review(job['id'])['reviewed']
    assert not store.rows('SELECT * FROM review_commitment_links')


def test_snapshot_revision_and_file_changes_are_rejected(env, tmp_path):
    store, registry, item, job = env
    path = tmp_path/'deck.txt'
    path.write_text('Evaluation hub access instructions', encoding='utf-8')
    store.write('INSERT INTO files VALUES(?,?,?,?,?,?)', ('deck', job['id'], path.name, str(path), 'text/plain', 'now'))
    state = {'result_kind': 'generated_content', 'deliverable_file_ids': ['deck']}
    store.complete_result(job['id'], state, 'Deck ready.', 2)
    review = context(store, job)
    assert review['commitments']['matches'][0]['id'] == item['id']
    path.write_text('Replaced file', encoding='utf-8')
    with pytest.raises(ValueError, match='files changed'):
        store.review_result(job['id'], review['revision'], [selection(item)], review['commitments']['snapshot_fingerprint'])
    path.unlink()
    with pytest.raises(ValueError, match='unavailable'):
        confirm(store, job, item)
    store.complete_result(job['id'], state, 'New review.', 3)
    with pytest.raises(ValueError, match='result has changed'):
        store.review_result(job['id'], review['revision'], [selection(item)], review['commitments']['snapshot_fingerprint'])
    assert not store.result_review(job['id'])['reviewed']
    assert registry.detail(item['id'])['status'] == 'open'


def test_multiple_choices_rollback_and_proposals_cannot_be_completed(env):
    store, registry, item, job = env
    review = context(store, job)
    with pytest.raises(ValueError):
        store.review_result(job['id'], review['revision'], [selection(item), {'commitment_id': 'missing', 'version': 1, 'relationship': 'related'}], review['commitments']['snapshot_fingerprint'])
    assert registry.detail(item['id'])['status'] == 'open'
    assert not store.rows('SELECT * FROM review_commitment_links')
    store.write("UPDATE commitments SET status='proposed' WHERE id=?", (item['id'],))
    with pytest.raises(ValueError, match='Confirm the proposed assignment'):
        confirm(store, job, item)
    confirm(store, job, item, 'progress')
    assert registry.detail(item['id'])['status'] == 'proposed'


def test_manual_choices_cover_unmatched_content_and_closed_items_are_excluded(env):
    store, registry, item, job = env
    store.complete_result(job['id'], {'result_kind': 'generated_content', 'deliverable_file_ids': []}, 'Completely different prose.', 2)
    review = context(store, job)
    assert not review['commitments']['matches']
    assert review['commitments']['choices'][0]['id'] == item['id']
    confirm(store, job, item, 'related')
    registry.review(item['id'], {'action': 'dismiss', 'expected_version': item['version']+1, 'note': 'No longer needed'})
    store.complete_result(job['id'], {'result_kind': 'generated_content', 'deliverable_file_ids': []}, 'Another report', 3)
    assert 'commitments' not in context(store, job)


def test_generic_words_and_conversation_title_alone_are_not_matches(env):
    store, _, _, job = env
    store.complete_result(job['id'], {'result_kind': 'generated_content', 'deliverable_file_ids': []}, 'We need to do it or run it once we can.', 2)
    assert not context(store, job)['commitments']['matches']


def test_linked_source_document_is_hashed_and_evidence_survives_merge(env, tmp_path):
    store, registry, item, job = env
    path = tmp_path/'source.txt'
    path.write_text('Evaluation hub access instructions.', encoding='utf-8')
    store.write('INSERT INTO files VALUES(?,?,?,?,?,?)', ('source', job['id'], path.name, str(path), 'text/plain', 'now'))
    store.complete_result(job['id'], {'result_kind': 'generated_content', 'deliverable_file_ids': []}, 'Read [instructions](/api/files/source).', 2)
    review = confirm(store, job, item, 'progress')
    evidence = registry.detail(item['id'])['evidence'][-1]
    assert evidence['review_snapshot']['files'][0]['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    # A second same-owner obligation can absorb this one without losing the
    # saved review provenance or its reverse link.
    from cos.db import uid
    target = uid()
    store.write('INSERT INTO commitments SELECT ?,owner,deliverable,scope,deadline_text,deadline_date,deadline_certainty,deadline_basis,status,merged_into,?,version,created,updated FROM commitments WHERE id=?', (target, uid(), item['id']))
    target_item = registry.detail(target)
    registry.review(item['id'], {'action': 'merge', 'expected_version': item['version']+1, 'target_id': target, 'target_version': target_item['version'], 'note': 'Same obligation'})
    merged = registry.detail(target)
    assert merged['evidence'][-1]['review_snapshot'] == evidence['review_snapshot']
    links = context(store, job)['commitments']['links']
    assert any(link['commitment_id'] == target for link in links)
    assert review['revision'] == evidence['review_snapshot']['revision']


def test_endpoint_requires_owner_auth_and_records_explicit_choice(env, monkeypatch):
    store, registry, item, job = env
    monkeypatch.setattr(server, 'store', store)
    review = context(store, job)
    client = TestClient(server.app)
    client.get('/')
    payload = {'revision': review['revision'], 'commitments': [selection(item)], 'snapshot_fingerprint': review['commitments']['snapshot_fingerprint']}
    url = '/api/jobs/'+job['id']+'/result-review'
    assert client.post(url, json=payload).status_code == 403
    assert client.post(url, json=payload, headers={'x-cos-token': server.TOKEN}).status_code == 200
    assert registry.detail(item['id'])['status'] == 'completed'


@pytest.mark.parametrize('choices', ['bad', {}, [{'commitment_id': 'x'}], [{'commitment_id': [], 'version': 1, 'relationship': 'fulfills'}]])
def test_malformed_selections_do_not_acknowledge_review(env, choices):
    store, _, _, job = env
    review = context(store, job)
    with pytest.raises(ValueError):
        store.review_result(job['id'], review['revision'], choices, review['commitments']['snapshot_fingerprint'])
    assert not store.result_review(job['id'])['reviewed']
