import json
from pathlib import Path

import pytest

from cos.conversations import cache_source_read, save_source
from cos.db import Store
from cos.monitor import Monitor
from cos.monitor_bridge import collect_teams
from cos.teams_content import download_images, empty_historical_gap, parse, pending_images, text
from cos.tools import Tools, allowed_job_action


URL = 'https://media.asm.skype.com/v1/objects/example/views/imgo'
IMAGE = '<img src="' + URL + '" alt="screenshot"/>'
PNG = b'\x89PNG\r\n\x1a\n' + b'synthetic image bytes'


@pytest.mark.parametrize('body', ['', '   ', '<p>&nbsp;</p>', '<div><br/></div>', '<script>hidden</script>'])
def test_only_truly_empty_content_is_empty(body):
    assert not text(body)


def test_images_links_and_picture_objects_survive_normalization():
    body = '<p>Please review</p>' + IMAGE + "<img src='" + URL + "' alt='screenshot'/>"
    assert 'Please review' in text(body)
    assert len(parse(body).images) == 1
    assert URL in text(body)
    assert text('<a href="https://example.com/file"></a>')
    assert text('<attachment id="file"/>')
    picture = '<URIObject type="Picture.1" uri="https://media.asm.skype.com/v1/objects/example"/>'
    assert parse(picture).images[0]['src'] == URL
    assert text('x < y', 'Text') == 'x < y'


def test_poller_retains_image_only_html_and_drops_empty_bodies():
    class Skype:
        _msg_base = 'https://example.com/v1'
        def get_skypetoken(self): pass
        def _hdr(self): return {}
        def list_messages(self, cid, **kwargs):
            if cid == '48:notes': return [], ''
            return [dict(id=str(i), composetime='2026-10-28T12:00:00Z',
                         messagetype='RichText', content=body, **{'from':'other'})
                    for i, body in enumerate(['', '<p>&nbsp;</p>', IMAGE, 'Caption' + IMAGE])], ''
    result = collect_teams(Skype(), {'id':'owner'}, '2026-10-28T00:00:00Z',
                           lambda *args: (200, {'conversations':[{'id':'chat'}]}, ''))
    assert [m['id'] for m in result['messages']] == ['2', '3']
    assert result['messages'][0]['content'] == IMAGE
    assert 'Teams image' in result['messages'][0]['text']


def setup_topic(tmp_path):
    store = Store(tmp_path/'images.db')
    monitor = Monitor(store)
    messages = [dict(id='image', chat_id='chat', owner=False, text='', content=IMAGE),
                dict(id='blank', chat_id='chat', owner=False, text='  ')]
    assert monitor.process({'messages':messages, 'emails':[]}) == 1
    job = store.rows('SELECT * FROM jobs')[0]
    assert store.rows('SELECT * FROM communication_sources')[0]['body'] == IMAGE
    assert len(store.rows('SELECT * FROM teams_topic_messages')) == 1
    return store, job


def test_image_downloads_are_topic_scoped_cached_and_require_visual_read(tmp_path, monkeypatch):
    from cos import tools
    store, job = setup_topic(tmp_path)
    calls = []
    def bridge(action, args):
        assert action == 'teams_attachments'
        assert args['messages'] == [{'id':'image', 'content':IMAGE}]
        calls.append(args)
        path = Path(args['target'])/'image.png'
        path.parent.mkdir(parents=True)
        path.write_bytes(PNG)
        return {'messages':[{'message_id':'image', 'saved':[str(path)], 'errors':[]}]}
    monkeypatch.setattr(tools, 'communication', bridge)
    store.update(job['id'], messages=json.dumps({'messages':[], 'historical_import':True}))
    job = store.job(job['id'])
    assert allowed_job_action(job, 'teams_attachments')
    assert pending_images(store, job['id']) == ['image']
    executor = Tools(store)
    result = executor.execute(job, 'teams_attachments', {'chat_id':'chat'})
    assert pending_images(store, job['id']) == ['image']
    assert executor.execute(job, 'teams_attachments', {'chat_id':'chat'}) == result
    assert len(calls) == 1
    viewed = executor.execute(job, 'read_file', {'path':result['files'][0]['file_id']})
    assert viewed['image_data'] and viewed['mime'] == 'image/png'
    assert pending_images(store, job['id']) == []
    with pytest.raises(ValueError, match='bound to this job'):
        executor.execute(job, 'teams_attachments', {'chat_id':'unrelated'})
    # A changed source needs fresh inspection even if its message ID is unchanged.
    cache_source_read(store, job['id'], 'read_teams', {'chat_id':'chat'},
                      [{'id':'image', 'content':IMAGE + '<img src="https://example.com/second"/>'}])
    assert pending_images(store, job['id']) == ['image']


def test_download_failure_records_a_gap_instead_of_silently_ignoring_image(tmp_path, monkeypatch):
    from cos import tools
    store, job = setup_topic(tmp_path)
    monkeypatch.setattr(tools, 'communication', lambda *args: (_ for _ in ()).throw(RuntimeError('Unavailable')))
    result = Tools(store).execute(job, 'teams_attachments', {'chat_id':'chat'})
    assert result['messages']['image']['errors']
    assert result['files'] == []
    assert pending_images(store, job['id']) == []
    assert store.rows('SELECT * FROM communication_sources')[0]['body'] == IMAGE


def test_media_guard_never_downloads_untrusted_host_and_keeps_other_images(tmp_path):
    class Client:
        calls = []
        def get_skypetoken(self): pass
        def is_trusted_media_url(self, url): return url == URL
        def download_object(self, url, target):
            self.calls.append(url)
            target.write_bytes(PNG)
    client = Client()
    body = "<img src='https://example.com/v1/objects/evil/views/imgo'/>" + IMAGE
    result = download_images(client, [{'id':'source', 'content':body}], tmp_path)
    assert client.calls == [URL]
    record = result['messages'][0]
    assert record['errors'] and len(record['saved']) == 1
    assert Path(record['saved'][0]).suffix == '.png'


def test_raw_reads_recover_historical_image_without_importing_sibling(tmp_path, monkeypatch):
    from cos import tools
    store, job = setup_topic(tmp_path)
    calls = []
    def read(name, script, args):
        calls.append(args)
        return [{'id':'image', 'content':IMAGE}, {'id':'sibling', 'content':'Unrelated'}]
    monkeypatch.setattr(tools, 'run_skill', read)
    result = Tools(store).execute(job, 'read_teams', {'chat_id':'chat'})
    assert '--raw' in calls[0]
    assert result == [{'id':'image', 'content':IMAGE}]
    assert [r['message_id'] for r in store.rows('SELECT * FROM communication_sources')] == ['image']


def test_blank_historical_gap_does_not_become_owner_task_but_image_gap_stays_visible(tmp_path):
    from cos.briefs import unresolved_action, update_brief
    store = Store(tmp_path/'historical.db')
    job = store.create_job('Historical import', 'Communications')
    store.update(job['id'], messages=json.dumps({'messages':[], 'historical_import':True}))
    job = store.job(job['id'])
    with store.connect() as c:
        save_source(c, job['id'], 'teams', 'blank', 'sender', '', '', ' ')
    brief = update_brief(store, job, {'action_needed':'unknown', 'response_needed':'unknown',
                                    'uncertainties':'The original source body is blank.'})
    assert empty_historical_gap(store, job, brief)
    assert unresolved_action(store, job) == ''
    with store.connect() as c:
        save_source(c, job['id'], 'teams', 'blank', 'sender', '', '', IMAGE)
    assert not empty_historical_gap(store, job, brief)
    assert unresolved_action(store, job) == 'The original source body is blank.'


@pytest.mark.parametrize('first_action', ['finish', 'ask_user'])
def test_engine_inspects_image_before_finishing_or_asking_owner(tmp_path, monkeypatch, first_action):
    from cos import tools
    from cos.briefs import update_brief
    from cos.engine import Engine
    store, job = setup_topic(tmp_path)
    def bridge(action, args):
        path = Path(args['target'])/'source.png'
        path.parent.mkdir(parents=True)
        path.write_bytes(PNG)
        return {'messages':[{'message_id':'image', 'saved':[str(path)], 'errors':[]}]}
    monkeypatch.setattr(tools, 'communication', bridge)
    decision = dict(context='Shared screenshot', status_summary='Image reviewed', next_step='None',
                    response_needed='no', response_reason='No reply requested', action_needed='no',
                    action_reason='Informational screenshot')
    update_brief(store, job, decision)
    class Provider:
        calls = 0
        def complete(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                action, args = first_action, ({'summary':'Done'} if first_action == 'finish' else {'question':'What is in the image?'})
            elif self.calls == 2:
                assert 'Teams source images remain uninspected' in messages[-1]['content']
                action, args = 'teams_attachments', {'chat_id':'chat'}
            elif self.calls == 3:
                action, args = 'read_file', {'path':store.rows('SELECT * FROM files')[0]['id']}
            elif self.calls == 4:
                assert any(m.get('_cos_tool_image') for m in messages)
                action, args = 'update_brief', decision
            else:
                action, args = 'finish', {'summary':'Screenshot reviewed; no owner obligation.'}
            return {'role':'assistant', 'content':None, 'tool_calls':[{
                'id':str(self.calls), 'type':'function', 'function':{'name':'perform',
                'arguments':json.dumps({'action':action, 'arguments':args})}}]}, {}
    store.write('DELETE FROM conversation_activity')
    engine = Engine(store, Provider())
    try:
        engine.run(job['id'])
    finally:
        engine.pool.shutdown()
    assert store.job(job['id'])['status'] == 'completed'
    assert not store.rows("SELECT * FROM events WHERE kind IN ('question','followup')")


def test_historical_visual_obligation_requires_viewing_the_exact_source_image(tmp_path, monkeypatch):
    from cos import tools
    from cos.briefs import update_brief
    store, job = setup_topic(tmp_path)
    store.update(job['id'], messages=json.dumps({'messages':[], 'historical_import':True}))
    job = store.job(job['id'])
    row = store.rows('SELECT * FROM teams_topic_messages')[0]
    payload = json.loads(row['payload'])
    payload['record_kind'] = 'message'
    store.write('UPDATE teams_topic_messages SET payload=?', (json.dumps(payload),))
    decision = dict(action_needed='yes', response_needed='no', owner_obligation_source_id='image',
                    owner_obligation_evidence='Please choose the release for this fix.')
    with pytest.raises(ValueError, match='Historical owner attention requires'):
        update_brief(store, job, decision)
    def bridge(action, args):
        path = Path(args['target'])/'source.png'
        path.parent.mkdir(parents=True)
        path.write_bytes(PNG)
        return {'messages':[{'message_id':'image', 'saved':[str(path)], 'errors':[]}]}
    monkeypatch.setattr(tools, 'communication', bridge)
    executor = Tools(store)
    result = executor.execute(job, 'teams_attachments', {'chat_id':'chat'})
    with pytest.raises(ValueError, match='Historical owner attention requires'):
        update_brief(store, job, decision)
    executor.execute(job, 'read_file', {'path':result['files'][0]['file_id']})
    assert update_brief(store, job, decision)['action_needed'] == 'yes'
    with pytest.raises(ValueError, match='Historical owner attention requires'):
        update_brief(store, job, {**decision, 'owner_obligation_source_id':'sibling'})


def test_startup_requeues_blank_historical_notification_for_raw_source_reassessment(tmp_path):
    from cos.briefs import update_brief
    from cos.engine import Engine
    store = Store(tmp_path/'old.db')
    job = store.create_job('Historical import', 'Communications')
    store.update(job['id'], messages=json.dumps({'messages':[], 'historical_import':True}))
    job = store.job(job['id'])
    with store.connect() as c:
        save_source(c, job['id'], 'teams', 'blank', 'sender', '', '', '')
    update_brief(store, job, {'action_needed':'unknown', 'response_needed':'unknown', 'uncertainties':'Missing original text'})
    store.update(job['id'], status='waiting_input', question='Follow-up remains: Missing original text')
    engine = Engine(store)
    try:
        engine.recover_historical_attention()
    finally:
        engine.pool.shutdown()
    assert store.job(job['id'])['status'] == 'queued'
    assert store.job(job['id'])['question'] == ''


@pytest.mark.parametrize('read_fails', [False, True])
def test_blank_import_must_attempt_raw_read_before_quiet_completion(tmp_path, monkeypatch, read_fails):
    from cos import tools
    from cos.briefs import update_brief
    from cos.engine import Engine
    store = Store(tmp_path/'blank-import.db')
    job = store.create_job('Triage this incoming Teams conversation.\nChat ID: chat', 'Communications')
    store.update(job['id'], messages=json.dumps({'messages':[], 'historical_import':True}))
    job = store.job(job['id'])
    with store.connect() as c:
        save_source(c, job['id'], 'teams', 'blank', 'sender', '', '', '')
    update_brief(store, job, dict(context='Historical source gap', status_summary='Blank source',
                 next_step='Preserve the gap', action_needed='unknown', response_needed='unknown',
                 action_reason='No assignment visible', response_reason='No request visible',
                 uncertainties='Original Teams content is unavailable.'))
    reads = []
    def raw_read(name, script, args):
        assert '--raw' in args
        reads.append(args)
        if read_fails: raise RuntimeError('Source unavailable')
        return [{'id':'blank', 'content':''}]
    monkeypatch.setattr(tools, 'run_skill', raw_read)
    class Provider:
        calls = 0
        def complete(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 2:
                assert 'may be image-only messages' in messages[-1]['content']
                action, args = 'read_teams', {'chat_id':'chat'}
            else:
                action, args = 'finish', {'summary':'Historical content gap retained; no evidenced owner obligation.'}
            return {'role':'assistant', 'content':None, 'tool_calls':[{
                'id':str(self.calls), 'type':'function', 'function':{'name':'perform',
                'arguments':json.dumps({'action':action,'arguments':args})}}]}, {}
    engine = Engine(store, Provider())
    try:
        engine.run(job['id'])
    finally:
        engine.pool.shutdown()
    assert len(reads) == 1
    assert store.job(job['id'])['status'] == 'completed'
    assert not store.job(job['id'])['question']
    assert store.detail(job['id'])['brief']['uncertainties'] == 'Original Teams content is unavailable.'
