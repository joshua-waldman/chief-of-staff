import json
from pathlib import Path

import pytest

from cos.db import Store
from cos.knowledge import Knowledge
from cos.meetings import save_summary
from cos.monitor import Monitor
from cos.monitor_bridge import collect_teams
from cos.policy import classify
from cos.tools import allowed_action


def test_meeting_summary_search_provenance_update_and_forgetting(tmp_path):
    store = Store(tmp_path / 'meeting.db')
    job = store.create_job('Review launch meeting', 'Communications')
    path = tmp_path / 'transcript.vtt'
    path.write_text('00:01:00 Alex: I will prepare the Example Project launch plan by Friday.', encoding='utf-8')
    store.write('INSERT INTO files VALUES(?,?,?,?,?,?)', ('f', job['id'], path.name, str(path), 'text/vtt', 'now'))
    store.set_state('meeting_sources:' + job['id'], {'f': {'source': 'https://amd.sharepoint.com/meeting', 'chat_id': 'meeting', 'identity': 'one'}})
    args = {'source_file_id': 'f', 'title': 'Example Project launch meeting', 'summary': 'Alex will prepare the Example Project launch plan by Friday. Timestamp 00:01:00.', 'evidence': 'Alex: I will prepare the Example Project launch plan by Friday.'}
    result = save_summary(store, job, args)
    knowledge = Knowledge(store)
    found = knowledge.search('Example Project launch')
    assert len(found) == 1
    assert found[0]['source'] == 'meeting-summary'
    assert found[0]['source_job'] == job['id']
    assert '/api/files/f' in found[0]['content']
    future = store.create_job('Reply to the email about the Example Project launch', 'Communications', project='Other')
    assert 'launch plan by Friday' in knowledge.context(future)
    save_summary(store, job, {**args, 'summary': 'Updated Example Project launch observations'})
    assert len(knowledge.search('launch')) == 1
    knowledge.forget(result['id'])
    assert save_summary(store, job, args)['status'] == 'forgotten'
    assert not knowledge.search('launch')
    with pytest.raises(ValueError, match='exact excerpt'):
        save_summary(store, job, {**args, 'evidence': 'Invented assignment'})
    other = store.create_job('Other meeting', 'Communications')
    with pytest.raises(ValueError, match='registered'):
        save_summary(store, other, args)


def test_owner_recording_notification_queues_once(tmp_path):
    store = Store(tmp_path / 'monitor.db')
    monitor = Monitor(store)
    data = {'emails': [], 'messages': [{'id': 'recording', 'chat_id': 'meeting', 'owner': True, 'date': '2026-10-22', 'text': 'New recording', 'meeting_event': True}]}
    assert monitor.process(data) == 1
    assert monitor.process(data) == 0
    job = store.rows('SELECT * FROM jobs')[0]
    assert 'save_meeting_summary' in job['request']
    assert 'delegate' in job['request']


def test_recording_service_events_keep_links_and_properties():
    class Skype:
        _msg_base = 'https://amer.ng.msg.teams.microsoft.com/v1'
        def get_skypetoken(self): pass
        def _hdr(self): return {}
        def list_messages(self, cid, **kwargs):
            if cid == '48:notes': return [], ''
            return [{'id': 'r', 'composetime': '2026-10-22T12:00:00Z', 'messagetype': 'ThreadActivity/Recording', 'from': 'owner', 'content': '<recording href="https://amd.sharepoint.com/:v:/g/source"/>', 'properties': {'transcript': 'available'}}], ''
    data = collect_teams(Skype(), {'id': 'owner'}, '2026-10-22T00:00:00Z', lambda *args: (200, {'conversations': [{'id': 'meeting'}]}, ''))
    message = data['messages'][0]
    assert message['meeting_event']
    assert message['owner']
    assert 'https://amd.sharepoint.com/:v:/g/source' in message['text']
    assert 'transcript' in message['text']


def test_meeting_tools_are_read_or_local_and_actions_keep_approval():
    for tool in ('meeting_transcripts', 'save_meeting_summary'):
        assert allowed_action('Communications', tool)
        assert not classify(tool, {}).approval
    assert classify('delegate', {}, 'Communications').approval
    assert classify('send_teams', {}).approval


def test_transcript_collection_skips_video_and_bounds_downloads(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    import urllib.request
    from cos.meetings import collect_transcripts, MAX_TRANSCRIPT
    from cos import integrations
    monkeypatch.setattr(integrations, 'skill_dir', lambda name: tmp_path)
    items = {
        'video': {'id': 'v', 'name': 'Recording.mp4'},
        'large': {'id': 'l', 'name': 'transcript.vtt', 'size': MAX_TRANSCRIPT + 1},
        'missing': None,
        'text': {'id': 't', 'name': '../../transcript.vtt', 'webUrl': 'https://amd.sharepoint.com/transcript', '@microsoft.graph.downloadUrl': 'https://amd.sharepoint.com/download'},
    }
    class Skype:
        def list_messages(self, *args, **kwargs): return [{'urls': list(items)}], 'more'
    monkeypatch.setitem(sys.modules, 'graph_client', SimpleNamespace(GraphClient=lambda: object(), _ssl_context=lambda: None))
    monkeypatch.setitem(sys.modules, 'meeting_artifacts', SimpleNamespace(SkypeClient=Skype, extract_urls_from_message=lambda m: m['urls'], resolve=lambda graph, url: items[url]))
    import io
    calls = []
    def download(url, **kwargs):
        calls.append(url)
        return io.BytesIO(b'WEBVTT\n00:01:00 Alex: Prepare the launch plan.')
    monkeypatch.setattr(urllib.request, 'urlopen', download)
    result = collect_transcripts('meeting', tmp_path / 'out')
    assert result['scan_incomplete']
    assert [x['status'] for x in result['artifacts']] == ['metadata_only', 'too_large', 'forbidden_or_missing', 'downloaded']
    assert calls == ['https://amd.sharepoint.com/download']
    path = Path(result['artifacts'][-1]['path'])
    assert path.parent == tmp_path / 'out'
    assert 'Alex' in path.read_text()


def test_long_transcripts_can_be_read_to_the_final_assignment(tmp_path, monkeypatch):
    from cos import tools
    monkeypatch.setattr(tools, 'DATA', tmp_path)
    store = Store(tmp_path / 'pages.db')
    job = store.create_job('Summarize long meeting', 'Communications')
    path = tools.workspace(job['id']) / 'transcript.vtt'
    path.write_text('x' * 80000 + 'Alex: Prepare the report.', encoding='utf-8')
    runner = tools.Tools(store)
    first = runner.execute(job, 'read_file', {'path': str(path)})
    assert first['truncated'] and first['next_offset'] == len(first['text'])
    parts=[first['text']]
    last=first
    while last['next_offset'] is not None:
        last = runner.execute(job, 'read_file', {'path': str(path), 'offset': last['next_offset']})
        assert len(last['text'])<=8000
        parts.append(last['text'])
    assert not last['truncated'] and last['next_offset'] is None
    assert last['text'] == 'Alex: Prepare the report.'
    assert ''.join(parts)==path.read_text(encoding='utf-8')
    with pytest.raises(ValueError, match='offset'):
        runner.execute(job, 'read_file', {'path': str(path), 'offset': -1})
