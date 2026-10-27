import json
import pytest
from cos.briefs import TIMING_POLICY, update_brief
from cos.db import Store


def test_inferred_urgency_survives_list_and_detail_without_deadline(tmp_path, monkeypatch):
    from cos import server
    store = Store(tmp_path / 'timing.db')
    monkeypatch.setattr(server, 'store', store)
    job = store.create_job('Review release decision', 'Technology')
    update_brief(store, job, dict(time_sensitive='yes', timing_reason='Assessment: team is blocked on owner approval.', deadline_at=''))
    listed = next(row for row in server.jobs() if row['id'] == job['id'])
    assert listed['time_sensitive'] == 'yes'
    assert listed['deadline_at'] == ''
    assert 'blocked' in listed['timing_reason']
    assert store.detail(job['id'])['brief']['time_sensitive'] == 'yes'
    update_brief(store, job, dict(time_sensitive='no', timing_reason='Blocker resolved.', deadline_at=''))
    assert server.jobs()[0]['time_sensitive'] == 'no'


@pytest.mark.parametrize('fields', [
    dict(time_sensitive='urgent'), dict(time_sensitive='yes'),
    dict(deadline_at='tomorrow'), dict(deadline_at='2026-10-27'),
    dict(deadline_at='2026-10-27T12:00:00'),
])
def test_invalid_timing_does_not_replace_saved_brief(tmp_path, fields):
    store = Store(tmp_path / 'invalid.db')
    job = store.create_job('Request', 'Technology')
    update_brief(store, job, dict(context='Preserve this'))
    with pytest.raises(ValueError):
        update_brief(store, job, fields)
    saved = json.loads(store.one('SELECT body FROM job_briefs WHERE job_id=?', (job['id'],))['body'])
    assert saved == {'context':'Preserve this'}


def test_supported_deadline_and_policy_cover_both_execution_paths(tmp_path):
    from cos.message_triage import PROMPT
    store = Store(tmp_path / 'deadline.db')
    job = store.create_job('Request', 'Technology')
    brief = update_brief(store, job, dict(deadline_at='2026-10-27T12:00:00-05:00'))
    assert brief['deadline_at'].endswith('-05:00')
    assert TIMING_POLICY in PROMPT
    assert 'even when no explicit timeline' in TIMING_POLICY


def test_agent_receives_contextual_urgency_policy_and_saves_assessment(tmp_path, monkeypatch):
    from cos import config, tools
    from cos.engine import Engine
    from tests.test_workflows import perform
    monkeypatch.setattr(tools, 'DATA', tmp_path / 'data')
    monkeypatch.setattr(config, 'DATA', tmp_path / 'data')
    store = Store(tmp_path / 'agent.db')
    job = store.create_job('The release team cannot proceed until I decide.', 'Technology')
    class Provider:
        calls = 0
        def complete(self, messages, **kwargs):
            self.calls += 1
            assert any(TIMING_POLICY in (m.get('content') or '') for m in messages if m['role'] == 'system')
            if self.calls == 1:
                return perform('update_brief', dict(time_sensitive='yes', timing_reason='Assessment: delaying the decision blocks release work.', deadline_at='')), {}
            return perform('ask_user', dict(question='Which release option do you prefer?')), {}
    provider = Provider()
    engine = Engine(store, provider)
    try:
        engine.run(job['id'])
        assert store.job(job['id'])['status'] == 'waiting_input'
        brief = store.detail(job['id'])['brief']
        assert brief['time_sensitive'] == 'yes'
        assert brief['deadline_at'] == ''
        assert 'blocks release' in brief['timing_reason']
    finally:
        engine.stop()
