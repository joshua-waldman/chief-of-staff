"""Evidence-gated meeting coverage; owner preferences are data, not product rules."""
import io
import sys
from types import SimpleNamespace

import pytest

from cos.meeting_coverage import artifact_coverage
from cos.meetings import collect_transcripts
from cos.message_triage import PROMPT

CARD = {'id': 'card-1', 'composetime': '2026-10-22T12:00:00Z',
        'content': '<card type="onedriveForBusinessVideo" driveid="drive" driveitemid="item">Recording-20261022_100000.mp4</card>'}
NO_CARD = {'id': 'notice-1', 'content': 'A meeting ended; no artifact link.'}


def test_complete_absence_requires_both_negative_findings():
    c = artifact_coverage([NO_CARD], [], [{'stage': 'stream', 'status': 'no_recording'}])
    assert (c['state'], c['recording'], c['transcript'], c['scan_complete']) == ('absent', 'absent', 'absent', True)
    assert c['recording_cards'] == [] and c['gaps'] == []


def test_recording_only_and_selected_date():
    row = {'drive_id': 'drive', 'item_id': 'item', 'provider': 'claude-meeting-recap',
           'source': 'https://amdcloud.sharepoint.com/recording', 'status': 'no_transcript'}
    c = artifact_coverage([CARD], [row], [], requested_date='2026-10-22')
    assert c['state'] == 'recording_only'
    assert c['recording'] == 'present' and c['transcript'] == 'absent'
    assert c['selected_date'] == '2026-10-22' and c['recording_cards'][0]['item_id'] == 'item'


@pytest.mark.parametrize('name', ['transcript.vtt', 'transcript.srt', 'meeting-transcript.txt', 'transcript.docx'])
def test_transcript_only_shared_file_is_positive_with_provenance(name):
    row = {'source': 'https://amdcloud.sharepoint.com/transcript', 'name': name,
           'status': 'downloaded', 'path': name}
    c = artifact_coverage([NO_CARD], [row], [{'status': 'no_recording'}])
    assert c['state'] == 'transcript_present' and c['recording'] == 'absent'
    assert c['transcript_sources'][0]['source'] == row['source']


@pytest.mark.parametrize('messages,rows,diagnostics,continuation,gap', [
    ([NO_CARD], [], [{'status': 'no_recording'}], 'next', 'chat_scan_incomplete'),
    ([], [], [{'status': 'chat_unavailable'}], '', 'chat_unavailable'),
    ([CARD], [], [{'status': 'helper_unavailable'}], '', 'helper_unavailable'),
    ([CARD], [{'provider': 'claude-meeting-recap', 'drive_id': 'drive', 'item_id': 'item',
               'status': 'forbidden'}], [], '', 'forbidden'),
    ([CARD], [], [{'status': 'transcript_list_failed'}], '', 'transcript_list_failed'),
    ([NO_CARD], [{'source': 'link', 'status': 'resolution_failed'}], [], '', 'resolution_failed'),
    ([NO_CARD], [{'source': 'link', 'name': 'transcript.vtt', 'status': 'download_failed'}], [], '', 'download_failed'),
    ([NO_CARD], [{'source': 'link', 'name': 'Recording.mp4', 'status': 'metadata_only'}], [], '', 'unverified_video_link'),
    ([NO_CARD], [{'source': 'link', 'name': 'transcript.vtt', 'status': 'metadata_only'}], [], '', 'unexamined_transcript_link'),
    ([NO_CARD], [{'source': 'link', 'status': 'new_status'}], [], '', 'unrecognized_artifact_status'),
    ([CARD], [], [{'status': 'no_recording'}], '', 'no_recording'),
])
def test_gaps_never_become_verified_absence(messages, rows, diagnostics, continuation, gap):
    c = artifact_coverage(messages, rows, diagnostics, continuation)
    assert c['state'] == 'unknown' and not c['scan_complete']
    assert gap in c['gaps']


def test_positive_transcript_with_incomplete_scan_retains_gap():
    c = artifact_coverage([NO_CARD], [{'status': 'downloaded', 'source': 'link', 'name': 'transcript.srt'}], [], 'more')
    assert c['state'] == 'transcript_present' and not c['scan_complete']
    assert c['recording'] == 'unknown'


def test_collector_surfaces_absence_transcript_and_chat_failure(tmp_path, monkeypatch):
    from cos import integrations, meeting_stream
    monkeypatch.setattr(integrations, 'skill_dir', lambda name: tmp_path)
    monkeypatch.setattr(meeting_stream, 'collect_recording_transcripts',
                        lambda graph, messages, target, date: ([], [{'stage': 'stream', 'status': 'no_recording'}]))
    items = {'transcript': {'id': 't', 'name': 'transcript.vtt', 'size': 6,
                            'webUrl': 'https://amdcloud.sharepoint.com/transcript',
                            '@microsoft.graph.downloadUrl': 'https://amdcloud.sharepoint.com/download'}}
    context = {'messages': [NO_CARD], 'continuation': '', 'urls': [], 'fail': False}
    class Skype:
        def list_messages(self, *args, **kwargs):
            if context['fail']:
                raise PermissionError('forbidden')
            return context['messages'], context['continuation']
    monkeypatch.setitem(sys.modules, 'graph_client', SimpleNamespace(GraphClient=lambda: object(), _ssl_context=lambda: None))
    monkeypatch.setitem(sys.modules, 'meeting_artifacts', SimpleNamespace(
        SkypeClient=Skype, extract_urls_from_message=lambda m: context['urls'],
        resolve=lambda graph, url: items[url]))
    import urllib.request
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *a, **kw: io.BytesIO(b'WEBVTT\n00:01:00 Alex: No owner assignment.'))
    result = collect_transcripts('chat', tmp_path / 'out')
    assert result['coverage']['state'] == 'absent'
    assert 'response_needed=no' in result['triage_guidance']
    context['urls'] = ['transcript']
    result = collect_transcripts('chat', tmp_path / 'out')
    assert result['coverage']['state'] == 'transcript_present'
    assert result['artifacts'][0]['status'] == 'downloaded'
    context['fail'] = True
    result = collect_transcripts('chat', tmp_path / 'out')
    assert result['coverage']['state'] == 'unknown'
    assert 'chat_unavailable' in result['coverage']['gaps']


def test_coverage_does_not_suppress_mixed_owner_ask_or_activity():
    c = artifact_coverage([NO_CARD, {'id': 'owner-ask', 'content': 'Joshua, please review the plan.'}], [],
                          [{'status': 'no_recording'}])
    assert c['state'] == 'absent'  # artifact evidence only; never a reply/action verdict
    assert 'response_needed' not in c and 'action_needed' not in c
    assert 'owner' not in str(c['scope']).lower()
    assert 'meeting transcripts could change' in PROMPT  # first-pass unknown pending evidence


def test_new_artifact_requires_fresh_coverage_decision():
    absent = artifact_coverage([NO_CARD], [], [{'status': 'no_recording'}])
    found = artifact_coverage([NO_CARD, CARD], [], [{'status': 'helper_unavailable'}])
    assert absent['state'] == 'absent' and found['state'] == 'unknown'
    assert found['recording'] == 'present'


def test_new_artifact_activity_is_re_evaluated_not_cached():
    initial = artifact_coverage([NO_CARD], [], [{'status': 'no_recording'}])
    row = {'drive_id': 'drive', 'item_id': 'item', 'provider': 'claude-meeting-recap',
           'status': 'downloaded', 'source': 'https://amdcloud.sharepoint.com/transcript'}
    updated = artifact_coverage([NO_CARD, CARD], [row], [])
    assert initial['state'] == 'absent'
    assert updated['state'] == 'transcript_present'
    assert updated['recording_cards'][0]['item_id'] == 'item'
