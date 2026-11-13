"""Waiting for an outside reply is separate from an actionable owner obligation."""
import json

import pytest

from cos.briefs import update_brief
from cos.conversations import deliver, save_source
from cos.db import Store, now
from cos.engine import Engine
from cos.message_triage import PERSONAL_ACTION_POLICY


def perform(action, arguments):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{
        'id': action, 'type': 'function', 'function': {
            'name': 'perform', 'arguments': json.dumps({
                'action': action, 'arguments': arguments})}}]}


@pytest.mark.parametrize('resumed', [False, True])
@pytest.mark.parametrize('independent_action', [False, True])
def test_waiting_reply_attention_and_later_reassessment(tmp_path, monkeypatch,
                                                      resumed, independent_action):
    from cos import server

    store = Store(tmp_path/'waiting.db')
    monkeypatch.setattr(server, 'store', store)
    store.write('''INSERT INTO commitments
        (id,owner,deliverable,scope,deadline_text,deadline_date,deadline_certainty,
         deadline_basis,status,fingerprint,version,created,updated)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        ('training', 'Joshua Waldman', 'Learn to use the evaluation hub', 'global',
         '', '', 'unknown', '', 'open', 'training', 1, now(), now()))
    commitment = store.one('SELECT * FROM commitments WHERE id=?', ('training',))
    other_attention = server.status()['needs_attention']
    job = store.create_job('Triage this incoming email conversation.', 'Communications')
    with store.connect() as c:
        save_source(c, job['id'], 'email', 'sent-request', 'owner@example.com',
                    'Training', now(), 'Kobe, please suggest a time for training.', True)
    if resumed:
        # Reproduce a persisted actionable classification from before the send.
        update_brief(store, job, dict(action_needed='yes', response_needed='no',
                                     requested_action='Contact Kobe for training'))
        store.update(job['id'], messages=json.dumps({'messages': [], 'source_revision': 1}))

    class Provider:
        calls = 0

        def complete(self, messages, **kwargs):
            self.calls += 1
            # Both compact triage and resumed workers must receive the rule.
            assert any(PERSONAL_ACTION_POLICY in m.get('content', '')
                       for m in messages if m.get('role') == 'system')
            if self.calls == 1:
                return perform('update_brief', dict(
                    context='Training request sent',
                    status_summary='Awaiting Kobe; training remains pending.',
                    next_step="Await Kobe's suggested time.",
                    action_needed='yes' if independent_action else 'no',
                    response_needed='no', response_reason='Request already sent',
                    action_reason='Choose a release independently of training' if independent_action
                    else 'Kobe must propose a time before the owner can act',
                    requested_action='Choose a release' if independent_action else '',
                )), {}
            return perform('finish', {'summary': 'Awaiting Kobe; training remains pending.'}), {}

    engine = Engine(store, Provider())
    try:
        engine.run(job['id'])
        result = store.job(job['id'])
        assert result['status'] == ('waiting_input' if independent_action else 'completed')
        assert server.status()['needs_attention'] == other_attention + int(independent_action)
        assert result['question'] == ('Follow-up remains: Choose a release'
                                      if independent_action else '')
        assert not store.rows("SELECT * FROM approvals WHERE job_id=? AND tool IN ('send_email','reply_email','send_teams','delegate')", (job['id'],))
        assert store.one('SELECT * FROM commitments WHERE id=?', ('training',)) == commitment

        # A reply reopens the same job and invalidates the previous no-action decision.
        store.write('INSERT INTO communication_inbox(job_id,body,created) VALUES(?,?,?)',
                    (job['id'], 'Kobe: Does Tuesday at 10 work for you?', now()))
        assert deliver(store, job['id'])
        updated = store.detail(job['id'])
        assert updated['status'] == 'queued'
        assert updated['brief']['action_needed'] == 'unknown'
        assert updated['brief']['response_needed'] == 'unknown'
        assert 'Does Tuesday at 10 work for you?' in store.job(job['id'])['messages']
        assert len(store.rows('SELECT id FROM jobs')) == 1
    finally:
        engine.pool.shutdown()
