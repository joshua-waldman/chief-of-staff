"""Conservative artifact coverage for the selected meeting occurrence.

This describes observed artifacts, not whether the owner owes a reply or work.
Only a completed, accessible scan can establish absence. A transcript file is
positive evidence even when the rest of the scan has gaps.
"""
from pathlib import Path

from .meeting_stream import recording_cards

_FAILURES = {'resolution_failed', 'forbidden_or_missing', 'too_large',
             'download_unavailable', 'download_failed', 'helper_unavailable',
             'recording_unavailable', 'forbidden', 'transcript_list_failed',
             'transcript_download_failed', 'empty_transcript', 'stream_request_failed',
             'unavailable'}
_VIDEO_SUFFIXES = {'.mp4', '.mov', '.webm'}
_TRANSCRIPT_SUFFIXES = {'.vtt', '.srt', '.txt', '.docx'}


def artifact_coverage(messages, rows, diagnostics, continuation='', requested_date=None):
    """Return a bounded, source-linked coverage decision, never an owner-action decision."""
    cards = recording_cards(messages, requested_date)
    stream = [r for r in rows if r.get('provider') == 'claude-meeting-recap']
    shared = [r for r in rows if r.get('provider') != 'claude-meeting-recap']
    card_ids = {(c['drive_id'], c['item_id']) for c in cards}
    observed_ids = {(r.get('drive_id'), r.get('item_id')) for r in stream}
    gaps = []
    if continuation:
        gaps.append('chat_scan_incomplete')
    gaps += [str(d.get('status', 'diagnostic')) for d in diagnostics
             if d.get('status') != 'no_recording' or cards]
    gaps += [str(r.get('status')) for r in rows if r.get('status') in _FAILURES]
    if card_ids - observed_ids or observed_ids - card_ids:
        gaps.append('recording_card_not_resolved')
    # A shared video link is not a verified Stream card, nor transcript text.
    if any(Path(r.get('name') or '').suffix.lower() in _VIDEO_SUFFIXES for r in shared):
        gaps.append('unverified_video_link')
    # Do not interpret unrecognized provider statuses or undiscovered transcript links
    # as an accessible, negative transcript scan.
    for row in rows:
        status = row.get('status', 'missing_status')
        name = row.get('name') or ''
        suffix = Path(name).suffix.lower()
        if status not in {'metadata_only', 'downloaded', 'no_transcript'} | _FAILURES:
            gaps.append('unrecognized_artifact_status')
        if status == 'metadata_only' and (not name or suffix in _TRANSCRIPT_SUFFIXES and (
                suffix not in {'.txt', '.docx'} or 'transcript' in name.lower())):
            gaps.append('unexamined_transcript_link')
        if status == 'downloaded' and row.get('provider') != 'claude-meeting-recap' and not (
                suffix in _TRANSCRIPT_SUFFIXES and (suffix not in {'.txt', '.docx'} or 'transcript' in name.lower())):
            gaps.append('unverified_download')
    transcript = [r for r in rows if r.get('status') == 'downloaded' and (
        r.get('provider') == 'claude-meeting-recap' or
        Path(r.get('name') or '').suffix.lower() in _TRANSCRIPT_SUFFIXES and (
            Path(r.get('name') or '').suffix.lower() not in {'.txt', '.docx'} or
            'transcript' in (r.get('name') or '').lower()))]
    complete = not gaps
    no_transcript = bool(cards) and len(stream) == len(cards) and all(
        r.get('status') == 'no_transcript' for r in stream)
    state = ('transcript_present' if transcript else
             'unknown' if not complete else
             'recording_only' if no_transcript else
             'unknown' if cards else 'absent')
    return {
        'state': state,
        'scan_complete': complete,
        'recording': 'present' if cards else 'unknown' if not complete else 'absent',
        'transcript': 'present' if transcript else 'unknown' if not complete else 'absent',
        'selected_date': requested_date or max((c['date'] for c in cards), default=''),
        'recording_cards': cards,
        'transcript_sources': [{'source': r.get('source', ''), 'name': r.get('name', ''),
                                'provider': r.get('provider', 'shared_file'), 'status': r['status']}
                               for r in transcript],
        'gaps': list(dict.fromkeys(gaps)),
        'scope': 'Scanned chat messages and their discoverable shared links; shared links are not occurrence-date filtered.',
    }
