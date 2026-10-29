import json

import pytest

from cos import recovery
from cos.briefs import detail_brief, unresolved_action, update_brief
from cos.db import Store
from cos.engine import Engine


def perform(action, args):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{
        'id': action, 'type': 'function', 'function': {'name': 'perform',
        'arguments': json.dumps({'action': action, 'arguments': args})}}]}


@pytest.fixture
def imported(tmp_path):
    store = Store(tmp_path/'attention.db')
    job = store.create_job('Historical topic import', 'Communications')
    store.update(job['id'], messages=json.dumps({'messages': [], 'historical_import': True}))
    store.write("INSERT INTO teams_topics VALUES('topic','chat','Audit')")
    store.write("INSERT INTO communication_threads VALUES('teams','topic',?)", (job['id'],))
    return store, store.job(job['id'])


def source(store, text, kind='message', topic='topic', id='source'):
    store.write('INSERT INTO teams_topic_messages VALUES(?,?,?,?)',
                ('chat', id, topic, json.dumps({'text': text, 'record_kind': kind})))


def decision(**changes):
    return dict(context='Audit result', status_summary='Static audit complete',
                next_step='Review the result and any deliverables.', response_needed='no',
                response_reason='No response requested', action_needed='no',
                action_reason='Completed work has no outstanding obligation', **changes)


@pytest.mark.parametrize('kind', ['brief', 'outcome', 'review_history', 'returned_work'])
def test_model_history_cannot_establish_historical_owner_obligation(imported, kind):
    store, job = imported
    text = 'Review the result and any deliverables.'
    source(store, text, kind)
    args = decision()
    args.update(action_needed='yes', action_reason='Prior brief says review remains',
                owner_obligation_source_id='source', owner_obligation_evidence=text)
    with pytest.raises(ValueError, match='original message or owner feedback'):
        update_brief(store, job, args)
    assert not store.one('SELECT 1 FROM job_briefs WHERE job_id=?', (job['id'],))


@pytest.mark.parametrize('kind', ['message', 'owner_feedback'])
@pytest.mark.parametrize('flag', ['action_needed', 'response_needed'])
def test_real_outstanding_owner_decision_is_preserved(imported, kind, flag):
    store, job = imported
    text = 'Joshua, choose which release should receive the fix.'
    source(store, text, kind)
    args = decision()
    args.update({flag: 'yes'}, requested_action='Choose the release to fix',
                owner_obligation_source_id='source', owner_obligation_evidence=text)
    brief = update_brief(store, job, args)
    assert brief[flag] == 'yes'
    if flag == 'action_needed':
        assert unresolved_action(store, job) == 'Choose the release to fix'


@pytest.mark.parametrize('topic,excerpt', [('other-topic', 'Choose a release'), ('topic', 'Invented assignment')])
def test_evidence_must_be_exact_and_belong_to_current_topic(imported, topic, excerpt):
    store, job = imported
    source(store, 'Choose a release', topic=topic)
    args = decision()
    args.update(action_needed='yes', owner_obligation_source_id='source', owner_obligation_evidence=excerpt)
    with pytest.raises(ValueError, match='Historical owner attention requires'):
        update_brief(store, job, args)


def test_completed_no_action_brief_with_generic_review_stays_completed(imported):
    store, job = imported
    source(store, json.dumps({'action_needed': 'no', 'next_step': 'Review the result and any deliverables.'}), 'brief')
    update_brief(store, job, decision())
    class Provider:
        calls = 0
        def complete(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return perform('update_brief', {'action_needed': 'yes', 'action_reason': 'Retained brief requests owner review'}), {}
            if self.calls == 2:
                assert 'Historical owner attention requires' in messages[-1]['content']
                return perform('update_brief', {'action_needed': 'no', 'response_needed': 'no'}), {}
            return perform('finish', {'summary': 'Audit complete; optional review only.'}), {}
    engine = Engine(store, Provider())
    try:
        engine.run(job['id'])
    finally:
        engine.pool.shutdown()
    result = store.detail(job['id'])
    assert result['status'] == 'completed'
    assert result['question'] == ''
    assert result['brief']['action_needed'] == 'no'
    assert 'optional review' in result['brief']['next_step']
    assert not store.one("SELECT 1 FROM events WHERE job_id=? AND kind='followup'", (job['id'],))


def test_finish_revalidates_preexisting_unsupported_historical_action(imported):
    store, job = imported
    brief = decision()
    brief.update(action_needed='yes', action_reason='Retained brief requests review')
    store.write('INSERT INTO job_briefs VALUES(?,?,?)', (job['id'], json.dumps(brief), 'now'))
    with pytest.raises(ValueError, match='Historical owner attention requires'):
        unresolved_action(store, job)


@pytest.mark.parametrize('followup', ['', 'Review the result and any deliverables.'])
def test_finish_does_not_invent_generic_owner_followup(tmp_path, followup):
    store = Store(tmp_path/'question.db')
    job = store.create_job('Triage this incoming conversation', 'Communications')
    update_brief(store, job, {'action_needed': 'yes', 'action_reason': followup})
    with pytest.raises(ValueError, match='specific missing information or owner decision'):
        unresolved_action(store, job)


def test_recovered_brief_validation_preserves_substantive_status(imported):
    store, job = imported
    update_brief(store, job, decision())
    state = {}
    recovery.record(store, job['id'], state, 'update_brief', {}, 'Invalid summary field length.', error=True)
    recovery.record(store, job['id'], state, 'update_brief', {'email_excerpt': 'Audit mapped the workflow'}, {})
    brief = detail_brief(store, job)
    assert brief['status_summary'] == 'Static audit complete'
    assert 'continuing' not in brief['status_summary']
    assert state['active_errors'] == {}
    assert store.one("SELECT 1 FROM events WHERE job_id=? AND kind='tool_recovered'", (job['id'],))


@pytest.mark.parametrize('unknown', [False, True])
def test_import_keeps_specific_owner_decision_or_evidence_gap_in_attention(imported, unknown):
    store, job = imported
    text = 'Joshua, choose which release should receive the fix.'
    source(store, text)
    args = decision()
    if unknown:
        args.update(action_needed='unknown', uncertainties='The referenced release assignment is missing. Who owns the fix?')
        expected = args['uncertainties']
    else:
        args.update(action_needed='yes', requested_action='Choose the release to fix',
                    owner_obligation_source_id='source', owner_obligation_evidence=text)
        expected = args['requested_action']
    update_brief(store, job, args)
    class Provider:
        def complete(self, messages, **kwargs):
            assert 'owner_obligation_source_id' in json.dumps(messages)
            return perform('finish', {'summary': 'Release decision remains unresolved.'}), {}
    engine = Engine(store, Provider())
    try:
        engine.run(job['id'])
    finally:
        engine.pool.shutdown()
    result = store.job(job['id'])
    assert result['status'] == 'waiting_input'
    assert result['question'] == 'Follow-up remains: ' + expected


def test_ask_user_rejects_generic_review_without_stopping_worker(tmp_path):
    store = Store(tmp_path/'ask.db')
    job = store.create_job('Prepare a report', 'Strategy')
    class Provider:
        calls = 0
        def complete(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return perform('ask_user', {'question': 'Review the result and any deliverables.'}), {}
            assert 'specific missing information or owner decision' in messages[-1]['content']
            return perform('finish', {'summary': 'Report complete.'}), {}
    engine = Engine(store, Provider())
    try:
        engine.run(job['id'])
    finally:
        engine.pool.shutdown()
    assert store.job(job['id'])['status'] == 'completed'
    assert not store.one("SELECT 1 FROM events WHERE job_id=? AND kind='question'", (job['id'],))


@pytest.mark.parametrize('supported,pending', [(False, False), (True, False), (False, True)])
def test_startup_reassesses_only_unsupported_imported_followups(imported, supported, pending):
    store, job = imported
    brief = decision()
    brief.update(action_needed='yes', action_reason='Retained brief requests review')
    if supported:
        text = 'Joshua, choose a release.'
        source(store, text)
        brief.update(owner_obligation_source_id='source', owner_obligation_evidence=text)
    store.write('INSERT INTO job_briefs VALUES(?,?,?)', (job['id'], json.dumps(brief), 'now'))
    store.update(job['id'], status='waiting_input', question='Follow-up remains: Review the result')
    if pending:
        store.write("INSERT INTO approvals(id,job_id,tool,args,status,created) VALUES(?,?,?,?,?,?)",
                    ('pending', job['id'], 'send_teams', '{}', 'pending', 'now'))
    engine = Engine(store)
    try:
        engine.recover_historical_attention()
        first = store.job(job['id'])
        engine.recover_historical_attention()
    finally:
        engine.pool.shutdown()
    assert first['status'] == ('waiting_input' if supported or pending else 'queued')
    assert store.job(job['id'])['status'] == first['status']
    assert first['question'] == ('Follow-up remains: Review the result' if supported or pending else '')
