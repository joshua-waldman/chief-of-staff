"""Meeting evidence ingestion and searchable, source-linked summaries."""
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from .db import now
from .knowledge import clean

TRANSCRIPT_SUFFIXES = {'.vtt', '.srt', '.txt', '.docx'}
MAX_TRANSCRIPT = 10 * 1024 * 1024


def collect_transcripts(chat_id, target, options=None):
    # Run in an isolated process, reusing the installed skill's authentication.
    from .integrations import skill_dir
    sys.path.insert(0, str(skill_dir('slai-teams') / 'scripts'))
    from graph_client import GraphClient, _ssl_context
    import meeting_artifacts as artifacts
    import urllib.request
    graph = GraphClient()
    skype = artifacts.SkypeClient()
    chat_error = None
    try:
        messages, continuation = skype.list_messages(chat_id, page_size=200)
    except (Exception, SystemExit):
        messages, continuation = [], ''
        chat_error = {'stage': 'shared_files', 'status': 'chat_unavailable',
                      'next_step': 'Check access using the installed Teams skill and existing Microsoft sign-in.'}
    from .meeting_stream import collect_recording_transcripts
    rows, diagnostics = collect_recording_transcripts(graph, messages, target, (options or {}).get('date'))
    if chat_error:
        diagnostics.append(chat_error)
    urls = list(dict.fromkeys(u for m in messages for u in artifacts.extract_urls_from_message(m)))
    for url in urls:
        try:
            item = artifacts.resolve(graph, url)
        except (Exception, SystemExit):
            rows.append({'source': url, 'status': 'resolution_failed'})
            continue
        if not item:
            rows.append({'source': url, 'status': 'forbidden_or_missing'})
            continue
        name = item.get('name') or ''
        row = {'source': item.get('webUrl') or url, 'name': name, 'status': 'metadata_only'}
        rows.append(row)
        suffix = Path(name).suffix.lower()
        if suffix not in TRANSCRIPT_SUFFIXES or (suffix in {'.txt', '.docx'} and 'transcript' not in name.lower()):
            continue
        if item.get('size', 0) > MAX_TRANSCRIPT:
            row['status'] = 'too_large'
            continue
        download = item.get('@microsoft.graph.downloadUrl', '')
        if urlparse(download).scheme != 'https':
            row['status'] = 'download_unavailable'
            continue
        # Never forward a Graph/Teams bearer token to a download URL.
        try:
            with urllib.request.urlopen(download, timeout=90, context=_ssl_context()) as response:
                content = response.read(MAX_TRANSCRIPT + 1)
        except Exception:
            row['status'] = 'download_failed'
            continue
        if len(content) > MAX_TRANSCRIPT:
            row['status'] = 'too_large'
            continue
        import hashlib
        identity = (item.get('parentReference', {}).get('driveId', '') + ':' + item['id']).encode()
        path = Path(target) / (hashlib.sha256(identity).hexdigest()[:24] + suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        row.update(status='downloaded', path=str(path))
    return {'artifacts': rows, 'scan_incomplete': bool(continuation), 'diagnostics': diagnostics,
            'limitation': 'Uses the Claude meeting-recap Stream helper and the installed Teams credentials. Defaults to the newest recording date; pass date for another occurrence. Shared-file discovery scans the latest 200 chat messages. Recording audio is not transcribed.'}


def save_summary(store, job, args):
    """Persist observations, never preferences or execution authorization."""
    from .tools import extract
    source_id = args['source_file_id']
    sources = store.state('meeting_sources:' + job['id'], {})
    source = sources.get(source_id)
    record = store.one('SELECT * FROM files WHERE id=? AND job_id=?', (source_id, job['id']))
    if not source or not record:
        raise ValueError('Use a transcript registered by meeting_transcripts in this job.')
    evidence = clean(args['evidence'], 1500)
    if not evidence or evidence not in extract(Path(record['path'])):
        raise ValueError('Evidence must be an exact excerpt from the source transcript.')
    title = clean(args['title'], 160)
    summary = clean(args['summary'], 2400)
    if not title or not summary:
        raise ValueError('Provide a meeting title and summary.')
    content = clean('Meeting observations (verify against current context):\n' + summary +
                    '\nSource: ' + source['source'] + '\nChat ID: ' + source['chat_id'] +
                    '\nTranscript: /api/files/' + source_id + '\nEvidence: ' + evidence)
    # Updating the same transcript refreshes its summary; forgotten entries stay forgotten.
    identity = 'meeting-' + source['identity']
    old = store.one('SELECT * FROM knowledge WHERE id=?', (identity,))
    if old and old['status'] == 'forgotten':
        return {'id': identity, 'status': 'forgotten'}
    store.write('INSERT OR REPLACE INTO knowledge VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (identity, 'lesson', title, content, 'global', 'active', 'meeting-summary',
                 job['id'], old['created'] if old else now(), now(),
                 old['uses'] if old else 0, old['helpful'] if old else 0, identity))
    return {'id': identity, 'status': 'active', 'source': source['source']}


if __name__ == '__main__':
    print(json.dumps(collect_transcripts(sys.argv[1], sys.argv[2],
                                        json.loads(sys.argv[3]) if len(sys.argv) > 3 else None), ensure_ascii=False))
