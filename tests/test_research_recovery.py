import json
from pathlib import Path

import httpx
import pytest

from cos import config, progress, recovery, research, tools
from cos.briefs import update_brief
from cos.context import prepare
from cos.db import Store
from cos.engine import Engine, SCHEMA, session


URL = 'https://learn.microsoft.com/en-us/graph/api/message-update'


def test_saved_research_pages_count_progress_and_repeated_ranges_do_not(env):
    job=env.create_job('Read saved documentation','Technology')
    state={}
    recovery.record(env,job['id'],state,'fetch_url',{'url':URL},{'source':URL,'text':'Evidence '+('x'*18000)})
    path=next(iter(state['research_sources'].values()))['evidence_path']
    for offset in (0,8000):
        result=tools.Tools(env).execute(job,'read_file',{'path':path,'offset':offset,'limit':8000})
        progress.observe(state,'read_file',{'path':path,'offset':offset},result)
        assert state['stalled_steps']==0
    progress.observe(state,'read_file',{'path':path,'offset':8000},result)
    assert state['stalled_steps']==1
    assert state['reads_since_brief']==2


def test_restart_hydrates_saved_research_coverage_without_resetting_stall(env):
    job=env.create_job('Resume documentation','Technology')
    path='context/research-'+'a'*20+'.txt'
    result={'source':path,'sha256':'digest','text':'Verified source','truncated':False}
    env.write('INSERT INTO approvals(id,job_id,tool,args,status,created,result) VALUES(?,?,?,?,?,?,?)',
        ('page',job['id'],'read_file',json.dumps({'path':path}), 'executed','now',json.dumps(result)))
    state={'stalled_steps':7}
    progress.hydrate_reads(env,job['id'],state)
    assert state['stalled_steps']==7
    assert next(iter(state['file_reads'].values()))['complete']
    progress.observe(state,'read_file',{'path':path},result)
    assert state['stalled_steps']==8


@pytest.mark.parametrize('name',['checkpoint-'+'a'*20+'.txt','history-'+'b'*20+'.txt','content-'+'c'*20+'.txt','research-fake.txt'])
def test_recovery_bookkeeping_still_never_counts_as_source_progress(name):
    state={}
    path='context/'+name
    progress.observe(state,'read_file',{'path':path},{'source':path,'sha256':'digest','text':'archive','truncated':False})
    assert state['stalled_steps']==1
    assert not state.get('file_reads')


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    monkeypatch.setattr(config, 'DATA', tmp_path / 'data')
    monkeypatch.setattr(tools, 'DATA', tmp_path / 'data')
    monkeypatch.setitem(config.DEFAULTS, 'research_hosts', ['learn.microsoft.com', 'docs.example.com'])
    monkeypatch.setattr(research.socket, 'getaddrinfo', lambda *a: [(2, 1, 6, '', ('20.70.246.20', 443))])
    return Store(tmp_path / 'test.db')


def test_learn_redirect_and_display_selectors(env, monkeypatch):
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        if len(calls) == 1:
            return httpx.Response(301, headers={'location': '/en-us/graph/api/message-update?view=graph-rest-1.0&tabs=http'}, request=httpx.Request('GET', url))
        return httpx.Response(200, text='<p>Official evidence</p>', request=httpx.Request('GET', url))
    monkeypatch.setattr(research.httpx, 'get', get)
    result = tools.Tools(env).execute(env.create_job('Research', 'Technology'), 'fetch_url', {'url': URL})
    assert result['requested_url'] == URL
    assert result['source'] == URL + '?view=graph-rest-1.0&tabs=http'
    assert 'Official evidence' in result['text']
    assert len(calls) == 2 and all(not kw['follow_redirects'] for _, kw in calls)


@pytest.mark.parametrize('destination', [
    'https://other.example.com/page', 'http://learn.microsoft.com/page',
    URL + '?secret=private', URL + '?view=graph-rest-1.0&view=graph-rest-beta',
    URL + '?tabs=arbitrary', 'https://docs.example.com/?view=graph-rest-beta',
    'https://user:pass@learn.microsoft.com/page', 'https://learn.microsoft.com:8443/page',
])
def test_unsafe_redirect_never_contacted(env, monkeypatch, destination):
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        return httpx.Response(302, headers={'location': destination}, request=httpx.Request('GET', url))
    monkeypatch.setattr(research.httpx, 'get', get)
    with pytest.raises(ValueError):
        research.fetch_url(URL)
    assert calls == [URL]


def test_private_redirect_and_loops_rejected(env, monkeypatch):
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        return httpx.Response(301, headers={'location': 'https://docs.example.com/page'}, request=httpx.Request('GET', url))
    monkeypatch.setattr(research.httpx, 'get', get)
    monkeypatch.setattr(research.socket, 'getaddrinfo', lambda host, *a: [(2, 1, 6, '', ('127.0.0.1' if host == 'docs.example.com' else '20.70.246.20', 443))])
    with pytest.raises(ValueError, match='Private network'):
        research.fetch_url(URL)
    assert calls == [URL]
    monkeypatch.setattr(research.socket, 'getaddrinfo', lambda *a: [(2, 1, 6, '', ('20.70.246.20', 443))])
    with pytest.raises(ValueError, match='loop'):
        research.fetch_url('https://docs.example.com/page')


def test_alternate_transport_clears_matching_error_and_keeps_other_errors(env):
    job = env.create_job('Research', 'Technology')
    state = {}
    update_brief(env, job, {'context': 'Research scope', 'status_summary': 'Cannot retrieve documentation', 'next_step': 'Enable host'})
    recovery.record(env, job['id'], state, 'fetch_url', {'url': URL}, '301 redirect', error=True)
    recovery.record(env, job['id'], state, 'read_file', {'path': 'missing.txt'}, 'Missing attachment', error=True)
    recovery.record(env, job['id'], state, 'browser_read', {'url': URL}, {'url': URL + '?view=graph-rest-1.0', 'text': 'Verified source text'})
    assert len(state['active_errors']) == 1
    assert next(iter(state['active_errors'].values()))['tool'] == 'read_file'
    brief = json.loads(env.one('SELECT body FROM job_briefs WHERE job_id=?', (job['id'],))['body'])
    assert brief['context'] == 'Research scope'
    assert 'other tool failures' in brief['status_summary']
    assert env.one("SELECT 1 FROM events WHERE job_id=? AND kind='tool_recovered'", (job['id'],))
    source = recovery.guidance(state)['research_sources'][0]
    assert json.loads(Path(source['evidence_path']).read_text(encoding='utf-8'))['text'] == 'Verified source text'


def test_host_enablement_clears_only_allowlist_failures(env):
    job = env.create_job('Research', 'Technology')
    state = {}
    recovery.record(env, job['id'], state, 'fetch_url', {'url': URL}, 'Add this exact research host in Connections before retrieval.', error=True)
    other = URL.replace('message-update', 'message-delete')
    recovery.record(env, job['id'], state, 'fetch_url', {'url': other}, '403 Forbidden', error=True)
    recovery.record(env, job['id'], state, 'browser_read', {'url': URL.replace('message-update', 'message-move')}, {'url': URL, 'text': 'Source'})
    assert len(state['active_errors']) == 1
    assert next(iter(state['active_errors'].values()))['message'] == '403 Forbidden'


def test_successful_research_survives_forced_compaction(env):
    job = env.create_job('Research', 'Technology')
    state = {'messages': [{'role': 'system', 'content': 'Policy'}, {'role': 'user', 'content': job['request']}]}
    result = {'url': URL, 'text': 'Evidence ' * 3000 + 'decisive final detail'}
    recovery.record(env, job['id'], state, 'browser_read', {'url': URL}, result)
    state['messages'].extend([perform('browser_read', {'url': URL}), {'role': 'tool', 'tool_call_id': 'call-browser_read', 'content': json.dumps(result)}])
    assert prepare(Engine(env), job, state, 'worker', SCHEMA, force=True)
    carry = next(m for m in state['messages'] if m.get('_cos_checkpoint'))
    source = json.loads(carry['content'])['tool_recovery']['research_sources'][0]
    assert 'decisive final detail' in Path(source['evidence_path']).read_text(encoding='utf-8')
    saved = session(env.job(job['id']))
    assert saved['research_sources'] == state['research_sources']


def perform(action, args):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call-' + action, 'type': 'function',
        'function': {'name': 'perform', 'arguments': json.dumps({'action': action, 'arguments': args})}}]}


def test_engine_retry_clears_current_status_and_preserves_history(env, monkeypatch):
    class Model:
        step = 0
        def complete(self, messages, **kwargs):
            self.step += 1
            if self.step <= 2:
                return perform('fetch_url', {'url': URL}), {}
            snapshot = json.dumps(messages)
            assert 'research_sources' in snapshot and 'resolved_errors' in snapshot
            brief = json.loads(env.one('SELECT body FROM job_briefs WHERE job_id=?', (job['id'],))['body'])
            assert 'resolved' in brief['status_summary']
            return perform('finish', {'summary': 'Research completed'}), {}
    job = env.create_job('Research', 'Technology')
    update_brief(env, job, {'status_summary': 'Source unavailable', 'next_step': 'Fix access'})
    engine = Engine(env, Model())
    attempts = []
    def execute(*a):
        attempts.append(a)
        if len(attempts) == 1:
            raise ValueError('Source unavailable')
        return {'source': URL, 'text': 'Official documentation'}
    monkeypatch.setattr(engine.tools, 'execute', execute)
    engine.run(job['id'])
    current = env.job(job['id'])
    assert current['status'] == 'completed' and current['question'] == ''
    assert not session(current)['active_errors']
    assert env.one("SELECT 1 FROM events WHERE job_id=? AND kind='tool_error'", (job['id'],))


def test_resume_replaces_stale_brief_and_question(env):
    job = env.create_job('Research', 'Technology')
    update_brief(env, job, {'context': 'Preserve scope', 'status_summary': 'Host blocked', 'next_step': 'Enable host'})
    env.update(job['id'], status='waiting_input', question='Enable host', messages=json.dumps({'messages': []}))
    Engine(env).answer(job['id'], 'Access is fixed')
    current = env.detail(job['id'])
    assert current['question'] == '' and current['status'] == 'queued'
    assert current['brief']['context'] == 'Preserve scope'
    assert current['brief']['status_summary'].startswith('Resuming')


def test_changing_screenshot_does_not_count_as_research_progress():
    state = {}
    for i in range(5):
        progress.observe(state, 'browser_read', {'url': URL}, {'url': URL, 'text': 'Same evidence', 'screenshot': {'file_id': str(i)}})
    assert state['stalled_steps'] == 4


def test_existing_receipts_restore_sources_and_resolve_old_access_errors(env):
    job = env.create_job('Research', 'Technology')
    rows = [('old-error', 'fetch_url', 'failed', 'Add this exact research host in Connections before retrieval.'),
            ('new-success', 'browser_read', 'executed', json.dumps({'url': URL, 'text': 'Official evidence'}))]
    for i, (identifier, tool, status, result) in enumerate(rows):
        env.write('INSERT INTO approvals(id,job_id,tool,args,status,created,result) VALUES(?,?,?,?,?,?,?)',
                  (identifier, job['id'], tool, json.dumps({'url': URL}), status, str(i), result))
    update_brief(env, job, {'status_summary': 'Host still blocked', 'next_step': 'Enable host'})
    state = {}
    recovery.hydrate(env, job['id'], state)
    assert not state['active_errors']
    assert len(state['research_sources']) == 1
    events = env.rows('SELECT * FROM events WHERE job_id=?', (job['id'],))
    recovery.hydrate(env, job['id'], state)
    assert env.rows('SELECT * FROM events WHERE job_id=?', (job['id'],)) == events
    assert 'resolved' in env.detail(job['id'])['brief']['status_summary']


def test_corrected_arguments_clear_validation_failure_without_clearing_other_target(env):
    job = env.create_job('Research', 'Technology')
    state = {}
    recovery.record(env, job['id'], state, 'update_brief', {'status_summary': 42}, 'Invalid summary', error=True)
    recovery.record(env, job['id'], state, 'update_brief', {'status_summary': 'Current status'}, {'status_summary': 'Current status'})
    assert not state['active_errors']
    recovery.record(env, job['id'], state, 'read_file', {'path': 'missing.txt'}, 'Missing file', error=True)
    recovery.record(env, job['id'], state, 'read_file', {'path': 'other.txt'}, {'text': 'Different file'})
    assert len(state['active_errors']) == 1


def test_evidence_index_stays_bounded_with_full_checkpoint_available(env):
    state = {'research_sources': {str(i): {'url': URL, 'evidence_path': 'evidence.txt', 'excerpt': '資料' * 600} for i in range(50)},
             'active_errors': {str(i): {'tool': 'read_file', 'target': '資料' * 300, 'message': '資料' * 1500} for i in range(20)}}
    result = recovery.guidance(state)
    assert len(json.dumps(result, ensure_ascii=False).encode('utf-8')) <= 6000
    assert result['source_count'] == 50 and result['active_error_count'] == 20


def test_browser_guards_redirects_preserves_screenshots_and_rejects_http_errors(env, monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from playwright import sync_api
    from cos.browser import browser_action
    status = [200]
    destination = [URL + '?view=graph-rest-1.0&tabs=http']
    allowed = []
    class Page:
        main_frame = object()
        url = URL
        def goto(self, url, **kwargs):
            request = SimpleNamespace(url=destination[0], frame=self.main_frame, is_navigation_request=lambda: True)
            route = SimpleNamespace(request=request, continue_=lambda: allowed.append(True), abort=lambda: allowed.append(False))
            browser.guard(route)
            if not allowed[-1]:
                raise ValueError('Navigation blocked')
            self.url = destination[0]
            return SimpleNamespace(status=status[0])
        def locator(self, selector):
            return SimpleNamespace(inner_text=lambda **kw: 'Official documentation')
        def screenshot(self, path, **kwargs):
            Path(path).write_bytes(b'synthetic screenshot')
    class Browser:
        pages = [Page()]
        def route(self, pattern, guard): self.guard = guard
        def close(self): pass
    browser = Browser()
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=lambda *a, **kw: browser))
    monkeypatch.setattr(sync_api, 'sync_playwright', lambda: nullcontext(playwright))
    job = env.create_job('Research', 'Technology')
    first = browser_action(env, job, 'browser_read', {'url': URL})
    second = browser_action(env, job, 'browser_read', {'url': URL})
    assert first['path'] != second['path']
    assert Path(first['path']).exists() and Path(second['path']).exists()
    status[0] = 403
    with pytest.raises(ValueError, match='HTTP 403'):
        browser_action(env, job, 'browser_read', {'url': URL})
    destination[0] = 'https://other.example.com/page'
    with pytest.raises(ValueError, match='Navigation blocked'):
        browser_action(env, job, 'browser_read', {'url': URL})
    assert allowed[-1] is False


def test_restart_recovers_success_committed_after_last_session_snapshot(env):
    job = env.create_job('Research', 'Technology')
    state = {}
    recovery.hydrate(env, job['id'], state)
    env.write('INSERT INTO approvals(id,job_id,tool,args,status,created,result) VALUES(?,?,?,?,?,?,?)',
              ('after-snapshot', job['id'], 'browser_read', json.dumps({'url': URL}), 'executed', 'now', json.dumps({'url': URL, 'text': 'Recovered source'})))
    recovery.hydrate(env, job['id'], state)
    assert len(state['research_sources']) == 1
    assert state['recovery_cursor'] > 0


def test_completed_work_supersedes_unused_method_errors_but_retains_history(env):
    job = env.create_job('Research', 'Technology')
    state = {}
    recovery.record(env, job['id'], state, 'fetch_url', {'url': 'https://docs.example.com/unneeded'}, 'Alternative unavailable', error=True)
    recovery.complete(env, job['id'], state)
    assert not state['active_errors']
    assert state['resolved_errors'][0]['resolution'] == 'superseded_by_completion'
    assert env.one("SELECT 1 FROM events WHERE job_id=? AND kind='errors_superseded'", (job['id'],))
