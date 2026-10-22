import json
from pathlib import Path
from types import SimpleNamespace

from cos import meeting_stream as ms


def card(day='20261022', drive='drive', item='item'):
    return {'composetime': '2026-10-22T12:00:00Z', 'content':
            '<card driveItemId="' + item + '" type="onedriveForBusinessVideo" driveId="' + drive + '">Meeting-' + day + '_100000.mp4</card>'}


def helper():
    return SimpleNamespace(
        site_root_from_weburl=lambda url: ('https://amdcloud-my.sharepoint.com', 'https://amdcloud-my.sharepoint.com/personal/owner'),
        foci_swap=lambda graph, audience: 'test-token', _ssl_context=lambda: None,
        pick_transcript=lambda rows: rows[0] if rows else None,
        render_transcript_json=lambda content: '[00:01:00] Alex: Prepare the report.' if content else '')


def test_recording_selection_is_by_occurrence_and_attribute_order():
    messages = [card('20261021', item='older'), card(item='latest'), card(item='latest')]
    assert [r['item_id'] for r in ms.recording_cards(messages)] == ['latest']
    assert [r['item_id'] for r in ms.recording_cards(messages, '2026-10-21')] == ['older']
    assert not ms.recording_cards(messages, '2026-10-20')
    nested = {'properties': {'cards': [{'body': card()['content']}]}}
    assert ms.recording_cards([nested])[0]['date'] == '2026-10-22'


def test_stream_uses_existing_sign_in_and_writes_source_linked_transcript(tmp_path, monkeypatch):
    graph = SimpleNamespace(try_get=lambda path: {'name': 'Recording.mp4', 'webUrl': 'https://amdcloud-my.sharepoint.com/personal/owner/Documents/Recording.mp4'})
    h = helper()
    calls = []
    h.foci_swap = lambda original, audience: calls.append((original, audience)) or 'test-token'
    monkeypatch.setattr(ms, 'recap_helper', lambda: h)
    def get(site, path, token, ssl):
        assert token == 'test-token'
        return (200, 'content') if '/streamContent?' in path else (200, json.dumps({'value': [{'id': 'transcript', 'languageTag': 'en-US'}]}))
    monkeypatch.setattr(ms, 'stream_get', get)
    rows, diagnostics = ms.collect_recording_transcripts(graph, [card()], tmp_path)
    assert diagnostics == [] and rows[0]['status'] == 'downloaded'
    assert calls == [(graph, 'https://amdcloud-my.sharepoint.com')]
    assert rows[0]['source'].endswith('Recording.mp4')
    assert Path(rows[0]['path']).read_text() == '[00:01:00] Alex: Prepare the report.'


def test_recording_url_cannot_send_token_to_untrusted_host(tmp_path, monkeypatch):
    h = helper()
    h.foci_swap = lambda *args: (_ for _ in ()).throw(AssertionError('must not mint token'))
    monkeypatch.setattr(ms, 'recap_helper', lambda: h)
    graph = SimpleNamespace(try_get=lambda path: {'webUrl': 'https://evil.example/personal/owner/Recording.mp4'})
    rows, _ = ms.collect_recording_transcripts(graph, [card()], tmp_path)
    assert rows[0]['status'] == 'recording_unavailable'
    assert not list(tmp_path.iterdir())


def test_missing_helper_and_no_transcript_are_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr(ms, 'recap_helper', lambda: None)
    rows, diagnostics = ms.collect_recording_transcripts(None, [card()], tmp_path)
    assert not rows and diagnostics[0]['status'] == 'helper_unavailable'
    monkeypatch.setattr(ms, 'recap_helper', helper)
    monkeypatch.setattr(ms, 'stream_get', lambda *args: (200, '{"value":[]}'))
    graph = SimpleNamespace(try_get=lambda path: {'webUrl': 'https://amdcloud-my.sharepoint.com/personal/owner/Recording.mp4'})
    rows, _ = ms.collect_recording_transcripts(graph, [card()], tmp_path)
    assert rows[0]['status'] == 'no_transcript'
    assert not list(tmp_path.iterdir())


def test_stream_bounds_response_and_does_not_follow_redirects(monkeypatch):
    import io
    seen = []
    class Response(io.BytesIO):
        status = 200
    class Opener:
        def open(self, request, **kwargs):
            seen.append(request)
            return Response(b'x' * 11)
    monkeypatch.setattr(ms, 'MAX_CONTENT', 10)
    handlers = []
    monkeypatch.setattr(ms.urllib.request, 'build_opener', lambda *args: handlers.extend(args) or Opener())
    code, body = ms.stream_get('https://amdcloud-my.sharepoint.com', '/_api/transcripts', 'test-token', lambda: None)
    assert code == 'too_large' and body == ''
    assert isinstance(handlers[0], ms.NoRedirect)
    assert handlers[0].redirect_request(None, None, None, None, None, None) is None
    assert seen[0].get_header('Authorization') == 'Bearer test-token'


def test_stream_file_is_registered_for_existing_summary_workflow(tmp_path, monkeypatch):
    from cos import tools
    from cos.db import Store
    monkeypatch.setattr(tools, 'DATA', tmp_path)
    store = Store(tmp_path / 'jobs.db')
    job = store.create_job('Summarize meeting', 'Communications')
    def run(argv, **kwargs):
        assert json.loads(argv[5]) == {'date': '2026-10-22'}
        path = Path(argv[4]) / 'transcript.txt'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('[00:01:00] Alex: Prepare the report.', encoding='utf-8')
        return SimpleNamespace(returncode=0, stdout=json.dumps({'artifacts': [{'status': 'downloaded', 'path': str(path), 'source': 'https://amdcloud-my.sharepoint.com/recording.mp4'}]}))
    monkeypatch.setattr(tools.subprocess, 'run', run)
    runner = tools.Tools(store)
    result = runner.execute(job, 'meeting_transcripts', {'chat_id': 'meeting', 'date': '2026-10-22'})
    file = result['artifacts'][0]
    saved = runner.execute(job, 'save_meeting_summary', {'source_file_id': file['file_id'], 'title': 'Meeting', 'summary': 'Alex will prepare the report.', 'evidence': 'Alex: Prepare the report.'})
    assert saved['status'] == 'active'
    assert saved['source'].endswith('recording.mp4')
