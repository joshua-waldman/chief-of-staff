"""Outcome reconciliation runs offline here; no mutation is ever retried."""
import json
from concurrent.futures import Future

import pytest

from cos import action_verification as verification, config, tools
from cos.communication_bridge import verify_action
from cos.db import Store, now
from cos.context import action_receipts
from cos.engine import Engine, session
from cos.local_checks import ActionNotExecuted
from tests.test_workflows import Scripted, perform


class InlinePool:
    def submit(self, fn, *args):
        fn(*args)
        return Future()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DATA', tmp_path)
    monkeypatch.setattr(tools, 'DATA', tmp_path)
    store = Store(tmp_path / 'jobs.db')
    engine = Engine(store, Scripted())
    engine.verification_pool.shutdown()
    engine.verification_pool = InlinePool()
    job = store.create_job('Handle communication', 'Communications')
    store.update(job['id'], status='blocked', question='Legacy manual verification question',
                 messages=json.dumps({'messages': [], 'waiting_action': 'action', 'waiting_call': 'call'}))
    store.write('INSERT INTO approvals(id,job_id,tool,args,status,created,result) VALUES(?,?,?,?,?,?,?)',
                ('action', job['id'], 'send_teams', json.dumps({'chat_id': 'chat', 'body': 'Draft'}), 'uncertain', now(), 'Timeout'))
    yield engine, store, job
    engine.verification_pool = engine.pool  # both shutdowns are idempotent
    engine.stop()


@pytest.mark.parametrize('outcome', ['executed', 'not_executed'])
def test_verified_outcome_resumes_with_evidence_and_no_user_assertion(env, monkeypatch, outcome):
    engine, store, job = env
    monkeypatch.setattr(verification, 'check', lambda *a: {'outcome': outcome, 'evidence': {'source_id': 'receipt'}})
    monkeypatch.setattr(engine.tools, 'execute', lambda *a: pytest.fail('Must never repeat the mutation'))
    verification.dispatch(engine)
    action = store.one('SELECT * FROM approvals')
    assert action['status'] == 'verified_' + outcome
    assert store.job(job['id'])['status'] == 'queued'
    assert store.job(job['id'])['question'] == ''
    state = session(store.job(job['id']))
    assert 'waiting_call' not in state and 'waiting_action' not in state
    assert 'source_id' in state['messages'][-1]['content']
    assert 'verified_by_user' not in state['messages'][-1]['content']
    assert json.loads(store.one('SELECT evidence FROM action_verifications')['evidence'])['outcome'] == outcome


def test_checks_are_bounded_and_unknown_outcome_is_never_replayed(env, monkeypatch):
    engine, store, job = env
    probes = []
    monkeypatch.setattr(verification, 'check', lambda *a: probes.append(True) or {'outcome': 'unknown', 'evidence': 'No receipt yet'})
    verification.dispatch(engine)
    verification.dispatch(engine)  # cooldown applies across scheduler ticks
    assert len(probes) == 1
    for _ in range(2):
        store.write("UPDATE action_verifications SET next_check=''")
        verification.dispatch(engine)
    assert len(probes) == verification.MAX_ATTEMPTS
    assert store.one('SELECT status FROM approvals')['status'] == 'verification_inconclusive'
    assert store.job(job['id'])['status'] == 'queued'
    # Changing the body does not permit an unconfirmed send to be retried.
    with pytest.raises(ValueError, match='Do not repeat'):
        verification.guard(store, job['id'], 'send_teams', {'chat_id': 'chat', 'body': 'Changed draft'})
    verification.guard(store, job['id'], 'send_teams', {'chat_id': 'other', 'body': 'Other work'})
    assert store.detail(job['id'], compact=True)['approvals'][0]['verification_evidence']
    assert 'verification_inconclusive' in json.dumps(action_receipts(store, job['id']))
    engine.provider = Scripted(perform('send_teams', {'chat_id': 'chat', 'body': 'Changed draft'}),
                               perform('finish', {'summary': 'Could not confirm delivery; no duplicate sent.'}))
    monkeypatch.setattr(engine.tools, 'execute', lambda *a: pytest.fail('No duplicate send'))
    engine.run(job['id'])
    assert store.job(job['id'])['status'] == 'completed'
    assert len(store.rows('SELECT * FROM approvals')) == 1


def test_cancel_during_check_never_resumes_job(env, monkeypatch):
    engine, store, job = env
    def check(*args):
        engine.cancel(job['id'])
        return {'outcome': 'executed', 'evidence': 'Source receipt'}
    monkeypatch.setattr(verification, 'check', check)
    verification.dispatch(engine)
    assert store.job(job['id'])['status'] == 'cancelled'
    assert store.one('SELECT status FROM approvals')['status'] == 'verified_executed'


def test_failed_checks_retain_attempts_across_engine_restart(env, monkeypatch):
    engine, store, job = env
    def fail(*a):
        raise TimeoutError('Read connection unavailable')
    monkeypatch.setattr(verification, 'check', fail)
    verification.dispatch(engine)
    replacement = Engine(store, Scripted())
    replacement.verification_pool.shutdown()
    replacement.verification_pool = InlinePool()
    try:
        verification.dispatch(replacement)
        assert store.one('SELECT attempts FROM action_verifications')['attempts'] == 1
        for _ in range(2):
            store.write("UPDATE action_verifications SET next_check=''")
            verification.dispatch(replacement)
        assert store.one('SELECT attempts FROM action_verifications')['attempts'] == 3
        assert store.job(job['id'])['status'] == 'queued'
    finally:
        replacement.verification_pool = replacement.pool
        replacement.stop()


def test_legacy_missing_mcp_server_recovers_without_external_call(env, monkeypatch):
    engine, store, job = env
    store.write("UPDATE approvals SET tool='mcp_call',args=?", (json.dumps({'tool': 'unknown'}),))
    monkeypatch.setattr('cos.integrations.communication', lambda *a: pytest.fail('No external call'))
    verification.dispatch(engine)
    assert store.one('SELECT status FROM approvals')['status'] == 'verified_not_executed'
    assert store.job(job['id'])['status'] == 'queued'
    with pytest.raises(ActionNotExecuted):
        engine.tools.execute(job, 'mcp_call', {'tool': 'unknown'})


def test_missing_mcp_argument_does_not_create_an_approval(env):
    engine, store, job = env
    other = store.create_job('Read imported tool', 'Technology')
    engine.provider = Scripted(perform('mcp_call', {'tool': 'read'}), perform('finish', {'summary': 'Missing connection identified.'}))
    engine.run(other['id'])
    assert store.job(other['id'])['status'] == 'completed'
    assert not store.rows('SELECT * FROM approvals WHERE job_id=?', (other['id'],))


class ReadGraph:
    def __init__(self, value):
        self.value, self.calls = value, []

    def get(self, path, params=None):
        self.calls.append((path, params))
        return self.value


def test_sent_mail_requires_exact_content_recipients_and_execution_time():
    started = '2026-11-02T12:00:00+00:00'
    message = {'id':'receipt', 'subject':'Review', 'body':{'contentType':'Text','content':'Approved'},
               'sentDateTime':'2026-11-02T12:00:02Z',
               'toRecipients':[{'emailAddress':{'address':'a@amd.com'}}]}
    payload = {'tool':'send_email','started':started, 'arguments':{'to':'a@amd.com','subject':'Review','body':'Approved'}}
    graph = ReadGraph({'value':[message]})
    assert verify_action(graph, payload)['outcome'] == 'executed'
    for field, value in [('sentDateTime', '2026-11-02T11:59:59Z'), ('subject','Different'),
                         ('body',{'contentType':'Text','content':'Different'}), ('toRecipients',[])]:
        graph.value = {'value':[{**message, field:value}]}
        assert verify_action(graph, payload)['outcome'] == 'unknown'
    graph.value = {'value':[]}
    assert verify_action(graph, payload)['outcome'] == 'unknown'
    assert verify_action(graph, {**payload, 'started':''})['outcome'] == 'unknown'
    assert all(path == '/me/mailFolders/sentitems/messages' for path, _ in graph.calls)


def test_read_flag_check_cannot_treat_unread_as_proof_of_nonexecution():
    graph = ReadGraph({'id':'mail', 'isRead':True})
    payload = {'tool':'mark_email_read', 'arguments':{'id':'mail'}}
    assert verify_action(graph, payload)['outcome'] == 'executed'
    graph.value['isRead'] = False
    assert verify_action(graph, payload)['outcome'] == 'unknown'


def test_response_state_does_not_prove_rsvp_notification_was_sent():
    graph = ReadGraph({'id':'event', 'responseStatus':{'response':'accepted','time':'2026-11-02T12:00:02Z'}})
    payload = {'tool':'calendar_rsvp','started':'2026-11-02T12:00:00Z',
               'arguments':{'id':'event','response':'accept','send_response':True}}
    assert verify_action(graph, payload)['outcome'] == 'unknown'
    payload['arguments']['send_response'] = False
    assert verify_action(graph, payload)['outcome'] == 'executed'


def test_same_action_is_not_checked_by_two_workers(env):
    engine, store, job = env
    tasks = []
    class QueuedPool:
        def submit(self, fn, *args):
            tasks.append((fn, args))
            return Future()
    engine.verification_pool = QueuedPool()
    verification.dispatch(engine)
    store.write("UPDATE action_verifications SET next_check=''")
    verification.dispatch(engine)
    assert len(tasks) == 1
    assert store.one('SELECT attempts FROM action_verifications')['attempts'] == 1


def test_patch_verification_uses_the_original_reviewed_hash(env):
    import hashlib
    engine, store, job = env
    root = tools.workspace(job['id'])
    source, target = root/'source.txt', root/'target.txt'
    source.write_bytes(b'reviewed change')
    target.write_bytes(b'original')
    args = {'_review_diffs':[{'source':str(source), 'target':str(target),
                             'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest()}]}
    store.write("UPDATE approvals SET tool='apply_changes',args=?", (json.dumps(args),))
    action = store.one('SELECT * FROM approvals')
    assert verification.check(store, job, action)['outcome'] == 'unknown'
    target.write_bytes(b'reviewed change')
    source.write_bytes(b'changed staging after execution')
    assert verification.check(store, job, action)['outcome'] == 'executed'
    target.write_bytes(source.read_bytes())
    assert verification.check(store, job, action)['outcome'] == 'unknown'


def test_teams_send_evidence_requires_owner_identity_and_execution_time(monkeypatch, tmp_path):
    import sys
    import types
    from cos import communication_bridge as bridge
    rows = [{'id':'message','from':'https://teams/contacts/8:orgid:owner',
             'composetime':'2026-11-02T12:00:02Z','content':'Approved','messagetype':'Text'}]
    class Skype:
        def list_messages(self, cid, page_size):
            assert cid == 'chat' and page_size == 100
            return rows, ''
    monkeypatch.setattr(bridge, 'skill_dir', lambda name:tmp_path)
    monkeypatch.setitem(sys.modules, 'skype_client', types.SimpleNamespace(SkypeClient=Skype))
    payload = {'tool':'send_teams','started':'2026-11-02T12:00:00Z','arguments':{'chat_id':'chat','body':'Approved'}}
    graph = ReadGraph({'id':'owner'})
    assert verify_action(graph, payload)['outcome'] == 'executed'
    rows[0]['from'] = 'https://teams/contacts/8:orgid:someone-else'
    assert verify_action(graph, payload)['outcome'] == 'unknown'
    rows[0]['from'] = 'https://teams/contacts/8:orgid:owner'
    rows[0]['composetime'] = '2026-11-02T11:59:59Z'
    assert verify_action(graph, payload)['outcome'] == 'unknown'


def test_legacy_manual_resolution_endpoint_cannot_assert_an_outcome(env, monkeypatch):
    from fastapi.testclient import TestClient
    from cos import server
    engine, store, job = env
    monkeypatch.setattr(server, 'store', store)
    monkeypatch.setattr(server, 'engine', engine)
    client = TestClient(server.app)
    client.cookies.set('cos_session', server.TOKEN)
    response = client.post('/api/approvals/action/resolve', json={'outcome':'executed'},
                           headers={'x-cos-token':server.TOKEN})
    assert response.status_code == 410
    assert store.one('SELECT status FROM approvals')['status'] == 'uncertain'


def test_bookkeeping_failure_uses_durable_success_receipt_without_replaying(env, monkeypatch):
    engine, store, job = env
    store.write("INSERT INTO action_verifications(action_id,started) VALUES('action',?)", (now(),))
    def bookkeeping_failure(*args):
        store.write("UPDATE approvals SET status='executed',result=?", (json.dumps({'message_id':'sent-message'}),))
        raise RuntimeError('Unable to update job context')
    monkeypatch.setattr(engine, '_complete_decision', bookkeeping_failure)
    monkeypatch.setattr('cos.integrations.communication', lambda *a:pytest.fail('Saved receipt is sufficient'))
    with pytest.raises(RuntimeError):
        engine._background_decision('action', True, job, {})
    verification.dispatch(engine)
    assert store.one('SELECT status FROM approvals')['status'] == 'verified_executed'
    assert store.job(job['id'])['status'] == 'queued'
    receipt = engine.tools.execute(job, 'read_action_receipt', {'id':'action'})
    assert 'sent-message' in receipt['text'] and 'execution_receipt' in receipt['text']


def test_changed_staging_is_corrected_without_outcome_verification(env):
    import hashlib
    engine, store, job = env
    root = tools.workspace(job['id'])
    source, target = root/'source.txt', root/'target.txt'
    source.write_bytes(b'reviewed change')
    target.write_bytes(b'original')
    store.update(job['id'], org='Engineering', status='awaiting_approval')
    job = store.job(job['id'])
    args = {'changes':[{'source':str(source),'target':str(target),'expected_sha256':hashlib.sha256(target.read_bytes()).hexdigest()}]}
    args['_review_diffs'] = tools.prepare_changes(job,args)
    store.write("UPDATE approvals SET tool='apply_changes',status='pending',args=?", (json.dumps(args),))
    source.write_bytes(b'changed since approval')
    engine.decide('action',True)
    assert store.one('SELECT status FROM approvals')['status'] == 'failed_not_executed'
    assert target.read_bytes() == b'original'
    assert store.job(job['id'])['status'] == 'queued'
