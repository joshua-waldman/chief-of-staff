import base64
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from cos import config, github
from cos.engine import Engine
from cos.tools import Tools, allowed_job_action
from test_workflows import store, Scripted, perform, run


BRANCH = 'feature/AVA-13322/Dimension-Suggestion'
LINK = 'https://github.com/AMD-DID/ava-mcp-server-http/commits/'+BRANCH
SHA = 'a'*40


@pytest.fixture
def routed(monkeypatch):
    monkeypatch.setitem(config.DEFAULTS, 'github_accounts', {'AMD-DID': 'work-login'})
    monkeypatch.setattr(github, 'settings', lambda: {'github_accounts': {'AMD-DID': 'work-login'}})


def test_branch_link_and_explicit_account_without_global_switch(routed, monkeypatch):
    monkeypatch.setenv('GH_TOKEN', 'wrong-personal-token')
    monkeypatch.setenv('GITHUB_TOKEN', 'another-wrong-token')
    monkeypatch.setattr(github.shutil, 'which', lambda command: '/bin/gh')
    commands = []

    def command(args, **kwargs):
        commands.append(args)
        assert args == ['/bin/gh', 'auth', 'token', '--hostname', 'github.com', '--user', 'work-login']
        assert 'GH_TOKEN' not in kwargs['env'] and 'GITHUB_TOKEN' not in kwargs['env']
        return SimpleNamespace(returncode=0, stdout='work-token\n')

    monkeypatch.setattr(github.subprocess, 'run', command)
    requests = []

    def get(url, **kwargs):
        requests.append((url, kwargs.get('params')))
        assert kwargs['headers']['Authorization'] == 'Bearer work-token'
        assert kwargs['follow_redirects'] is False
        if '/commits/' in url:
            assert url.endswith('feature%2FAVA-13322%2FDimension-Suggestion')
            data = {'sha': SHA}
        else:
            assert url.endswith('/readme') and kwargs['params'] == {'ref': SHA}
            data = {'type': 'file', 'encoding': 'base64', 'path': 'README.md',
                    'content': base64.b64encode(b'# Query engine\nVerified text').decode()}
        return httpx.Response(200, json=data)

    monkeypatch.setattr(github.httpx, 'get', get)
    result = github.read_repository(LINK)
    assert result['ref'] == BRANCH and result['account'] == 'work-login'
    assert result['commit'] == SHA and SHA in result['source']
    assert result['text'] == '# Query engine\nVerified text'
    assert 'work-token' not in json.dumps(result)
    assert len(commands) == 1 and len(requests) == 2


def test_discovery_uses_routed_owner_and_returns_candidates(routed, monkeypatch):
    monkeypatch.setattr(github, '_token', lambda login: 'selected-token')
    calls = []

    def get(endpoint, login, token, params=None):
        calls.append((endpoint, login, params))
        return {'total_count': 1, 'items': [{'full_name': 'AMD-DID/ava-mcp-server-http',
                'html_url': LINK.split('/commits/')[0], 'default_branch': 'main'}]}

    monkeypatch.setattr(github, '_get', get)
    found = github.search_repositories('ava-mcp-server-http')
    assert calls == [('search/repositories', 'work-login',
                      {'q': 'ava-mcp-server-http in:name user:AMD-DID', 'per_page': 20})]
    assert found['results'][0]['repositories'][0]['repository'] == 'AMD-DID/ava-mcp-server-http'
    with pytest.raises(ValueError, match='name from source'):
        github.search_repositories('meeting body with confidential details')


def test_missing_route_and_access_denial_never_fall_back(routed, monkeypatch):
    monkeypatch.setattr(github, '_token', lambda login: 'work-secret')
    with pytest.raises(ValueError, match='owner Other'):
        github.read_repository('Other/private-repo')
    calls = []

    def denied(url, **kwargs):
        calls.append(url)
        return httpx.Response(404, json={'message': 'Not Found'})

    monkeypatch.setattr(github.httpx, 'get', denied)
    with pytest.raises(ValueError, match='404 for account work-login') as error:
        github.read_repository(LINK)
    assert len(calls) == 1 and 'work-secret' not in str(error.value)


@pytest.mark.parametrize('value', ['https://evil.test/AMD-DID/repo',
    'https://user:secret@github.com/AMD-DID/repo', 'https://github.com/AMD-DID/repo?token=secret',
    'https://github.com/AMD-DID/repo/blob/main/README.md', '../repo'])
def test_invalid_repository_targets(value):
    with pytest.raises(ValueError):
        github.repository_target(value)


def test_account_routes_validate():
    for routes in ([], {'AMD-DID': 'token with spaces'}, {'AMD-DID': 'work', 'amd-did': 'other'}):
        with pytest.raises(ValueError):
            github.validate_accounts(routes)


def test_blocked_public_github_fetch_points_to_authenticated_route(monkeypatch):
    from cos import research
    monkeypatch.setattr(research, 'settings', lambda: {'research_hosts': []})
    with pytest.raises(ValueError, match='repository_read'):
        research.validate_url(LINK, dns=False)


def test_read_saved_evidence_and_source_reader_at_boundary(store, monkeypatch):
    parent = store.create_job('Find README from meeting context', 'Technology')
    for _ in range(3):
        parent = store.create_job('Research README', 'Technology', parent_id=parent['id'])
    engine = Engine(store, Scripted(perform('delegate', {'tasks': [
        {'org': 'Engineering', 'source_only': True, 'request': 'Read the README at '+LINK}]})))
    assert run(engine, parent)['status'] == 'waiting_children'
    child = store.rows('SELECT * FROM jobs WHERE parent_id=?', (parent['id'],))[0]
    assert allowed_job_action(child, 'repository_read')
    assert not allowed_job_action(child, 'run_command')
    text = '# README\n' + 'query engine evidence\n'*500
    monkeypatch.setattr(github, 'read_repository', lambda *args: {
        'repository': 'AMD-DID/ava-mcp-server-http', 'account': 'work-login', 'ref': BRANCH,
        'commit': SHA, 'path': 'README.md', 'source': LINK, 'sha256': 'b'*64, 'text': text})
    evidence = Tools(store).execute(child, 'repository_read', {'repository': LINK})
    assert evidence['next_offset'] == 4000 and evidence['truncated']
    assert Path(evidence['path']).read_bytes() == text.encode()
    page = Tools(store).execute(child, 'read_file', {'path': evidence['path'], 'offset': 4000})
    assert page['text'] == text[4000:8000]
    engine.provider = Scripted(perform('finish', {'summary': 'Verified README', 'file_ids': [evidence['file_id']]}))
    assert run(engine, child)['status'] == 'completed'
    engine.release_parents()
    assert store.job(parent['id'])['status'] == 'queued'
    assert len(store.rows('SELECT * FROM files WHERE job_id=?', (parent['id'],))) == 1


def test_new_source_routing_does_not_resume_demo_job(store):
    job = store.create_job('MCP query engine README research', 'Technology')
    store.update(job['id'], status='waiting_input', question='Please supply README or URL.')
    before = store.job(job['id'])
    engine = Engine(store, Scripted())
    engine.recover_research_approvals()
    assert store.job(job['id']) == before
