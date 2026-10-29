"""Keep Teams visual evidence visible without treating source HTML as instructions."""
import hashlib
import json
from html.parser import HTMLParser
from pathlib import Path


class Content(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.images = [], []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {'script', 'style'}:
            self.hidden += 1
        if self.hidden:
            return
        if tag in {'br', 'p', 'div', 'li', 'tr'}:
            self.parts.append('\n')
        picture = tag == 'uriobject' and str(attrs.get('type', '')).lower().startswith('picture')
        if tag == 'img' or picture:
            image = {k: attrs[k] for k in ('src', 'alt', 'itemid') if attrs.get(k)}
            if picture and not image.get('src'):
                uri = attrs.get('uri', '')
                image['src'] = uri.rstrip('/') + '/views/imgo' if uri else attrs.get('thumbnail', '')
            if image not in self.images:
                self.images.append(image)
                self.parts.append('\n[Teams image: ' + json.dumps(image, ensure_ascii=False) + ']\n')
        if tag == 'attachment' or (tag == 'uriobject' and not picture):
            self.parts.append('\n[Teams attachment: ' + json.dumps(attrs, ensure_ascii=False) + ']\n')
        if tag == 'a' and attrs.get('href'):
            self.parts.append('[' + attrs['href'] + '] ')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in {'script', 'style'}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def parse(content, kind=''):
    parser = Content()
    if kind == 'Text':
        parser.parts.append(content or '')
    else:
        parser.feed(content or '')
    return parser


def text(content, kind=''):
    return ''.join(parse(content, kind).parts).strip()


def download_images(client, messages, target):
    """Download only server-selected source images, through the skill's host guard."""
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    records = []
    client.get_skypetoken()
    for message in messages:
        record = {'message_id': message['id'], 'saved': [], 'errors': []}
        for index, image in enumerate(parse(message['content']).images):
            url = image.get('src', '')
            if not client.is_trusted_media_url(url):
                record['errors'].append('Image reference is unavailable or outside the trusted Teams media service; not fetched.')
                continue
            key = hashlib.sha256((str(message['id']) + ':' + str(index)).encode()).hexdigest()
            path = target / (key + '.bin')
            try:
                client.download_object(url, path)
                raw = path.read_bytes()
                suffix = ('.png' if raw.startswith(b'\x89PNG\r\n\x1a\n') else
                          '.jpg' if raw.startswith(b'\xff\xd8\xff') else
                          '.gif' if raw.startswith((b'GIF87a', b'GIF89a')) else
                          '.webp' if raw.startswith(b'RIFF') and raw[8:12] == b'WEBP' else '')
                if not suffix or len(raw) > 25 * 1024 * 1024:
                    raise ValueError('Unsupported image format or image exceeds 25 MB.')
                destination = path.with_suffix(suffix)
                path.replace(destination)
                record['saved'].append(str(destination))
            except (Exception, SystemExit):
                # Do not expose authentication output or silently call the image empty.
                record['errors'].append('Teams image could not be downloaded or decoded.')
            finally:
                path.unlink(missing_ok=True)
        records.append(record)
    return {'messages': records}


def pending_images(store, job_id):
    """Require visual reads, or a recorded access gap, before finishing triage."""
    coverage = store.state('teams_images:' + job_id, {})
    viewed = store.state('viewed_images:' + job_id, [])
    pending = []
    for source in store.rows("SELECT * FROM communication_sources WHERE job_id=? AND source='teams'", (job_id,)):
        if not parse(source['body']).images:
            continue
        digest = hashlib.sha256(source['body'].encode()).hexdigest()
        record = coverage.get(source['message_id'], {})
        if record.get('digest') != digest or any(f['file_id'] not in viewed for f in record.get('files', [])):
            pending.append(source['message_id'])
    return pending


def inspected_image(store, job_id, message_id):
    source = store.one("SELECT body FROM communication_sources WHERE job_id=? AND source='teams' AND message_id=?", (job_id, message_id))
    if not source or not parse(source['body']).images:
        return False
    record = store.state('teams_images:' + job_id, {}).get(message_id, {})
    viewed = store.state('viewed_images:' + job_id, [])
    return (record.get('digest') == hashlib.sha256(source['body'].encode()).hexdigest()
            and bool(record.get('files')) and not record.get('errors')
            and all(f['file_id'] in viewed for f in record['files']))


def empty_historical_gap(store, job, brief):
    """A blank legacy source alone cannot establish an owner obligation."""
    state = json.loads(job['messages'])
    if (not isinstance(state, dict) or not state.get('historical_import') or
            brief.get('action_needed') != 'unknown' or brief.get('response_needed') == 'yes'):
        return False
    sources = store.rows('SELECT * FROM communication_sources WHERE job_id=?', (job['id'],))
    if not sources or any(s['source'] != 'teams' or text(s['body']) for s in sources):
        return False
    for row in store.rows('''SELECT m.payload FROM teams_topic_messages m JOIN communication_threads t
            ON t.thread_id=m.topic_id AND t.source='teams' WHERE t.job_id=?''', (job['id'],)):
        record = json.loads(row['payload'])
        if record.get('record_kind') in {'message', 'owner_feedback', 'owner_preference'} and text(record.get('content') or record.get('text', '')):
            return False
    return not store.one("SELECT 1 FROM review_messages WHERE job_id=? AND role='user'", (job['id'],))
