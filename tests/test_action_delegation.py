import json

import pytest

from test_workflows import store, Scripted, perform, run
from cos.engine import Engine
from cos.policy import classify
from cos.reviews import submit


def proposal():
    return {'tasks': [
        {'org': 'Technology', 'request': 'Research the requested options; return sources and a comparison.'},
        {'org': 'Strategy', 'request': 'Prepare a plan with milestones and acceptance criteria.'},
    ]}


def test_communications_delegation_policy_is_contextual():
    assert classify('delegate', {}, org='Communications').approval
    assert not classify('delegate', {}, org='Technology').approval


def test_approved_delegation_waits_for_results_and_separate_send_review(store):
    engine = Engine(store, Scripted(perform('delegate', proposal())))
    job = store.create_job('Handle the work requested in this message', 'Communications')
    assert run(engine, job)['status'] == 'awaiting_approval'
    assert not store.rows('SELECT * FROM jobs WHERE parent_id=?', (job['id'],))
    approval = store.rows("SELECT * FROM approvals WHERE status='pending'")[0]
    assert json.loads(approval['args']) == proposal()
    engine.decide(approval['id'], True)
    assert store.job(job['id'])['status'] == 'waiting_children'
    children = store.rows('SELECT * FROM jobs WHERE parent_id=?', (job['id'],))
    assert len(children) == 2
    with pytest.raises(ValueError):
        engine.decide(approval['id'], True)
    for child in children:
        engine.provider = Scripted(perform('finish', {'summary': 'Verified deliverable returned'}))
        assert run(engine, child)['status'] == 'completed'
    engine.release_parents()
    assert store.job(job['id'])['status'] == 'queued'
    engine.provider = Scripted(perform('send_teams', {'chat_id': 'source', 'body': 'Results ready'}))
    assert run(engine, job)['status'] == 'awaiting_approval'
    assert store.one("SELECT * FROM approvals WHERE tool='send_teams'")['status'] == 'pending'


@pytest.mark.parametrize('change', ['reject', 'revise', 'incoming'])
def test_rejected_or_stale_delegation_never_starts_work(store, change):
    engine = Engine(store, Scripted(perform('delegate', proposal())))
    job = store.create_job('Handle incoming requested work', 'Communications')
    run(engine, job)
    approval = store.rows("SELECT * FROM approvals WHERE status='pending'")[0]
    if change == 'reject':
        engine.decide(approval['id'], False)
    elif change == 'revise':
        submit(engine, job['id'], 'Only research; remove the planning task.')
        with pytest.raises(ValueError):
            engine.decide(approval['id'], True)
    else:
        store.write('INSERT INTO communication_inbox(job_id,body,created) VALUES(?,?,?)',
                    (job['id'], 'The request has changed', 'now'))
        with pytest.raises(ValueError, match='reassessment'):
            engine.decide(approval['id'], True)
    assert not store.rows('SELECT * FROM jobs WHERE parent_id=?', (job['id'],))


def test_no_reply_can_still_require_action(store):
    brief = {'context': 'Research request', 'status_summary': 'Research proposed',
             'next_step': 'Review delegation', 'response_needed': 'no',
             'response_reason': 'No acknowledgement requested', 'action_needed': 'yes',
             'action_reason': 'Sender requested a comparison',
             'requested_action': 'Research options', 'action_plan': 'Technology returns a sourced comparison'}
    engine = Engine(store, Scripted(perform('update_brief', brief), perform('delegate', proposal())))
    job = store.create_job('Triage this incoming email conversation. Research requested.', 'Communications')
    assert run(engine, job)['status'] == 'awaiting_approval'
    detail = store.detail(job['id'])
    assert detail['brief']['response_needed'] == 'no'
    assert detail['brief']['action_needed'] == 'yes'
    assert not detail['children']


def test_pending_delegation_survives_restart_without_starting_children(store):
    engine = Engine(store, Scripted(perform('delegate', proposal())))
    job = store.create_job('Investigate this request', 'Communications')
    run(engine, job)
    restarted = Engine(store, Scripted())
    restarted.start()
    try:
        assert store.job(job['id'])['status'] == 'awaiting_approval'
        assert not store.rows('SELECT * FROM jobs WHERE parent_id=?', (job['id'],))
    finally:
        restarted.stop()
