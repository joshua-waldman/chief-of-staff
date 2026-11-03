from cos.job_names import compact_name, display_name
from cos.briefs import update_brief
from cos.db import Store
import pytest
from fastapi.testclient import TestClient


def test_user_names_persist_across_updates_and_reset(tmp_path, monkeypatch):
    from cos import server
    path = tmp_path/'renames.db'
    store = Store(path)
    monkeypatch.setattr(server, 'store', store)
    parent = store.create_job('Review the release')
    child = store.create_job('Inspect logs', 'Technology', parent_id=parent['id'])
    original = store.job(child['id'])
    client = TestClient(server.app)
    client.cookies.set('cos_session', server.TOKEN)
    headers = {'x-cos-token': server.TOKEN}
    url = '/api/jobs/'+child['id']+'/rename'
    name = '发布计划 — RE: release 2.0 <review> and all follow-up actions'
    assert client.post(url, json={'name':name}).status_code == 403
    assert client.post(url, json={'name':'  '+name+'  '}, headers=headers).status_code == 200
    assert store.job(child['id']) == original
    update_brief(store, child, {'job_name':'New automatic label'})
    monkeypatch.setattr(server, 'store', Store(path))
    for compact in (False, True):
        queue = client.get('/api/jobs', params={'compact':compact}).json()
        assert next(j for j in queue if j['id']==child['id'])['display_name'] == name
        detail = client.get('/api/jobs/'+parent['id'], params={'compact':compact}).json()
        assert detail['children'][0]['display_name'] == name
        assert client.get('/api/jobs/'+child['id'], params={'compact':compact}).json()['user_name'] == name
    agents = client.get('/api/organization-agents').json()
    assert next(j for j in agents if j['id']==child['id'])['display_name'] == name
    assert client.post(url, json={'name':''}, headers=headers).status_code == 200
    assert client.get('/api/jobs/'+child['id']).json()['display_name'] == 'New automatic label'


@pytest.mark.parametrize('payload', [{}, {'name':None}, {'name':False}, {'name':[]},
    {'name':'x'*121}, {'name':'two\nlines'}, {'name':'null\x00character'}, []])
def test_rename_rejects_invalid_names(tmp_path, monkeypatch, payload):
    from cos import server
    store = Store(tmp_path/'invalid.db')
    monkeypatch.setattr(server, 'store', store)
    job = store.create_job('Review release')
    client = TestClient(server.app)
    client.cookies.set('cos_session', server.TOKEN)
    assert client.post('/api/jobs/'+job['id']+'/rename', json=payload,
                       headers={'x-cos-token':server.TOKEN}).status_code == 400
    assert not store.rows('SELECT * FROM job_names')


def test_rename_missing_job(tmp_path, monkeypatch):
    from cos import server
    monkeypatch.setattr(server, 'store', Store(tmp_path/'missing.db'))
    client = TestClient(server.app)
    client.cookies.set('cos_session', server.TOKEN)
    assert client.post('/api/jobs/missing/rename', json={'name':'Name'},
                       headers={'x-cos-token':server.TOKEN}).status_code == 404


def test_compact_names_remove_source_ids_and_bound_length():
    assert compact_name('Release review Meeting ID: 12345 Message ID: 99999') == 'Release review'
    assert compact_name('RE: Fwd: Release readiness') == 'Release readiness'
    name = compact_name('Review release readiness and prepare a comprehensive report with all evidence')
    assert len(name.split()) <= 7 and len(name) <= 60
    assert '1234567890123' not in compact_name('Launch 1234567890123')
    assert compact_name('Review https://example.com/a') == 'Review'


def test_legacy_names_and_generated_names():
    job = {'org':'Communications', 'title':'Teams conversation: 19:meeting_long_identifier@thread.v2'}
    assert display_name(job, {}) == 'Conversation review'
    assert display_name(job, {'context':'Launch readiness and follow-up'}) == 'Launch readiness and follow-up'
    assert display_name(job, {'job_name':'Quarterly budget approval','email_subject':'A very long original subject'}) == 'Quarterly budget approval'


def test_queue_uses_short_name_without_changing_detail(tmp_path, monkeypatch):
    from cos import server
    store = Store(tmp_path/'names.db')
    monkeypatch.setattr(server, 'store', store)
    job = store.create_job('Please review the launch readiness and prepare a detailed report', 'Technology')
    update_brief(store, job, {'job_name':'Launch readiness Meeting ID: 12345'})
    row = server.jobs()[0]
    assert row['display_name'] == 'Launch readiness'
    assert store.job(job['id'])['request'] == job['request']
    assert store.job(job['id'])['title'] == job['title']


def test_legacy_meeting_topic_ignores_system_ids_and_recording_controls():
    job = {'org':'Communications', 'title':'Teams conversation: 19:meeting_opaque@thread.v2'}
    sender = '8:orgid:00000000-0000-0000-0000-000000000000'
    sources = [{'body':'1786485384389'+sender+'ChatEnabled'},
               {'body':'1786485384390'+sender+'Launch Readiness Forum'},
               {'body':'Launch Readiness ForumPlay'}]
    assert display_name(job, {}, sources) == 'Launch Readiness Forum'
    assert display_name(job, {}, [{'body':'Launch Readiness ForumPlay'}]) == 'Launch Readiness Forum'
    assert display_name(job, {}, [{'body':'{\"callId\":\"opaque\"}'}]) == 'Conversation review'


def test_raw_html_and_plain_source_topics():
    job = {'org':'Communications', 'title':'Teams conversation: 19:meeting_opaque@thread.v2'}
    assert display_name(job, {}, [{'body':'<root><topic>Launch &amp; budget review</topic></root>'}]) == 'Launch & budget review'
    assert display_name(job, {}, [{'subject':'RE: Launch readiness','body':'Other content'}]) == 'Launch readiness'
    assert display_name(job, {}, [{'body':'Review the launch readiness. Details follow.'}]) == 'Review the launch readiness'
    assert display_name(job, {'job_name':'Generated launch summary'}, [{'subject':'Earlier topic'}]) == 'Generated launch summary'


def test_legacy_job_list_and_detail_share_source_topic_without_mutation(tmp_path, monkeypatch):
    from cos import server
    from cos.conversations import save_source
    store = Store(tmp_path/'legacy-names.db')
    monkeypatch.setattr(server, 'store', store)
    job = store.create_job('Triage this incoming Teams conversation. Chat ID: 19:meeting_opaque@thread.v2', 'Communications')
    store.update(job['id'], title='Teams conversation: 19:meeting_opaque@thread.v2', status='blocked')
    with store.connect() as c:
        save_source(c, job['id'], 'teams', 'one', 'Participant', '', '', 'Launch Readiness ForumPlay', True)
    original = store.job(job['id'])
    assert server.jobs()[0]['display_name'] == 'Launch Readiness Forum'
    detail = server.job(job['id'])
    assert detail['display_name'] == 'Launch Readiness Forum'
    assert detail['title'] == original['title']
    assert detail['request'] == original['request']
    assert store.job(job['id']) == original
    assert 'source_messages' not in server.jobs()[0]


def test_detail_and_child_use_generated_names(tmp_path, monkeypatch):
    from cos import server
    store = Store(tmp_path/'child-names.db')
    monkeypatch.setattr(server, 'store', store)
    parent = store.create_job('Review the release')
    child = store.create_job('Investigate the release logs', 'Technology', parent_id=parent['id'])
    update_brief(store, parent, {'job_name':'Release review'})
    update_brief(store, child, {'job_name':'Release failure analysis'})
    detail = server.job(parent['id'])
    assert detail['display_name'] == 'Release review'
    assert detail['children'][0]['display_name'] == 'Release failure analysis'
