import copy
import hashlib
import json

import httpx
import pytest

from cos import config, github, repositories, stash

URL = 'https://stash.amd.com/projects/ATE/repos/test-repo'
SERVERS = {'amd':{'url':'https://stash.amd.com','projects':{'ATE':'work-login'}}}
SHA = 'a'*40


@pytest.fixture
def routed(monkeypatch):
    monkeypatch.setattr(stash,'settings',lambda:{'stash_servers':copy.deepcopy(SERVERS)})
    monkeypatch.setattr(stash,'token_for',lambda server,login:'private-stash-token')


@pytest.mark.parametrize('value', [URL, URL+'/browse?at=refs%2Fheads%2Ffeature%2FATE-12',
    'https://stash.amd.com/scm/ate/test-repo.git',
    'ssh://git@stash.amd.com:7999/ATE/test-repo.git',
    'git@stash.amd.com:ATE/test-repo.git'])
def test_browser_clone_and_ssh_routing(routed,value):
    repo = stash.Repository(value)
    assert repo.slug=='test-repo' and repo.login=='work-login'
    assert repo.clone_url.lower()=='https://stash.amd.com/scm/ate/test-repo.git'
    if '?at=' in value:
        assert repo.ref=='refs/heads/feature/ATE-12'


@pytest.mark.parametrize('value', ['https://evil.test/projects/ATE/repos/test-repo',
    'https://stash.amd.com.evil.test/projects/ATE/repos/test-repo',
    'https://user:secret@stash.amd.com/projects/ATE/repos/test-repo',
    URL+'?token=secret',URL+'/browse?at=main&at=other',URL+'/browse/src/file.py',
    'http://stash.amd.com/projects/ATE/repos/test-repo',
    URL.replace('/ATE/','/OTHER/'),URL.replace('test-repo','%2e%2e')])
def test_invalid_targets_never_request_credentials(routed,monkeypatch,value):
    monkeypatch.setattr(stash,'token_for',lambda *args:pytest.fail('Credential requested before validation'))
    with pytest.raises(ValueError):
        stash.Repository(value)


def test_url_branch_conflict_and_context_path(routed,monkeypatch):
    with pytest.raises(ValueError,match='conflicts'):
        stash.Repository(URL+'/browse?at=feature%2Fone','feature/two')
    monkeypatch.setattr(stash,'settings',lambda:{'stash_servers':{
        'other':{'url':'https://stash.example:8443/bitbucket','projects':{'ATE':'other'}}}})
    repo = stash.Repository('https://stash.example:8443/bitbucket/scm/ATE/test-repo.git')
    assert repo.url=='https://stash.example:8443/bitbucket/projects/ATE/repos/test-repo'
    with pytest.raises(ValueError):
        stash.Repository('https://stash.example:8443/scm/ATE/test-repo.git')


def test_pinned_read_and_tree_preserve_branch_and_server_pagination(routed,monkeypatch):
    calls = []
    def request(method,url,**kwargs):
        assert method=='GET' and kwargs['follow_redirects'] is False
        assert kwargs['headers']['Authorization']=='Bearer private-stash-token'
        calls.append((url,kwargs['params']))
        if url.endswith('/commits'):
            assert kwargs['params']['until'] in {'refs/heads/feature/ATE-12',SHA}
            data = {'values':[{'id':SHA}]}
        elif url.endswith('/files'):
            assert kwargs['params']['at']==SHA and kwargs['params']['start']==0
            data = {'values':['src/engine.py'],'isLastPage':False,'nextPageStart':17}
        else:
            assert '/raw/src/engine.py' in url and kwargs['params']=={'at':SHA}
            return httpx.Response(200,content=b'# Source\n')
        return httpx.Response(200,json=data)
    monkeypatch.setattr(stash.httpx,'request',request)
    result = repositories.read_repository(URL+'/browse?at=refs%2Fheads%2Ffeature%2FATE-12','src/engine.py')
    assert result['commit']==SHA and result['sha256']==hashlib.sha256(b'# Source\n').hexdigest()
    assert result['account']=='work-login' and 'private-stash-token' not in json.dumps(result)
    tree = repositories.repository_tree(URL,ref=SHA,path='src',limit=1)
    assert tree['next_offset']==17 and tree['entries']==[{'path':'src/engine.py','type':'blob'}]
    assert len(calls)==4


def test_readme_and_search_are_scoped_and_paginated(routed,monkeypatch):
    def request(method,url,**kwargs):
        if url.endswith('/branches/default'):
            data = {'id':'refs/heads/main','latestCommit':SHA}
        elif url.endswith('/browse'):
            data = {'children':{'isLastPage':True,'values':[
                {'type':'FILE','path':{'toString':'README.md'}}]}}
        elif url.endswith('/raw/README.md'):
            return httpx.Response(200,content=b'# README')
        else:
            assert url.endswith('/projects/ATE/repos')
            assert kwargs['params']=={'start':7,'limit':20}
            data = {'values':[{'slug':'test-repo'}],'isLastPage':True}
        return httpx.Response(200,json=data)
    monkeypatch.setattr(stash.httpx,'request',request)
    assert repositories.read_repository(URL)['text']=='# README'
    result = repositories.search_repositories('test-repo',server='amd',offset=7)
    assert result['results'][0]['repositories'][0]['repository']==URL
    assert result['results'][0]['next_offset'] is None


@pytest.mark.parametrize('code',[302,401,403,404,500])
def test_http_errors_do_not_leak_secrets_or_follow_redirects(routed,monkeypatch,code):
    calls = []
    def request(*args,**kwargs):
        calls.append(args)
        assert kwargs['follow_redirects'] is False
        return httpx.Response(code,text='private-stash-token',headers={'Location':'https://evil.test'})
    monkeypatch.setattr(stash.httpx,'request',request)
    with pytest.raises(ValueError) as error:
        repositories.read_repository(URL,'README.md')
    assert 'private-stash-token' not in str(error.value) and len(calls)==1


@pytest.mark.parametrize('path',['../secret','src/../secret','/secret','src\\secret','src//secret'])
def test_traversal_is_rejected(routed,path):
    with pytest.raises(ValueError):
        stash.validate_path(path)


def test_connection_config_and_os_credentials_never_store_token_in_settings(tmp_path,monkeypatch):
    import keyring
    monkeypatch.setattr(config,'ROOT',tmp_path)
    saved = config.save_settings({'stash_servers':SERVERS})
    assert saved['stash_servers']==SERVERS
    monkeypatch.setattr(stash,'settings',config.settings)
    credentials = {}
    monkeypatch.setattr(keyring,'get_keyring',lambda:object())
    monkeypatch.setattr(keyring,'set_password',lambda service,identity,value:credentials.update({(service,identity):value}))
    monkeypatch.setattr(keyring,'get_password',lambda service,identity:credentials.get((service,identity)))
    stash.store_token('amd','ATE','secret-stash-pat')
    server,login = stash.route('amd','ATE')
    assert stash.token_for(server,login)=='secret-stash-pat'
    assert 'secret-stash-pat' not in (tmp_path/'config.local.json').read_text()
    assert 'secret-stash-pat' not in json.dumps(repositories.connection_status())
    with pytest.raises(ValueError,match='No fallback'):
        stash.token_for(server,'another-account')
    monkeypatch.setattr(keyring,'get_keyring',lambda:type('PlaintextKeyring',(),{})())
    with pytest.raises(ValueError,match='secure'):
        stash.store_token('amd','ATE','another-secret')


@pytest.mark.parametrize('change',[{'url':'http://stash.amd.com'}, {'url':'https://user:secret@stash.amd.com'},
    {'url':'https://stash.amd.com/?token=secret'}, {'url':'https://stash.amd.com/'},
    {'url':'https://stash.amd.com/../evil'}, {'token':'secret'}, {'projects':{'ATE':'one','ate':'two'}}])
def test_invalid_server_config_rejected(change):
    server = {**SERVERS['amd'],**change}
    with pytest.raises(ValueError):
        stash.validate_servers({'amd':server})


def test_mixed_discovery_and_github_backward_compatibility(routed,monkeypatch):
    monkeypatch.setattr(github,'routes',lambda:{'work-org':'work'})
    monkeypatch.setattr(github,'search_repositories',lambda *args:{'results':[{'provider':'github'}]})
    monkeypatch.setattr(stash,'search_repositories',lambda *args,**kwargs:{'results':[{'provider':'stash'}]})
    assert repositories.search_repositories('repo')['results']==[{'provider':'github'},{'provider':'stash'}]
    assert repositories.search_repositories('repo',server='github')['results']==[{'provider':'github'}]


def test_token_endpoint_requires_local_auth_and_csrf(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from cos import server
    calls = []
    monkeypatch.setattr(stash,'store_token',lambda *args:calls.append(args))
    client = TestClient(server.app)
    payload = {'server':'amd','project':'ATE','token':'test-secret'}
    assert client.post('/api/stash/token',json=payload).status_code==401
    client.get('/')
    assert client.post('/api/stash/token',json=payload).status_code==403
    response = client.post('/api/stash/token',json=payload,headers={'x-cos-token':server.TOKEN})
    assert response.status_code==200 and response.json()=={'ok':True}
    assert calls==[('amd','ATE','test-secret')]


@pytest.mark.parametrize('historical',[True,False])
def test_merge_receipt_checks_changed_and_deleted_files_at_immutable_commit(routed,monkeypatch,historical):
    head = 'b'*40
    target = 'c'*40
    repo = {'slug':'test-repo','project':{'key':'ATE'}}
    pr = {'state':'MERGED','closedDate':1234,
          'fromRef':{'id':'refs/heads/feature/ATE-12','latestCommit':head,'repository':repo},
          'toRef':{'id':'refs/heads/main','repository':repo}}
    if historical:
        pr['properties']={'mergeCommit':{'id':head}}
    calls = []
    def api(method,url,**kwargs):
        calls.append((url,kwargs['params']))
        if url.endswith('/branches/default'):
            return httpx.Response(200,json={'id':'refs/heads/main','latestCommit':target})
        if '/raw/' in url:
            assert kwargs['params']=={'at':head if historical else target}
            if url.endswith('/removed.txt'):
                return httpx.Response(404)
            return httpx.Response(200,content=b'new content')
        return httpx.Response(200,json=pr)
    monkeypatch.setattr(stash.httpx,'request',api)
    files = [{'path':'updated.txt','after_sha256':hashlib.sha256(b'new content').hexdigest()},
             {'path':'removed.txt','after_sha256':None}]
    receipt = stash.verify_merge(URL,12,'feature/ATE-12',head,files)
    assert receipt['verified_target_commit']==(head if historical else target)
    assert receipt['merge_commit']==(head if historical else None)
    assert len(calls)==4
    pr['fromRef']['latestCommit']='f'*40
    with pytest.raises(ValueError,match='exact tested'):
        stash.verify_merge(URL,12,'feature/ATE-12',head,files)


def test_empty_filtered_pages_retain_server_cursor(routed,monkeypatch):
    def api(method,url,**kwargs):
        if url.endswith('/commits'):
            data = {'values':[{'id':SHA}]}
        elif url.endswith('/files'):
            data = {'isLastPage':False,'nextPageStart':9,'values':['docs/README.md']}
        else:
            data = {'isLastPage':False,'nextPageStart':9,'values':[{'slug':'unrelated'}]}
        return httpx.Response(200,json=data)
    monkeypatch.setattr(stash.httpx,'request',api)
    tree = stash.repository_tree(URL,ref=SHA,path='src')
    assert tree['entries']==[] and tree['next_offset']==9
    found = stash.search_repositories('test-repo',server='amd')
    assert found['results'][0]['repositories']==[] and found['results'][0]['next_offset']==9


def test_stash_git_credentials_use_the_routed_account_and_canonical_transport(routed):
    url,header = repositories.git_credentials('ssh://git@stash.amd.com:7999/ATE/test-repo.git')
    assert url=='https://stash.amd.com/scm/ATE/test-repo.git'
    assert header=='Bearer private-stash-token'


def test_unavailable_github_does_not_hide_stash_discovery(routed,monkeypatch):
    monkeypatch.setattr(github,'routes',lambda:{'org':'work'})
    def unavailable(*args):
        raise ValueError('GitHub sign-in missing')
    monkeypatch.setattr(github,'search_repositories',unavailable)
    monkeypatch.setattr(stash.httpx,'request',lambda *args,**kwargs:
        httpx.Response(200,json={'isLastPage':True,'values':[{'slug':'test-repo'}]}))
    found = repositories.search_repositories('test-repo')
    assert found['results'][0]['repositories'][0]['repository']==URL
    assert found['errors']==[{'provider':'github','error':'GitHub sign-in missing'}]


def test_failed_stash_project_does_not_hide_another_configured_project(routed,monkeypatch):
    configured = copy.deepcopy(SERVERS)
    configured['amd']['projects']['OTHER']='other-login'
    monkeypatch.setattr(stash,'settings',lambda:{'stash_servers':configured})
    def api(method,url,**kwargs):
        if '/projects/ATE/' in url:
            return httpx.Response(403)
        return httpx.Response(200,json={'isLastPage':True,'values':[{'slug':'test-repo'}]})
    monkeypatch.setattr(stash.httpx,'request',api)
    found = stash.search_repositories('test-repo')
    assert found['results'][0]['owner']=='OTHER'
    assert found['errors'][0]['owner']=='ATE'
    assert 'private-stash-token' not in json.dumps(found)
