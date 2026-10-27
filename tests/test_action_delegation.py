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


def research_proposal():
    return {'tasks': [proposal()['tasks'][0]]}


def test_research_starts_without_approval_and_returns_results(store):
    engine = Engine(store, Scripted(perform('delegate', research_proposal())))
    job = store.create_job('Research this email request', 'Communications')
    assert run(engine, job)['status'] == 'waiting_children'
    assert not store.rows("SELECT * FROM approvals WHERE status='pending'")
    child = store.rows('SELECT * FROM jobs WHERE parent_id=?', (job['id'],))[0]
    assert json.loads(child['messages'])['research_only']
    engine.provider = Scripted(perform('finish', {'summary': 'Sourced findings', 'result_kind': 'strategy'}))
    assert run(engine, child)['status'] == 'completed'
    engine.release_parents()
    assert store.job(job['id'])['status'] == 'queued'
    engine.provider = Scripted(perform('send_teams', {'chat_id': 'source', 'body': 'Findings ready'}))
    assert run(engine, job)['status'] == 'awaiting_approval'


def test_research_redelegates_without_approval_but_external_work_requires_it(store):
    engine = Engine(store, Scripted(perform('delegate', {'tasks': [proposal()['tasks'][1]]})))
    job = store.create_job('Research options', 'Technology')
    assert run(engine, job)['status'] == 'waiting_children'
    assert not store.rows("SELECT * FROM approvals WHERE status='pending'")
    child = store.rows('SELECT * FROM jobs WHERE parent_id=?', (job['id'],))[0]
    assert json.loads(child['messages'])['research_only']
    engine.provider = Scripted(perform('jira_write', {'operation': 'comment', 'issue': 'EX-1', 'body': 'Findings'}))
    assert run(engine, child)['status'] == 'awaiting_approval'
    assert store.one("SELECT status FROM approvals WHERE tool='jira_write'")['status'] == 'pending'


def test_research_policy_does_not_bypass_external_gates():
    assert not classify('delegate', research_proposal(), 'Communications').approval
    assert classify('delegate', proposal(), 'Communications').approval
    for action in ['jira_write', 'mcp_call', 'browser_action', 'send_email', 'apply_changes', 'run_command', 'create_feature_branch']:
        assert classify(action, {}, 'Self-Improvement', research_only=True).approval
    for action in ['read_file', 'fetch_url', 'write_file', 'create_document', 'create_presentation']:
        assert not classify(action, {}, 'Technology', research_only=True).approval


def test_recover_only_obsolete_research_stops(store):
    engine = Engine(store, Scripted())
    old = store.create_job('TP DB schema research', 'Technology')
    store.update(old['id'], status='waiting_input', question='Has the exact-action delegation approval for this TP DB schema/ETL research been granted? I will not access sources or begin research until it is confirmed through the approval workflow.')
    substantive = store.create_job('Research', 'Technology')
    store.update(substantive['id'], status='waiting_input', question='Which database should I research?')
    uncertain = store.create_job('Research', 'Technology')
    store.update(uncertain['id'], status='waiting_input', question=store.job(old['id'])['question'])
    store.write("INSERT INTO approvals(id,job_id,tool,status) VALUES('uncertain',?,'browser_action','uncertain')", (uncertain['id'],))
    pending = store.create_job('Research email', 'Communications')
    state = {'messages': [], 'waiting_call': 'legacy-call', 'waiting_action': 'legacy-approval'}
    store.update(pending['id'], status='awaiting_approval', messages=json.dumps(state))
    store.write("INSERT INTO approvals(id,job_id,tool,args,status) VALUES('legacy-approval',?,'delegate',?,'pending')", (pending['id'], json.dumps(research_proposal())))
    engine.recover_research_approvals()
    assert store.job(old['id'])['status'] == 'queued'
    assert store.job(old['id'])['question'] == ''
    assert json.loads(store.job(old['id'])['messages'])['research_only']
    assert store.job(substantive['id'])['status'] == 'waiting_input'
    assert store.job(uncertain['id'])['status'] == 'waiting_input'
    assert store.job(pending['id'])['status'] == 'queued'
    assert store.one("SELECT status FROM approvals WHERE id='legacy-approval'")['status'] == 'superseded'
    assert not store.rows('SELECT * FROM jobs WHERE parent_id=?', (pending['id'],))
    engine.recover_research_approvals()
    assert len(store.rows("SELECT * FROM events WHERE kind='continuation'")) == 2
