import json
import pytest

from cos.db import Store
from cos.intake import EmailIntake, preferences, revision
from cos.knowledge import Knowledge
from cos.monitor import Monitor


def mail(id='m', sender='updates@example.com', subject='Weekly digest', thread='thread'):
    return {'id': id, 'conversationId': thread, 'from': {'emailAddress': {'address': sender}},
            'subject': subject, 'bodyPreview': 'Untrusted source text', 'hasAttachments': False}


class Classifier:
    def __init__(self, decide):
        self.decide = decide
        self.calls = []

    def complete(self, messages, **options):
        payload = json.loads(messages[-1]['content'])
        self.calls.append((messages, options))
        return {'content': json.dumps({'decisions': self.decide(payload)})}, {}


def ignore_all(payload):
    return [{'thread_id': t['thread_id'], 'ignore': True,
             'rule_ids': [payload['owner_preferences'][0]['id']], 'reason': 'Matches saved category ignore guidance.'}
            for t in payload['conversations']]


def setup(tmp_path, decide=ignore_all):
    store = Store(tmp_path/'intake.db')
    rule = Knowledge(store).save({'kind': 'preference', 'scope': 'Communications',
        'title': 'Digest handling', 'content': 'Ignore all weekly digests from updates@example.com; no work or reply is needed.'}, user=True)
    monitor = Monitor(store)
    provider = Classifier(decide)
    monitor.email_intake = EmailIntake(store, provider)
    return store, monitor, rule, provider


def process(monitor, emails):
    return monitor.process({'messages': [], 'emails': emails})


def test_generic_ignore_closes_without_agent_and_deduplicates(tmp_path):
    store, m, rule, provider = setup(tmp_path)
    assert process(m, [mail()]) == 1
    job = store.rows('SELECT * FROM jobs')[0]
    assert job['status'] == 'completed' and job['step'] == 0 and job['messages'] == '[]'
    assert rule['id'] in job['result']
    brief = store.detail(job['id'])['brief']
    assert brief['response_needed'] == brief['action_needed'] == 'no'
    assert brief['email_from'] == 'updates@example.com'
    assert process(m, [mail()]) == 0
    assert len(provider.calls) == 1  # No classification for already-seen mail.
    assert process(m, [mail('m2')]) == 0
    assert store.job(job['id'])['status'] == 'completed'
    assert len(store.rows('SELECT * FROM jobs')) == 1
    assert not store.rows('SELECT * FROM communication_inbox')


def test_batches_different_categories_and_passes_current_verbatim_guidance(tmp_path):
    store, m, rule, provider = setup(tmp_path)
    Knowledge(store).save({'kind': 'preference', 'scope': 'Engineering', 'title': 'Build success',
        'content': 'Successful nightly build notifications from ci@example.com need no attention.'}, user=True)
    process(m, [mail(thread='a'), mail('b', 'ci@example.com', 'Build succeeded', 'b')])
    assert len(provider.calls) == 1
    messages, options = provider.calls[0]
    payload = json.loads(messages[-1]['content'])
    assert len(payload['owner_preferences']) == 2
    assert rule['content'] in messages[-1]['content']
    assert options == {'role': 'triage', 'timeout': 8, 'attempts': 1, 'max_tokens': 2000}
    assert 'tools' not in options
    assert all(j['status'] == 'completed' for j in store.rows('SELECT * FROM jobs'))


@pytest.mark.parametrize('change', ["status='forgotten'", "status='suggested'", "source='task-outcome'", "source='agent'", "kind='task'"])
def test_inactive_or_unapproved_knowledge_never_classifies(tmp_path, change):
    store, m, rule, provider = setup(tmp_path)
    store.write('UPDATE knowledge SET '+change+' WHERE id=?', (rule['id'],))
    process(m, [mail()])
    assert not provider.calls
    assert store.rows('SELECT * FROM jobs')[0]['status'] == 'queued'


@pytest.mark.parametrize('change', ['forget', 'edit', 'add'])
def test_kb_change_during_inference_invalidates_ignore(tmp_path, change):
    store, m, rule, provider = setup(tmp_path)
    def decide(payload):
        if change == 'forget':
            Knowledge(store).forget(rule['id'])
        elif change == 'edit':
            Knowledge(store).save({'kind': 'preference', 'scope': 'Communications', 'title': rule['title'],
                'content': 'Review all digest emails.'}, user=True, id=rule['id'])
        else:
            Knowledge(store).save({'kind': 'preference', 'title': 'Exception', 'content': 'Review these digests.'}, user=True)
        return ignore_all(payload)
    provider.decide = decide
    process(m, [mail()])
    assert store.rows('SELECT * FROM jobs')[0]['status'] == 'queued'


def test_new_edited_and_forgotten_rules_take_effect_without_restart(tmp_path):
    store, m, rule, provider = setup(tmp_path)
    process(m, [mail()])
    Knowledge(store).save({'kind': 'preference', 'scope': 'Communications', 'title': rule['title'],
        'content': 'Ignore monthly reports from reports@example.net.'}, user=True, id=rule['id'])
    process(m, [mail('b', 'reports@example.net', 'Monthly report', 'b')])
    assert 'monthly reports' in provider.calls[-1][0][-1]['content']
    assert 'weekly digests' not in provider.calls[-1][0][-1]['content']
    Knowledge(store).forget(rule['id'])
    process(m, [mail('c', thread='c')])
    assert len(provider.calls) == 2
    assert store.one("SELECT status FROM jobs WHERE title='Weekly digest' ORDER BY created DESC")['status'] == 'queued'


@pytest.mark.parametrize('bad', [None, 'not-json', {'decisions': 'invalid'}, {'decisions': [
    {'thread_id': 'thread', 'ignore': True, 'rule_ids': ['invented'], 'reason': 'Ignore'}]},
    {'decisions': [{'thread_id': 'invented', 'ignore': True, 'rule_ids': [], 'reason': 'Ignore'}]}])
def test_bad_classifier_output_falls_back_to_normal_queue(tmp_path, bad):
    store, m, _, _ = setup(tmp_path)
    class Bad:
        def complete(self, *args, **kwargs):
            if bad is None:
                raise TimeoutError('deadline')
            return {'content': bad if isinstance(bad, str) else json.dumps(bad)}, {}
    m.email_intake.provider = Bad()
    process(m, [mail()])
    assert store.rows('SELECT * FROM jobs')[0]['status'] == 'queued'


def test_uncertain_conditional_or_conflicting_rules_keep_normal_triage(tmp_path):
    store, m, rule, provider = setup(tmp_path, lambda payload: [])
    Knowledge(store).save({'kind': 'preference', 'title': 'Digest exception',
        'content': 'Unless a digest contains an action assigned to me, ignore it.'}, user=True)
    process(m, [mail()])
    assert store.rows('SELECT * FROM jobs')[0]['status'] == 'queued'
    prompt = provider.calls[0][0][0]['content']
    assert 'conflicting or ambiguous' in prompt
    assert 'unseen body/thread/attachment' in prompt
    assert 'BOTH no reply AND no substantive work' in prompt
    assert 'untrusted' in prompt


def test_owner_feedback_is_not_overridden_even_by_ignore_classification(tmp_path):
    store, m, _, provider = setup(tmp_path)
    process(m, [mail()])
    job = store.rows('SELECT * FROM jobs')[0]
    store.write('INSERT INTO review_messages(job_id,role,body,created) VALUES(?,?,?,?)',
        (job['id'], 'user', 'Please investigate this one.', job['created']))
    process(m, [mail('followup')])
    assert store.job(job['id'])['status'] == 'queued'


def test_existing_active_thread_is_not_sent_to_intake_classifier(tmp_path):
    store, m, _, provider = setup(tmp_path, lambda payload: [])
    process(m, [mail()])
    process(m, [mail('followup')])
    assert len(provider.calls) == 1


def test_batch_size_bounds_calls(tmp_path):
    store, m, _, provider = setup(tmp_path)
    process(m, [mail(str(i), thread=str(i)) for i in range(26)])
    assert len(provider.calls) == 2
    assert len(store.rows("SELECT * FROM jobs WHERE status='completed'")) == 26
