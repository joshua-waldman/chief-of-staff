"""Bounded downloads of SharePoint documents referenced by Teams source data."""
import hashlib
import html
import re
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

MAX_FILE = 25 * 1024 * 1024
MEDIA = {'.mp4', '.mov', '.mkv', '.avi', '.mp3', '.wav', '.m4a', '.webm'}


def sharepoint_url(url):
    try:
        parsed = urlparse(url)
        return (parsed.scheme == 'https' and not parsed.username and not parsed.password
                and parsed.port in {None, 443}
                and (parsed.hostname or '').lower().endswith('.sharepoint.com'))
    except ValueError:
        return False


def source_urls(message):
    import json
    body = html.unescape((message.get('content') or '') + '\n' + json.dumps(message.get('properties') or {}))
    return list(dict.fromkeys(url for url in
        (u.rstrip(').,;]') for u in re.findall(r'https://[^\s<>"\'\\]+', body)) if sharepoint_url(url)
        and '/_api/' not in urlparse(url).path
        and not re.fullmatch(r'/personal/[^/]+/?',urlparse(url).path)))


class DownloadRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not sharepoint_url(newurl):
            raise ValueError('Download redirected outside SharePoint.')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_item(item, target, context):
    """Never send Microsoft bearer credentials to a preauthenticated URL."""
    name = item.get('name') or 'attachment.bin'
    suffix = Path(name).suffix.lower()
    if item.get('folder') is not None:
        return {'status': 'folder', 'limitation': 'Folder contents were not downloaded.'}
    if suffix in MEDIA:
        return {'status': 'metadata_only', 'limitation': 'Audio/video decoding is unavailable; use meeting_transcripts for meeting speech.'}
    if item.get('size', 0) > MAX_FILE:
        return {'status': 'too_large', 'limitation': 'File exceeds the 25 MB input limit.'}
    url = item.get('@microsoft.graph.downloadUrl', '')
    if not sharepoint_url(url):
        return {'status': 'download_unavailable'}
    opener = urllib.request.build_opener(DownloadRedirect(), urllib.request.HTTPSHandler(context=context))
    with opener.open(url, timeout=30) as response:
        content = response.read(MAX_FILE + 1)
    if len(content) > MAX_FILE:
        return {'status': 'too_large'}
    key = hashlib.sha256((item.get('parentReference', {}).get('driveId', '') + ':' + item['id']).encode()).hexdigest()[:24]
    # Display names never become filesystem paths, even on Windows.
    suffix = suffix if re.fullmatch(r'\.[a-z0-9]{1,12}', suffix) else '.bin'
    path = Path(target) / (key + suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return {'status': 'downloaded', 'path': str(path)}


def collect(graph, messages, target):
    from .integrations import skill_dir
    import sys
    sys.path.insert(0, str(skill_dir('slai-teams') / 'scripts'))
    from graph_client import _ssl_context
    from meeting_artifacts import resolve
    rows, seen, items = [], set(), set()
    downloaded = 0
    for message in messages:
        for url in source_urls(message):
            if url in seen:
                continue
            if len(seen) >= 50:
                return {'artifacts': rows, 'scan_incomplete': True}
            seen.add(url)
            row = {'source': url, 'message_id': str(message['id'])}
            try:
                item = resolve(graph, url)
                if not item:
                    row['status'] = 'forbidden_or_missing'
                else:
                    identity=(str(message['id']),item.get('parentReference',{}).get('driveId'),item.get('id'))
                    if identity in items:
                        continue
                    items.add(identity)
                    row.update(name=item.get('name'), source=item.get('webUrl') or url)
                    if downloaded >= 100 * 1024 * 1024:
                        row.update(status='budget_exceeded',limitation='Download batch reached 100 MB. Narrow the source messages.')
                        rows.append(row)
                        return {'artifacts':rows,'scan_incomplete':True}
                    row.update(download_item(item, target, _ssl_context()))
                    if row['status']=='downloaded':
                        downloaded+=Path(row['path']).stat().st_size
            except (Exception, SystemExit):
                row.update(status='download_failed', limitation='Could not resolve or download this shared file using the existing Microsoft sign-in.')
            rows.append(row)
    return {'artifacts': rows, 'scan_incomplete': False}
