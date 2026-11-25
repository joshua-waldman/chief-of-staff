"""Prompted owner inbox. Source messages and model tools cannot invoke this route."""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import settings
from .db import now, uid
from .commitments import Commitments

PAGE_SIZE = 6000
LIST_SIZE = 6
ATTENTION = ('waiting_input', 'awaiting_approval', 'blocked', 'failed', 'capacity_paused')
ALIASES = {
    'inbox': 'inbox', 'help inbox': 'inbox',
    'attention': 'attention', 'needs attention': 'attention',
    'what needs my attention': 'attention', 'what needs attention': 'attention',
    'reviews': 'reviews', 'ready for review': 'reviews',
    "what's ready for review": 'reviews', 'what is ready for review': 'reviews',
}
COMMANDS = {'show', 'more', 'approve', 'reject', 'answer', 'revise', 'reviewed',
            'file', 'accept', 'complete', 'dismiss', 'retry-capacity'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def send_reply(chat_id, body):
    # Only called by the authenticated self-chat router, never exposed as a tool.
    from .integrations import communication
    return communication('owner_inbox_reply', {'chat_id': chat_id, 'body': body})


class TeamsInbox:
    def __init__(self, store, engine=None, sender=None):
        self.store, self.engine = store, engine
        self.sender = sender or send_reply

    def parse(self, request):
        text = request.strip()
        normalized = re.sub(r'\s+', ' ', text.casefold().replace('’', "'")).rstrip('?.!')
        if normalized in ALIASES:
            return ALIASES[normalized], ''
        head, _, rest = text.partition(' ')
        if head.casefold() == 'status':
            return 'status', rest.strip()
        if head.casefold() in {'attention', 'reviews'} and rest.strip().isdigit():
            return head.casefold(), rest.strip()
        if head.casefold() == 'show' and rest and not re.match(r'^(?:job:|result:|commitment:|monitor\b)', rest):
            return None
        if head.casefold() in COMMANDS-{'show'} and rest and not re.fullmatch(r'[a-f0-9]{16}', rest.split()[0]):
            return None
        if head.casefold() in COMMANDS:
            return head.casefold(), rest.strip()
        return None

    def handle(self, message, request):
        config = settings()
        if not message.get('owner') or message.get('chat_id') != config['self_chat_id']:
            return False
        parsed = self.parse(request)
        if not parsed:
            return False
        chat, mid = message['chat_id'], str(message['id'])
        with self.store.lock, self.store.connect() as c:
            # Older !cos requests already queued as jobs must never be reinterpreted.
            if c.execute("SELECT 1 FROM seen WHERE source='teams-command' AND item_id=?", (mid,)).fetchone():
                return True
            if not c.execute('INSERT OR IGNORE INTO teams_inbox_requests(chat_id,message_id,command,status,created,updated) VALUES(?,?,?,?,?,?)',
                             (chat, mid, request, 'processing', now(), now())).rowcount:
                return True
        try:
            try:
                body, delivered = self.execute(chat, *parsed)
            except ValueError as error:
                body, delivered = 'COS inbox: '+str(error), None
            self.store.write('UPDATE teams_inbox_requests SET status=?,response=?,updated=? WHERE chat_id=? AND message_id=?',
                             ('sending', body, now(), chat, mid))
            self.sender(chat, 'COS inbox\n\n'+body)
            if delivered:
                delivered()
            self.store.write('UPDATE teams_inbox_requests SET status=?,updated=? WHERE chat_id=? AND message_id=?',
                             ('sent', now(), chat, mid))
        except Exception:
            # Claim before any effects. A crash/timeout must never repeat an approval
            # or a potentially delivered reply when the monitoring checkpoint retries.
            self.delivery_issue(chat, mid)
        return True

    def delivery_issue(self, chat, mid):
        identity = digest(['teams-inbox-delivery', chat, mid])[:32]
        title = 'Teams inbox response could not be confirmed'
        question = (
            'A Teams inbox request or its response was interrupted. No automatic replay was attempted. '
            f'Ask {settings()["command_prefix"]} inbox again for current state; inspect the original item before deciding again.')
        with self.store.lock, self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("UPDATE teams_inbox_requests SET status='uncertain',updated=? WHERE chat_id=? AND message_id=?", (now(), chat, mid))
            if c.execute('INSERT OR IGNORE INTO jobs(id,title,request,org,project,status,question,created,updated) VALUES(?,?,?,?,?,?,?,?,?)',
                         (identity, title, title, 'Chief of Staff', '', 'blocked', question, now(), now())).rowcount:
                c.execute('INSERT INTO events(job_id,kind,body,created) VALUES(?,?,?,?)',
                          (identity, 'teams_inbox_delivery', json.dumps({'chat_id': chat, 'message_id': mid}), now()))

    def recover(self):
        # Called before the monitor starts: no processing rows belong to live calls.
        rows = self.store.rows("SELECT chat_id,message_id FROM teams_inbox_requests WHERE status IN ('processing','sending')")
        for row in rows:
            self.delivery_issue(row['chat_id'], row['message_id'])

    def save(self, kind, chat, **data):
        token = uid()[:16]
        self.store.set_state('teams_inbox:'+token, {'kind': kind, 'chat': chat,
            'expires': (datetime.now(timezone.utc)+timedelta(hours=24)).isoformat(), **data, 'format_version':1})
        return token

    def load(self, token, chat, kind=None):
        value = self.store.state('teams_inbox:'+token, {})
        if (not value or value.get('format_version')!=1 or value['chat'] != chat or value['expires'] < now()
                or kind and value['kind'] != kind):
            raise ValueError('That reference expired or belongs to an older layout. Request a fresh list or preview.')
        return value

    def page(self, chat, text, delivered=None):
        from .teams_formatting import page_end,fragment
        footer='\n\nContinued: '+settings()['command_prefix']+' more '+'f'*16
        end=page_end(text,0,PAGE_SIZE,footer) if text else 0
        if end == len(text):
            return fragment(text,0,end), delivered
        # Persist the remainder; continuation never re-renders newer content under
        # an old preview token. The final page enables its confirmation commands.
        token = self.save('page', chat, text=text, offset=end, preview=delivered)
        return fragment(text,0,end)+'\n\nContinued: '+settings()['command_prefix']+' more '+token, None

    def enable(self, token):
        value = self.store.state('teams_inbox:'+token)
        value['ready'] = True
        self.store.set_state('teams_inbox:'+token, value)

    def execute(self, chat, command, rest):
        prefix = settings()['command_prefix']
        if command == 'inbox':
            return (f'Ask {prefix} what needs my attention? or {prefix} what’s ready for review?\n'
                    f'Use {prefix} attention or {prefix} reviews, then {prefix} show <item reference>.\n'
                    f'Use {prefix} status for top-level tasks, or {prefix} status <task name or ID> for one task.\n'
                    'Details include exact commands for responding, revising, approving, or marking reviewed. '
                    'These commands work only in your configured owner self-chat.'), None
        if command == 'status':
            from .task_status import respond
            return self.page(chat, respond(self.store, rest))
        if command in {'attention', 'reviews'}:
            return self.page(chat, self.list_items(command, int(rest or 0)))
        if command == 'show':
            return self.show(chat, rest)
        if command == 'more':
            with self.store.lock:
                saved = self.load(rest, chat, 'page')
                if saved['offset'] >= len(saved['text']):
                    raise ValueError('All pages of this saved preview have been delivered.')
                from .teams_formatting import page_end,fragment
                end = page_end(saved['text'],saved['offset'],PAGE_SIZE,f'\n\nContinued: {prefix} more {rest}')
                body = fragment(saved['text'],saved['offset'],end)
                final = end >= len(saved['text'])
                if not final:
                    body += f'\n\nContinued: {prefix} more {rest}'
                def sent():
                    saved['offset'] = min(end, len(saved['text']))
                    self.store.set_state('teams_inbox:'+rest, saved)
                    if final and saved['preview']:
                        self.enable(saved['preview'])
                return body, sent
        if command == 'file':
            token, _, file_id = rest.partition(' ')
            with self.store.lock:
                preview = self.valid_preview(token, chat)
                if preview['item_kind'] != 'result':
                    raise ValueError('Choose a file from a saved result preview.')
                review = self.store.result_review(preview['id'])
                if file_id not in review['file_ids']:
                    raise ValueError('Choose a declared file from this result.')
                file = self.store.one('SELECT * FROM files WHERE id=?', (file_id,))
                if not file:
                    raise ValueError('This file is unavailable.')
                from .file_readers import extract
                try:
                    if Path(file['path']).stat().st_size > 25*1024*1024:
                        raise ValueError('This artifact exceeds the 25 MB chat extraction limit.')
                    text = extract(Path(file['path']))
                except (OSError, ValueError) as error:
                    raise ValueError('Artifact text is unavailable. Download the original from job details.') from error
                return self.page(chat, file['name']+'\n\n'+text)
        token, _, text = rest.partition(' ')
        if command == 'retry-capacity':
            if not self.engine:
                raise ValueError('The job engine is unavailable. Try again after the server starts.')
            # Match the scheduler's engine -> store lock order.
            with self.engine.lock, self.store.lock:
                preview = self.valid_preview(token, chat)
                if preview['item_kind'] != 'job' or text:
                    raise ValueError('Use the capacity retry command from a job preview.')
                self.engine.retry_capacity(preview['id'])
            return 'Capacity retry requested.', None
        with self.store.lock:
            preview = self.valid_preview(token, chat)
            kind, identity = preview['item_kind'], preview['id']
            if command == 'reviewed' and kind == 'result' and not text:
                self.store.review_result(identity, preview['revision'], request_id='teams:'+token)
                return 'Marked this saved result reviewed. Commitments and external actions are unchanged.', None
            if kind == 'commitment' and command in {'accept', 'complete', 'dismiss'}:
                Commitments(self.store).review(identity, {'action': command,
                    'expected_version': preview['version'], 'note': text})
                return 'Commitment updated: '+command+'.', None
            if not self.engine:
                raise ValueError('The job engine is unavailable. Try again after the server starts.')
            if kind == 'job' and command in {'approve', 'reject'} and not text:
                if not preview.get('approval_id'):
                    raise ValueError('This item has no pending approval. Refresh its details.')
                self.engine.decide(preview['approval_id'], command == 'approve', wait=False)
                return ('Approval recorded; execution will run in the background. This does not confirm success.'
                        if command == 'approve' else 'Action rejected; COS will reassess.'), None
            if command == 'revise' and kind in {'job', 'result'} and text.strip():
                from .reviews import submit
                submit(self.engine, identity, text)
                return 'Revision direction recorded on the original request.', None
            if command == 'answer' and kind == 'job' and text.strip():
                self.engine.answer(identity, text)
                return 'Answer recorded; COS will resume the original request.', None
            raise ValueError('Use an exact command shown at the end of this item’s preview.')

    def list_items(self, category, offset):
        if not 0 <= offset <= 1000000:
            raise ValueError('Use a nonnegative list offset up to 1000000.')
        rows = []
        prefix=settings()['command_prefix']
        if category == 'reviews':
            from .job_names import ellipsize
            rows = [{'job':ellipsize(r['review_title'],120),
                     'context':ellipsize(r['summary'],160),
                     'status':'Ready for review',
                     'next_step':'Open the saved result to review it; marking reviewed does not send content or complete commitments.',
                     'command':prefix+' show result:'+r['id'],
                     'updated':self._list_timestamp(r.get('completed'))}
                    for r in self.store.pending_results()]
            heading = 'Ready for review'
        else:
            heading = 'Needs attention'
            jobs = self.store.rows('SELECT id,parent_id,title,org,project,status,question FROM jobs WHERE status IN (?,?,?,?,?) ORDER BY updated DESC,id', ATTENTION)
            from .job_names import add_display_names
            from .task_status import title,label,timestamp
            add_display_names(self.store,jobs)
            from .job_names import ellipsize
            for j in jobs:
                saved=self.store.one('SELECT body FROM job_briefs WHERE job_id=?',(j['id'],))
                brief=json.loads(saved['body']) if saved else {}
                context=next((' '.join(str(brief.get(key) or '').split()) for key in
                              ('context','requested_action','email_excerpt') if brief.get(key)), '')
                context=context or ' · '.join(value for value in (j.get('org'),j.get('project')) if value)
                rows.append({'job':title(j),
                             'context':ellipsize(context or 'Task requiring owner attention',160),
                             'status':label(j['status']),
                             'next_step':j['question'] or 'Open the task to provide the missing information or inspect its current state.',
                             'command':prefix+' show job:'+j['id'],
                             'updated':timestamp(j.get('updated'))})
            commitments = Commitments(self.store).search(limit=100)
            # The register includes all active proposals and accepted obligations.
            for start in range(0, commitments['total'], 100):
                page = commitments if start == 0 else Commitments(self.store).search(offset=start, limit=100)
                from .job_names import ellipsize
                rows.extend({'job':ellipsize(r['deliverable'],120),
                             'context':ellipsize(r.get('scope') or 'Commitment',160),
                             'status':r['status'].capitalize()+(' · '+r['deadline_text'] if r['deadline_text'] else ''),
                             'next_step':self._commitment_next_step(r),
                             'command':prefix+' show commitment:'+r['id'],
                             'updated':self._list_timestamp(r.get('updated') or r.get('created'))}
                            for r in page['items'])
            monitor = self.store.state('monitor_status', {})
            if settings()['monitor_enabled'] and (monitor.get('attention_required') or monitor.get('state') == 'needs_attention'):
                rows.append({'job':'Monitoring needs attention',
                             'context':'Mail and Teams source monitoring',
                             'status':'Needs attention',
                             'next_step':str(monitor.get('error', 'Open the monitor details to inspect source errors and access limits.')),
                             'command':prefix+' show monitor',
                             'updated':self._list_timestamp(monitor.get('last_attempt') or monitor.get('started'))})
        selected = rows[offset:offset+LIST_SIZE]
        from .teams_formatting import status_table,render,MAX_HTML_BYTES
        from .job_names import ellipsize
        for row in selected:
            row['job']=ellipsize(row['job'],120)
            row['context']=ellipsize(row['context'],160)
            row['next_step']=ellipsize(row['next_step'],220)
        text = '## '+heading+f'\n\n**{len(rows):,} item(s)**\n\n'
        table_rows=[]
        for row in selected:
            candidate=table_rows+[row]
            body=text+status_table(candidate)
            if len(body)>PAGE_SIZE or len(render('COS inbox\n\n'+body).encode('utf-8'))>MAX_HTML_BYTES:
                if not table_rows:
                    raise ValueError('This inbox row is too large. Open its item directly for details.')
                break
            table_rows=candidate
        text+=status_table(table_rows) if table_rows else 'No items on this page.'
        if offset+len(table_rows) < len(rows):
            text += f'\n\n**More items**\n`{prefix} {category} {offset+len(table_rows)}`'
        text += '\n\nOpen an item with its command in the Next Steps column.'
        return text

    @staticmethod
    def _commitment_next_step(item):
        return {'proposed':'Review and accept or dismiss this proposed commitment.',
                'open':'Confirm completion or dismiss this open commitment.'}.get(item['status'],'Open the commitment details for its current next step.')

    @staticmethod
    def _list_timestamp(value):
        from .task_status import timestamp
        return timestamp(value)

    def snapshot(self, kind, identity):
        if kind == 'job':
            job = self.store.job(identity)
            if not job or job['status'] == 'cancelled':
                raise ValueError('This job is unavailable.')
            approvals = self.store.rows("SELECT * FROM approvals WHERE job_id=? AND status IN ('pending','executing','uncertain','verification_inconclusive') ORDER BY created,id", (identity,))
            brief = self.store.one('SELECT body FROM job_briefs WHERE job_id=?', (identity,))
            incoming = self.store.rows('SELECT id FROM communication_inbox WHERE job_id=? ORDER BY id', (identity,))
            return digest([job['updated'], job['status'], job['question'], approvals, brief, incoming]), job, approvals
        if kind == 'result':
            job = self.store.job(identity)
            review = self.store.result_review(identity)
            if not job or job['parent_id'] or job['status'] == 'cancelled' or not review or review['reviewed']:
                raise ValueError('This saved result is no longer awaiting review.')
            from .review_commitments import snapshot
            with self.store.connect() as c:
                _, _, fingerprint = snapshot(c, {**review, 'file_ids': json.dumps(review['file_ids'])})
            return fingerprint, review, []
        if kind == 'commitment':
            item = Commitments(self.store).detail(identity)
            return digest(item), item, []
        raise ValueError('Use job:<id>, result:<id>, commitment:<id>, or monitor from an inbox list.')

    def valid_preview(self, token, chat):
        preview = self.load(token, chat, 'preview')
        if not preview['ready']:
            raise ValueError('Read all preview pages before responding.')
        current, _, _ = self.snapshot(preview['item_kind'], preview['id'])
        if current != preview['fingerprint']:
            raise ValueError('This item changed. Request a fresh preview before responding.')
        return preview

    def show(self, chat, reference):
        if reference == 'monitor':
            return self.page(chat, 'Monitoring\n'+json.dumps(self.store.state('monitor_status', {}), ensure_ascii=False, indent=2))
        kind, _, identity = reference.partition(':')
        with self.store.lock:
            fingerprint, item, approvals = self.snapshot(kind, identity)
            fields = {'revision': item['revision']} if kind == 'result' else {'version': item['version']} if kind == 'commitment' else {}
            pending = [a for a in approvals if a['status'] == 'pending']
            if len(pending) == 1:
                fields['approval_id'] = pending[0]['id']
            token = self.save('preview', chat, item_kind=kind, id=identity, fingerprint=fingerprint, ready=False, **fields)
            p = settings()['command_prefix']
            commands = []
            if kind == 'result':
                body = item['review_title']+'\n\n'+item['summary']
                for file_id in item['file_ids']:
                    file = self.store.one('SELECT name FROM files WHERE id=?', (file_id,))
                    body += '\n\nArtifact: '+(file['name'] if file else 'Unavailable file')+f'\nRead text: {p} file {token} {file_id}'
                body += '\n\nArtifact text omits visual layout; use the web app to download originals when needed.'
                commands = [f'{p} reviewed {token}', f'{p} revise {token} <direction>']
            elif kind == 'commitment':
                body = (item['deliverable']+'\nOwner: '+item['owner']+'\nStatus: '+item['status']+
                        '\nScope: '+item['scope']+'\nDeadline: '+(item['deadline_text'] or 'Unknown'))
                if item['deadline_date']:
                    body += '\nRecorded date: '+item['deadline_date']+' ('+item['deadline_certainty']+')'
                if item['execution']:
                    body += '\nCOS execution: '+item['execution']['status']+'\n'+item['execution']['next_step']
                for evidence in item['evidence']:
                    body += '\n\n'+evidence['kind']+' evidence · '+evidence['source_timestamp']+'\n'+evidence['excerpt']
                    if evidence['source_url']:
                        body += '\nSource: '+evidence['source_url']
                if item['status'] == 'proposed':
                    commands.append(f'{p} accept {token}')
                if item['status'] == 'open':
                    commands.append(f'{p} complete {token} <your completion confirmation>')
                if item['status'] in {'proposed', 'open'}:
                    commands.append(f'{p} dismiss {token} <reason>')
            else:
                body = item['title']+'\nStatus: '+item['status']+'\n\n'+item['question']
                brief = self.store.one('SELECT body FROM job_briefs WHERE job_id=?', (identity,))
                if brief:
                    brief = json.loads(brief['body'])
                    for key, label in (('status_summary', 'Summary'), ('context', 'Context'),
                                       ('requested_action', 'Requested action'), ('next_step', 'Next step'),
                                       ('uncertainties', 'Uncertainties')):
                        if brief.get(key):
                            body += '\n\n'+label+': '+str(brief[key])
                for a in approvals:
                    body += '\n\nAction: '+a['tool']+'\nStatus: '+a['status']+'\nReason: '+a['reason']+'\nExact arguments and diffs:\n```json\n'+json.dumps(json.loads(a['args']), ensure_ascii=False, indent=2)+'\n```'
                if len(pending) == 1:
                    commands.extend([f'{p} approve {token}', f'{p} reject {token}'])
                elif len(pending) > 1:
                    body += '\nMultiple pending approvals; use the web app to select an individual action.'
                if item['status'] in {'waiting_input', 'blocked', 'failed'}:
                    commands.append(f'{p} answer {token} <answer or direction>')
                if item['org'] == 'Communications' or self.store.result_review(identity):
                    commands.append(f'{p} revise {token} <direction>')
                if item['status'] == 'capacity_paused':
                    commands.append(f'{p} retry-capacity {token}')
            body += '\n\nEnd of saved preview. Commands:\n'+'\n'.join(commands or ['No chat action available for this item.'])
            body += '\nApproval does not acknowledge a result. Marking reviewed does not send content or complete commitments.'
            text, callback = self.page(chat, body, token)
            # page() stores only serializable preview IDs for continuation pages.
            return text, (lambda: self.enable(token)) if callback else None
