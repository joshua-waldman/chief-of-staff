"""Reuse Claude's meeting-recap helper and the installed Teams sign-in."""
import hashlib
import html
import importlib.util
import json
import re
import urllib.error
import urllib.request
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urlparse

MAX_CONTENT = 10 * 1024 * 1024


def recap_helper():
    from .integrations import skill_dir
    try:
        path = skill_dir('meeting-recap') / 'scripts/fetch_transcript_graph.py'
    except ValueError:
        path = Path.home() / '.claude/plugins/marketplaces/slai-marketplace/skills/meeting-recap/scripts/fetch_transcript_graph.py'
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location('cos_claude_meeting_recap', path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper


def recording_cards(messages, requested_date=None):
    if requested_date:
        date.fromisoformat(requested_date)
    class Cards(HTMLParser):
        def __init__(self):
            super().__init__()
            self.items = []
        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if a.get('type') == 'onedriveForBusinessVideo' and a.get('driveid') and a.get('driveitemid'):
                self.items.append((a['driveid'], a['driveitemid']))
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for child in value.values(): yield from strings(child)
        elif isinstance(value, list):
            for child in value: yield from strings(child)
    found = {}
    for message in messages:
        texts = list(strings([message.get('content', ''), message.get('properties') or {}]))
        parser = Cards()
        for text in texts:
            parser.feed(html.unescape(text))
        match = re.search(r'(20[0-9]{6})_[0-9]', ' '.join(texts))
        day = (match[1][:4] + '-' + match[1][4:6] + '-' + match[1][6:8]
               if match else (message.get('composetime') or message.get('originalarrivaltime') or '')[:10])
        for drive, item in parser.items:
            found[(drive, item)] = {'drive_id': drive, 'item_id': item, 'date': day}
    selected_date = requested_date or max((r['date'] for r in found.values()), default='')
    return [r for r in found.values() if r['date'] == selected_date]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def stream_get(site_root, path, token, ssl_context):
    # Stream content URLs from response bodies are not trusted destinations.
    req = urllib.request.Request(site_root + path, headers={
        'Authorization': 'Bearer ' + token, 'Accept': 'application/json'})
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl_context()))
    try:
        with opener.open(req, timeout=60) as response:
            body = response.read(MAX_CONTENT + 1)
            if len(body) > MAX_CONTENT:
                return 'too_large', ''
            return response.status, body.decode('utf-8-sig')
    except urllib.error.HTTPError as error:
        return error.code, ''


def collect_recording_transcripts(graph, messages, target, requested_date=None):
    cards = recording_cards(messages, requested_date)
    if not cards:
        return [], [{'stage': 'stream', 'status': 'no_recording',
                     'next_step': 'No recording card found for the requested occurrence in the scanned chat messages.'}]
    try:
        helper = recap_helper()
    except (Exception, SystemExit):
        helper = None
    if helper is None:
        return [], [{'stage': 'stream', 'status': 'helper_unavailable',
                     'next_step': "Install the existing Claude meeting-recap skill; its fetch_transcript_graph.py helper uses the shared Microsoft sign-in."}]
    rows, diagnostics, tokens = [], [], {}
    for card in cards:
        row = {**card, 'provider': 'claude-meeting-recap', 'status': 'unavailable'}
        try:
            drive, item_id = quote(card['drive_id'], safe=''), quote(card['item_id'], safe='')
            item = graph.try_get('/drives/' + drive + '/items/' + item_id)
            web_url = (item or {}).get('webUrl', '')
            parsed = urlparse(web_url)
            if (parsed.scheme != 'https' or parsed.hostname not in {'amdcloud-my.sharepoint.com', 'amdcloud.sharepoint.com'}
                    or parsed.username or parsed.password or parsed.port not in (None, 443)):
                row['status'] = 'recording_unavailable'
                rows.append(row)
                continue
            audience, site_root = helper.site_root_from_weburl(web_url)
            row.update(source=web_url, name=item.get('name', 'Recording'))
            if audience not in tokens:
                tokens[audience] = helper.foci_swap(graph, audience)
            token = tokens[audience]
            base = '/_api/v2.1/drives/' + drive + '/items/' + item_id + '/media/transcripts'
            code, body = stream_get(site_root, base, token, helper._ssl_context)
            if code != 200:
                row.update(status='forbidden' if code == 403 else 'transcript_list_failed', http_status=code)
                rows.append(row)
                continue
            chosen = helper.pick_transcript(json.loads(body).get('value', []))
            if not chosen:
                row['status'] = 'no_transcript'
                rows.append(row)
                continue
            tid = quote(chosen['id'], safe='')
            code, content = stream_get(site_root, base + '/' + tid + '/streamContent?format=json', token, helper._ssl_context)
            if code != 200:
                row.update(status='forbidden' if code == 403 else 'transcript_download_failed', http_status=code)
            else:
                text = helper.render_transcript_json(content)
                if not text.strip():
                    row['status'] = 'empty_transcript'
                else:
                    identity = hashlib.sha256(('stream:' + drive + ':' + item_id + ':' + tid).encode()).hexdigest()[:24]
                    path = Path(target) / (identity + '.txt')
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text, encoding='utf-8')
                    row.update(status='downloaded', path=str(path), language=chosen.get('languageTag'))
            rows.append(row)
        except (Exception, SystemExit):
            row['status'] = 'stream_request_failed'
            rows.append(row)
    return rows, diagnostics
