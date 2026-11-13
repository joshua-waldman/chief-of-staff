import json

import pytest

from test_workflows import store, Scripted, perform, run
from cos.engine import Engine
from cos.policy import classify
from cos.reviews import submit
from cos.tools import Tools, allowed_job_action


def legacy_pending(engine, job):
    from cos import engine as module
    from cos.policy import Decision
    original = module.classify
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(module, 'classify', lambda action, args, **kwargs:
                      Decision('high', True, 'Legacy delegation gate') if action == 'delegate'
                      else original(action, args, **kwargs))
        run(engine, job)


def proposal():
    return {'tasks': [
        {'org': 'Technology', 'request': 'Research the requested options; return sources and a comparison.'},
        {'org': 'Strategy', 'request': 'Prepare a plan with milestones and acceptance criteria.'},
    ]}


def test_communications_delegation_policy_is_contextual():
    assert not classify('delegate', {}, org='Communications').approval
    assert not classify('delegate', {}, org='Technology').approval


def test_approved_delegation_waits_for_results_and_separate_send_review(store):
    engine = Engine(store, Scripted(perform('delegate', proposal())))
    job = store.create_job('Handle the work requested in this message', 'Communications')
    assert run(engine, job)['status'] == 'waiting_children'
    assert not store.rows("SELECT * FROM approvals WHERE status='pending'")
    children = store.rows('SELECT * FROM jobs WHERE parent_id=?', (job['id'],))
    assert len(children) == 2
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
    legacy_pending(engine, job)
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
    assert run(engine, job)['status'] == 'waiting_children'
    detail = store.detail(job['id'])
    assert detail['brief']['response_needed'] == 'no'
    assert detail['brief']['action_needed'] == 'yes'
    assert len(detail['children']) == 2


def test_pending_delegation_survives_restart_without_starting_children(store):
    engine = Engine(store, Scripted(perform('delegate', proposal())))
    job = store.create_job('Investigate this request', 'Communications')
    legacy_pending(engine, job)
    restarted = Engine(store, Scripted())
    restarted.start()
    try:
        assert store.job(job['id'])['status'] == 'queued'
        assert store.one("SELECT status FROM approvals WHERE tool='delegate'")['status'] == 'superseded'
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
    assert not classify('delegate', proposal(), 'Communications').approval
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


def source_proposal():
    return {'tasks': [{'org': 'Communications', 'source_only': True,
                       'request': 'Read the October 1 spec extraction mail thread and attachments; return IDs, dates and exact quotes.'}]}


def at_depth_limit(store):
    job = store.create_job('Research spec extraction', 'Technology')
    for _ in range(3):
        job = store.create_job('Investigate the mail chronology', 'Technology', parent_id=job['id'])
    return job


def test_mail_source_handoff_at_depth_limit_returns_evidence(store, monkeypatch):
    engine = Engine(store, Scripted(perform('delegate', source_proposal())))
    parent = at_depth_limit(store)
    assert run(engine, parent)['status'] == 'waiting_children'
    child = store.rows('SELECT * FROM jobs WHERE parent_id=?', (parent['id'],))[0]
    state = json.loads(child['messages'])
    assert state['source_only'] and state['research_only']
    assert not store.rows("SELECT * FROM approvals WHERE status='pending'")
    calls = []

    def mail_skill(skill, script, args):
        calls.append((skill, script, args))
        return {'id': 'mail-1', 'received': '2026-10-01', 'body': 'Extraction is ready.'}

    monkeypatch.setattr('cos.tools.run_skill', mail_skill)
    monkeypatch.setattr('cos.tools.communication', lambda action, args: calls.append((action, args)) or {'files': []})
    evidence = engine.tools.execute(child, 'write_file', {
        'path': 'evidence.txt', 'content': 'mail-1, October 1: Extraction is ready.'})
    engine.provider = Scripted(
        perform('search_mail', {'query': 'spec extraction'}),
        perform('read_mail', {'id': 'mail-1'}),
        perform('mail_attachments', {'id': 'mail-1'}),
        perform('finish', {'summary': 'mail-1, October 1: "Extraction is ready."',
                           'result_kind': 'strategy', 'file_ids': [evidence['file_id']]}))
    assert run(engine, child)['status'] == 'completed'
    assert len(calls) == 3
    engine.release_parents()
    resumed = store.job(parent['id'])
    assert resumed['status'] == 'queued'
    assert 'Extraction is ready.' in resumed['messages']
    returned = store.rows('SELECT * FROM files WHERE job_id=?', (parent['id'],))
    assert len(returned) == 1
    assert returned[0]['name'] == 'evidence.txt'
    engine.provider = Scripted(perform('finish', {'summary': 'Verified chronology', 'result_kind': 'strategy'}))
    assert run(engine, resumed)['status'] == 'completed'


@pytest.mark.parametrize('action', ['delegate', 'send_email', 'reply_email', 'send_teams',
                                  'delete_email', 'mark_email_read', 'mark_teams_read',
                                  'mcp_call', 'jira_write', 'calendar_rsvp'])
def test_source_reader_cannot_delegate_or_mutate(store, action):
    engine = Engine(store, Scripted())
    parent = at_depth_limit(store)
    engine.delegate(parent, source_proposal())
    child = store.rows('SELECT * FROM jobs WHERE parent_id=?', (parent['id'],))[0]
    assert not allowed_job_action(child, action)
    with pytest.raises(ValueError, match='lacks the capability'):
        Tools(store).execute(child, action, {})
    with pytest.raises(ValueError, match='cannot delegate'):
        engine.delegate(child, source_proposal())


def test_depth_exception_is_only_for_valid_source_readers(store):
    engine = Engine(store, Scripted())
    parent = at_depth_limit(store)
    with pytest.raises(ValueError, match='depth limit'):
        engine.delegate(parent, research_proposal())
    with pytest.raises(ValueError, match='depth limit'):
        engine.delegate(parent, {'tasks': source_proposal()['tasks'] + research_proposal()['tasks']})
    for flag in ['true', 1, None]:
        task = {**source_proposal()['tasks'][0], 'source_only': flag}
        with pytest.raises(ValueError, match='boolean'):
            engine.delegate(parent, {'tasks': [task]})
    with pytest.raises(ValueError, match='must use Communications'):
        engine.delegate(parent, {'tasks': [{**source_proposal()['tasks'][0], 'org': 'Strategy'}]})
    deeper = store.create_job('Too deep', 'Technology', parent_id=parent['id'])
    with pytest.raises(ValueError, match='depth limit'):
        engine.delegate(deeper, source_proposal())
    assert not store.rows('SELECT * FROM jobs WHERE parent_id=?', (deeper['id'],))


def test_communications_source_reads_are_autonomous_but_ordinary_work_is_not():
    assert not classify('delegate', source_proposal(), 'Communications').approval
    assert not classify('delegate', {'tasks': [{'org': 'Communications', 'request': 'Handle email'}]}, 'Communications').approval
