import json

import pytest

from cos.conversations import cache_source_read, load_sources
from cos.db import Store
from cos.knowledge import Knowledge
from cos.message_triage import read_targets
from cos.monitor import Monitor
from cos.teams_topics import TeamsTopics, chat_ids


def message(mid, text, owner=False, chat='chat'):
    return {'id': mid, 'chat_id': chat, 'owner': owner, 'text': text, 'date': mid}


class SemanticProvider:
    """Deterministic assignments let tests verify routing and ingestion separately."""
    def __init__(self):
        self.inputs = []

    def complete(self, messages, **kwargs):
        payload = json.loads(messages[-1]['content'])
        self.inputs.append(payload)
        groups = {}
        for m in payload['messages']:
            title = 'Budget planning' if 'budget' in m['text'].lower() else 'Release readiness'
            existing = next((t for t in payload['existing_topics'] if t['title'] == title), None)
            label = existing['topic_id'] if existing else title
            groups.setdefault(label, {'topic_id': label, 'title': title, 'message_ids': []})['message_ids'].append(str(m['id']))
        return {'content': json.dumps({'topics': list(groups.values())})}, {}


@pytest.fixture
def monitor(tmp_path):
    m = Monitor(Store(tmp_path/'topics.db'))
    m.teams_topics = TeamsTopics(m.store, SemanticProvider())
    return m


def ingest(m, *messages):
    return m.process({'messages': list(messages), 'emails': []})


def topic_job(m, title):
    return m.store.one('SELECT j.* FROM jobs j JOIN communication_threads c ON c.job_id=j.id '
                       'JOIN teams_topics t ON t.topic_id=c.thread_id WHERE t.title=?', (title,))


def test_unrelated_topics_in_one_chat_have_separate_sources_and_approvals(monitor):
    assert ingest(monitor, message('01', 'Review budget'), message('02', 'Release is ready'),
                  message('03', 'Budget approved', owner=True)) == 2
    budget = topic_job(monitor, 'Budget planning')
    release = topic_job(monitor, 'Release readiness')
    assert [s['message_id'] for s in monitor.store.detail(budget['id'])['source_messages']] == ['01', '03']
    assert [s['message_id'] for s in monitor.store.detail(release['id'])['source_messages']] == ['02']
    for job in (budget, release):
        monitor.store.update(job['id'], status='awaiting_approval')
        monitor.store.write('INSERT INTO approvals(id,job_id,status) VALUES(?,?,?)', (job['id'], job['id'], 'pending'))
    ingest(monitor, message('04', 'Budget changed'))
    assert monitor.store.one('SELECT status FROM approvals WHERE id=?', (budget['id'],))['status'] == 'superseded'
    assert monitor.store.one('SELECT status FROM approvals WHERE id=?', (release['id'],))['status'] == 'pending'
    assert monitor.store.job(release['id'])['status'] == 'awaiting_approval'


def test_completed_topic_reopens_after_restart_without_reopening_sibling(monitor):
    ingest(monitor, message('01', 'Budget review'), message('02', 'Release review'))
    budget, release = topic_job(monitor, 'Budget planning'), topic_job(monitor, 'Release readiness')
    for job in (budget, release):
        monitor.store.update(job['id'], status='completed')
    restarted = Monitor(Store(monitor.store.path))
    provider = SemanticProvider()
    restarted.teams_topics = TeamsTopics(restarted.store, provider)
    assert ingest(restarted, message('01', 'Budget review'), message('03', 'Budget done', owner=True)) == 0
    assert len(restarted.store.rows('SELECT * FROM jobs')) == 2
    assert restarted.store.job(budget['id'])['status'] == 'queued'
    assert restarted.store.job(release['id'])['status'] == 'completed'
    assert [m['id'] for m in provider.inputs[0]['messages']] == ['03']


@pytest.mark.parametrize('groups', [
    [],
    [{'topic_id': 'a', 'title': 'Budget', 'message_ids': ['01']}],
    [{'topic_id': 'a', 'title': 'Budget', 'message_ids': ['01', 'unknown']}],
    [{'topic_id': 'a', 'title': 'Budget', 'message_ids': ['01', '01', '02']}],
    [{'topic_id': 'a', 'title': 'Budget', 'message_ids': ['01']},
     {'topic_id': 'a', 'title': 'Release', 'message_ids': ['02']}],
])
def test_invalid_assignments_leave_messages_unseen_for_retry(monitor, groups):
    class Broken:
        def complete(self, *args, **kwargs):
            return {'content': json.dumps({'topics': groups})}, {}
    monitor.teams_topics.provider = Broken()
    with pytest.raises(ValueError, match='retained for retry'):
        ingest(monitor, message('01', 'Budget'), message('02', 'Release'))
    assert not monitor.store.rows('SELECT * FROM seen')
    assert not monitor.store.rows('SELECT * FROM teams_topic_messages')
    assert not monitor.store.rows('SELECT * FROM jobs')
    monitor.teams_topics.provider = SemanticProvider()
    assert ingest(monitor, message('01', 'Budget'), message('02', 'Release')) == 2


def test_transport_id_and_source_loading_remain_topic_scoped(monitor, monkeypatch):
    from cos import integrations
    from cos import tools
    from cos.teams_topics import scoped_messages
    ingest(monitor, message('01', 'Budget'), message('02', 'Release'))
    budget = topic_job(monitor, 'Budget planning')
    assert chat_ids(monitor.store, budget['id']) == ['chat']
    monitor.store.update(budget['id'], status='completed')
    monitor.store.write('INSERT INTO job_briefs VALUES(?,?,?)',
        (budget['id'], json.dumps({'response_needed':'no', 'action_needed':'no'}), 'now'))
    assert read_targets(monitor.store, monitor.store.job(budget['id'])) == [
        {'tool':'mark_teams_read', 'args':{'chat_id':'chat'}, 'label':'Mark entire Teams chat as read'}]
    raw = [{'id':'01', 'content':'Full budget text'}, {'id':'02', 'content':'Unrelated release text'}]
    assert scoped_messages(monitor.store, budget['id'], 'chat', raw) == [raw[0]]
    assert scoped_messages(monitor.store, budget['id'], 'other-chat', raw) == []
    monkeypatch.setattr(integrations, 'run_skill', lambda *args: raw)
    monkeypatch.setattr(tools, 'run_skill', lambda *args: raw)
    assert tools.Tools(monitor.store).execute(budget, 'read_teams', {'chat_id':'chat'}) == [raw[0]]
    assert not load_sources(monitor.store, budget)['errors']
    cache_source_read(monitor.store, budget['id'], 'read_teams', {'chat_id':'chat'}, raw)
    sources = monitor.store.detail(budget['id'])['source_messages']
    assert len(sources) == 1 and sources[0]['body'] == 'Full budget text'


def test_topic_guidance_does_not_apply_to_sibling_or_whole_chat(monitor):
    from cos.message_triage import first_pass
    ingest(monitor, message('01', 'Budget'), message('02', 'Release'))
    budget, release = topic_job(monitor, 'Budget planning'), topic_job(monitor, 'Release readiness')
    tid = monitor.store.one('SELECT thread_id FROM communication_threads WHERE job_id=?', (budget['id'],))['thread_id']
    kb = Knowledge(monitor.store)
    kb.save({'kind':'preference', 'title':'Budget handling', 'content':'Ignore budget updates',
             'target':{'source':'teams', 'thread_id':tid}}, user=True)
    assert 'Ignore budget updates' in kb.context(budget)
    assert 'Ignore budget updates' not in kb.context(release)
    evidence = json.loads(first_pass(monitor.store, release, {'messages': []})[-1]['content'])
    assert evidence['owner_preferences'] == []


def test_chat_boundaries_and_bounded_batches(monitor):
    assert ingest(monitor, message('01', 'Budget', chat='a'), message('01', 'Budget', chat='b')) == 2
    assert len(monitor.store.rows('SELECT * FROM teams_topics')) == 2
    provider = monitor.teams_topics.provider
    assert ingest(monitor, *[message(str(i).zfill(3), 'Release') for i in range(85)]) == 1
    assert [len(p['messages']) for p in provider.inputs[-3:]] == [40, 40, 5]
    assert len(monitor.store.rows('SELECT * FROM teams_topic_messages')) == 87


def test_owner_only_topic_keeps_evidence_without_starting_work(monitor):
    assert ingest(monitor, message('01', 'I already approved the budget', owner=True)) == 0
    assert not monitor.store.rows('SELECT * FROM jobs')
    assert ingest(monitor, message('02', 'Thanks for the budget approval')) == 1
    job = topic_job(monitor, 'Budget planning')
    assert [s['message_id'] for s in monitor.store.detail(job['id'])['source_messages']] == ['01', '02']
    assert 'I already approved the budget' in job['request']


def test_routing_previews_keep_full_original_messages(monitor):
    original = 'Budget ' + 'x'*10000
    assert ingest(monitor, message('01', original)) == 1
    routed = monitor.teams_topics.provider.inputs[0]['messages'][0]
    assert len(routed['text']) == 3000 and routed['may_be_incomplete']
    job = topic_job(monitor, 'Budget planning')
    assert monitor.store.detail(job['id'])['source_messages'][0]['body'] == original


def test_model_outage_retains_checkpoint_and_does_not_merge_chat(monitor, monkeypatch):
    from types import SimpleNamespace
    import cos.monitor as module
    stamp = '2026-10-22T00:00:00+00:00'
    monitor.store.set_state('monitor_checkpoint', stamp)
    monkeypatch.setattr(module, 'gateway_key', lambda: 'test')
    monkeypatch.setattr(module.subprocess, 'run', lambda args, **kw: SimpleNamespace(
        returncode=0, stderr='', stdout=json.dumps({'messages': [message('01', 'Budget')]
            if args[-1]=='teams' else [], 'emails':[], 'conversations':1})))
    class Unavailable:
        def complete(self, *args, **kwargs):
            raise RuntimeError('Unavailable')
    monitor.teams_topics.provider = Unavailable()
    monitor.tick()
    assert monitor.store.state('monitor_checkpoint_teams') == stamp
    assert monitor.store.state('monitor_status')['attention_required']
    assert not monitor.store.rows('SELECT * FROM seen')
    monitor.teams_topics.provider = SemanticProvider()
    monitor.tick()
    assert monitor.store.state('monitor_checkpoint_teams') != stamp
    assert len(monitor.store.rows('SELECT * FROM jobs')) == 1


def test_legacy_chat_context_is_preserved_without_receiving_new_topics(monitor):
    job = monitor.store.create_job('Triage this incoming Teams conversation. Chat ID: chat', 'Communications')
    monitor.store.write("INSERT INTO communication_threads VALUES('teams','chat',?)", (job['id'],))
    monitor.store.update(job['id'], status='awaiting_approval')
    assert ingest(monitor, message('01', 'Budget'), message('02', 'Release')) == 2
    assert monitor.store.job(job['id'])['status'] == 'awaiting_approval'
    assert not monitor.store.detail(job['id'])['source_messages']


def legacy_job(monitor, status='awaiting_approval'):
    from cos.conversations import save_source
    job=monitor.store.create_job('Triage this incoming Teams conversation. Chat ID: chat','Communications')
    monitor.store.update(job['id'],status=status)
    monitor.store.write("INSERT INTO communication_threads VALUES('teams','chat',?)",(job['id'],))
    with monitor.store.connect() as c:
        save_source(c,job['id'],'teams','01','Participant','','01','Review budget')
        save_source(c,job['id'],'teams','02','Participant','','02','Release readiness')
        save_source(c,job['id'],'teams','03','You','','03','Budget approved')
    return job


def test_existing_contexts_migrate_atomically_and_keep_relevant_history(monitor):
    from cos.teams_migration import migrate
    from cos.tools import allowed_job_action
    job=legacy_job(monitor)
    monitor.store.write('INSERT INTO review_messages(job_id,role,body,created) VALUES(?,?,?,?)',
        (job['id'],'user','Use the approved budget numbers','04'))
    monitor.store.write('INSERT INTO approvals(id,job_id,tool,args,status,result) VALUES(?,?,?,?,?,?)',
        ('sent',job['id'],'send_teams','{"body":"Budget approved"}','executed','{"sent":true}'))
    monitor.store.write('INSERT INTO approvals(id,job_id,status) VALUES(?,?,?)',('old-pending',job['id'],'pending'))
    assert migrate(monitor)=={'migrated':1,'topics':2,'deferred':0,'failed':0}
    budget,release=topic_job(monitor,'Budget planning'),topic_job(monitor,'Release readiness')
    assert monitor.store.job(job['id'])['status']=='cancelled'
    assert 'Use the approved budget numbers' in budget['request']
    assert 'Use the approved budget numbers' not in release['request']
    assert 'action_receipt' in budget['request']
    assert monitor.store.one("SELECT status FROM approvals WHERE id='sent'")['status']=='executed'
    assert monitor.store.one("SELECT status FROM approvals WHERE id='old-pending'")['status']=='superseded'
    assert [s['message_id'] for s in monitor.store.detail(budget['id'])['source_messages']]==['01','03']
    assert not allowed_job_action(budget,'send_teams')
    assert not allowed_job_action(budget,'delegate')
    assert not allowed_job_action(budget,'mark_teams_read')
    assert allowed_job_action(budget,'update_brief') and allowed_job_action(budget,'finish')
    assert migrate(monitor)=={'migrated':0,'topics':0,'deferred':0,'failed':0}
    assert len(monitor.store.rows('SELECT * FROM jobs'))==3
    ingest(monitor,message('05','Budget update'))
    assert len(monitor.store.rows('SELECT * FROM jobs'))==3
    assert allowed_job_action(monitor.store.job(budget['id']),'send_teams')


def test_failed_import_retries_without_partial_topics(monitor):
    from cos.teams_migration import migrate
    job=legacy_job(monitor)
    real=monitor.teams_topics.provider
    class Broken:
        def complete(self,*args,**kwargs):
            raise RuntimeError('Unavailable')
    monitor.teams_topics.provider=Broken()
    assert migrate(monitor)['failed']==1
    assert not monitor.store.rows('SELECT * FROM teams_topics')
    assert len(monitor.store.detail(job['id'])['source_messages'])==3
    monitor.teams_topics.provider=real
    assert migrate(monitor)['migrated']==1
    assert len(monitor.store.rows('SELECT * FROM teams_topics'))==2


def test_uncertain_legacy_mutation_defers_only_its_chat(monitor):
    from cos.teams_migration import migrate
    job=legacy_job(monitor,'blocked')
    monitor.store.write('INSERT INTO approvals(id,job_id,status) VALUES(?,?,?)',('unknown',job['id'],'uncertain'))
    assert migrate(monitor)=={'migrated':0,'topics':0,'deferred':1,'failed':0}
    assert monitor.store.one("SELECT status FROM approvals WHERE id='unknown'")['status']=='uncertain'
    assert not monitor.store.rows('SELECT * FROM teams_topics')


def test_import_detects_history_changed_during_inference(monitor):
    from cos.teams_migration import migrate
    job=legacy_job(monitor)
    original=monitor.teams_topics.provider
    class Changed:
        def complete(self,*args,**kwargs):
            result=original.complete(*args,**kwargs)
            monitor.store.write('INSERT INTO review_messages(job_id,role,body) VALUES(?,?,?)',(job['id'],'user','Latest budget direction'))
            return result
    monitor.teams_topics.provider=Changed()
    assert migrate(monitor)['failed']==1
    assert not monitor.store.rows('SELECT * FROM teams_topics')
    monitor.teams_topics.provider=original
    assert migrate(monitor)['migrated']==1
    assert 'Latest budget direction' in topic_job(monitor,'Budget planning')['request']


def test_pending_legacy_jobs_and_children_cannot_run_while_migrating(monitor):
    from cos.teams_migration import prepare
    from cos.engine import Engine
    job=legacy_job(monitor)
    child=monitor.store.create_job('Delegated budget work','Technology',parent_id=job['id'])
    prepare(monitor.store)
    monitor.store.update(job['id'],status='queued')
    monitor.store.update(child['id'],status='queued')
    engine=Engine(monitor.store)
    assert engine.ready_jobs(None)==[]
    engine.stop()


def test_migration_preserves_unreviewed_returned_drafts(monitor):
    from cos.teams_migration import migrate
    job=legacy_job(monitor)
    monitor.store.write('INSERT INTO result_reviews(job_id,revision,summary,file_ids,completed,reviewed,eligible) VALUES(?,?,?,?,?,NULL,1)',
        (job['id'],'draft-revision','Saved budget draft for review','[]','04'))
    assert migrate(monitor)['migrated']==1
    assert monitor.store.job(job['id'])['status']=='completed'
    assert [r['job_id'] if 'job_id' in r else r['id'] for r in monitor.store.pending_results()]==[job['id']]
    assert 'Saved budget draft for review' in topic_job(monitor,'Budget planning')['request']


def test_older_topic_table_upgrades_and_backfills_retained_sources(tmp_path):
    import sqlite3
    from cos.conversations import save_source
    store=Store(tmp_path/'older-schema.db')
    job=store.create_job('Older topic','Communications')
    with store.connect() as c:
        c.execute("INSERT INTO teams_topics VALUES('topic','chat','Budget')")
        c.execute("INSERT INTO communication_threads VALUES('teams','topic',?)",(job['id'],))
        save_source(c,job['id'],'teams','m','You','','2026-10-28','Approved the budget')
        c.execute('DROP TABLE teams_topic_messages')
        c.execute('CREATE TABLE teams_topic_messages(chat_id TEXT,message_id TEXT,topic_id TEXT NOT NULL,PRIMARY KEY(chat_id,message_id))')
        c.execute("INSERT INTO teams_topic_messages VALUES('chat','m','topic')")
        c.execute("CREATE TRIGGER fail_backfill BEFORE UPDATE ON teams_topic_messages BEGIN SELECT RAISE(ABORT,'interrupted backfill'); END")
    with pytest.raises(sqlite3.IntegrityError,match='interrupted backfill'):
        Store(store.path)
    with sqlite3.connect(store.path) as c:
        assert 'payload' not in {r[1] for r in c.execute('PRAGMA table_info(teams_topic_messages)')}
        c.execute('DROP TRIGGER fail_backfill')
    upgraded=Store(store.path)
    row=upgraded.one("SELECT * FROM teams_topic_messages WHERE message_id='m'")
    assert row['topic_id']=='topic'
    payload=json.loads(row['payload'])
    assert payload['text']=='Approved the budget' and payload['owner'] is True
    assert payload['date']=='2026-10-28'
    assert Store(store.path).one("SELECT payload FROM teams_topic_messages WHERE message_id='m'")['payload']==row['payload']



def test_migration_collects_unreviewed_child_before_archiving_director(monitor):
    from cos.teams_migration import migrate
    job = legacy_job(monitor, status='waiting_input')
    child = monitor.store.create_job('Research budget', 'Technology', parent_id=job['id'])
    monitor.store.update(child['id'], status='completed', result='Budget analysis for owner review',
        messages=json.dumps({'result_kind': 'strategy', 'deliverable_file_ids': []}))
    assert migrate(monitor)['migrated'] == 1
    assert monitor.store.job(job['id'])['status'] == 'completed'
    assert [r['id'] for r in monitor.store.pending_results()] == [job['id']]
    review = monitor.store.result_review(job['id'])
    assert 'Budget analysis for owner review' in review['summary']
    assert Store(monitor.store.path).result_review(job['id']) == review
    monitor.store.review_result(job['id'], review['revision'])
    assert not Store(monitor.store.path).pending_results()
