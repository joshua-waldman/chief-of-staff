"""Offline authentication and credential isolation checks."""
import base64
import json
import time
from types import SimpleNamespace

import pytest

from cos import teams_auth as auth
from cos.communication_bridge import perform

APP = {'client_id': '11111111-1111-1111-1111-111111111111',
       'tenant_id': '22222222-2222-2222-2222-222222222222'}


def jwt(scopes, exp=None):
    body = base64.urlsafe_b64encode(json.dumps({'scp': scopes, 'exp': exp or time.time()+600}).encode()).decode().rstrip('=')
    return 'header.'+body+'.signature'


@pytest.fixture(autouse=True)
def configuration(monkeypatch):
    monkeypatch.setattr(auth, 'settings', lambda: {'teams_auth': {}})


@pytest.mark.parametrize('value', [None, [], {'client_id': 'bad'},
    {**APP, 'secret': 'not-accepted'},
    {**APP, 'tenant_id': '../common'},
    {**APP, 'client_id': '1fec8e78-bce4-4aaf-ab1b-5451cc387264'}])
def test_invalid_registration_is_rejected(value):
    with pytest.raises(ValueError):
        auth.validate_config(value)


def test_empty_and_owned_app_registration_are_valid():
    auth.validate_config({})
    auth.validate_config(APP)


@pytest.mark.parametrize('scopes,expected', [('User.Read ChatMessage.Send', False), ('Chat.ReadWrite User.Read', True)])
def test_shared_scope_diagnostics_never_return_credentials(scopes, expected):
    credential = jwt(scopes)
    result = auth.status(SimpleNamespace(_access_token=credential))
    assert result['available'] is expected
    assert credential not in json.dumps(result)
    assert auth.status(SimpleNamespace(_access_token=jwt(scopes, time.time()-60)))['available'] is False


def test_missing_scope_rejected_before_any_graph_request():
    graph = SimpleNamespace(_access_token=jwt('User.Read'))
    with pytest.raises(ValueError, match='Chat.ReadWrite'):
        perform(graph, 'mark_teams_read', {'chat_id': 'chat'})


@pytest.mark.parametrize('result', [None, {'error': 'interaction_required'}])
def test_silent_auth_failure_requires_sign_in(monkeypatch, result):
    monkeypatch.setattr(auth, 'client', lambda: SimpleNamespace(get_accounts=lambda: [{}], acquire_token_silent=lambda *a, **k: result))
    with pytest.raises(ValueError, match='renewal or tenant consent'):
        auth.token()


def test_silent_auth_returns_token_without_interactive_fallback(monkeypatch):
    calls = []
    def acquire(scopes, account):
        calls.append((scopes, account))
        return {'access_token': 'private-access-token'}
    monkeypatch.setattr(auth, 'client', lambda: SimpleNamespace(get_accounts=lambda: [{'id': 'owner'}], acquire_token_silent=acquire))
    assert auth.token() == 'private-access-token'
    assert calls == [(auth.SCOPES, {'id': 'owner'})]


@pytest.mark.parametrize('same_account', [True, False])
def test_separate_auth_cannot_write_for_a_different_owner(monkeypatch, same_account):
    monkeypatch.setattr(auth, 'settings', lambda: {'teams_auth': APP})
    monkeypatch.setattr(auth, 'token', lambda: 'separate-token')
    writes = []
    class Graph:
        def get_token(self):
            return 'shared-token'
        def get(self, *a, **k):
            return {'id': 'owner' if self.get_token() == 'shared-token' or same_account else 'someone-else'}
        def post(self, path, body):
            writes.append((path, body, self.get_token()))
    graph = Graph()
    if same_account:
        assert perform(graph, 'mark_teams_read', {'chat_id': 'chat'})['executed']
        assert writes == [('/chats/chat/markChatReadForUser', {'user': {'id': 'owner'}}, 'separate-token')]
    else:
        with pytest.raises(ValueError, match='same account'):
            perform(graph, 'mark_teams_read', {'chat_id': 'chat'})
        assert not writes
    assert graph.get_token() == 'shared-token'


def test_configured_status_is_offline_and_uses_encrypted_cache(monkeypatch):
    monkeypatch.setattr(auth, 'settings', lambda: {'teams_auth': APP})
    monkeypatch.setattr(auth, 'cache', lambda: SimpleNamespace(find=lambda kind: [{'account': 'owner'}]))
    assert auth.status()['available']


def test_encryption_failure_does_not_fall_back_to_plaintext(monkeypatch):
    monkeypatch.setattr(auth, 'settings', lambda: {'teams_auth': APP})
    def unavailable():
        raise RuntimeError('encrypted storage unavailable')
    monkeypatch.setattr(auth, 'cache', unavailable)
    assert not auth.status()['available']
    with pytest.raises(RuntimeError, match='encrypted storage'):
        auth.client()



def test_refreshed_scope_takes_precedence_over_stale_loaded_token():
    graph = SimpleNamespace(_access_token=jwt('User.Read'), get_token=lambda: jwt('Chat.ReadWrite User.Read'))
    assert auth.read_client(graph) is graph


def test_successful_sign_in_requests_scopes_and_switches_account_without_printing_tokens(monkeypatch, capsys):
    removed = []
    calls = []
    app = SimpleNamespace(
        initiate_device_flow=lambda scopes: calls.append(scopes) or {'user_code': 'ABCD', 'message': 'Use Microsoft device login'},
        acquire_token_by_device_flow=lambda flow: {'access_token': 'private-token', 'id_token_claims': {'oid': 'owner'}},
        get_accounts=lambda: [{'local_account_id': 'old'}, {'local_account_id': 'owner'}],
        remove_account=lambda account: removed.append(account))
    monkeypatch.setattr(auth, 'client', lambda: app)
    auth.sign_in()
    output = capsys.readouterr().out
    assert 'private-token' not in output
    assert 'encrypted local storage' in output
    assert calls == [auth.SCOPES]
    assert removed == [{'local_account_id': 'old'}]


def test_preflight_auth_failure_is_definitively_not_executed(monkeypatch):
    monkeypatch.setattr(auth, 'settings', lambda: {'teams_auth': APP})
    def fail():
        raise RuntimeError('No OS encryption provider')
    monkeypatch.setattr(auth, 'token', fail)
    with pytest.raises(ValueError, match='before mark-as-read was attempted'):
        perform(object(), 'mark_teams_read', {'chat_id': 'chat'})


@pytest.mark.parametrize('teams', [False, True])
def test_server_authentication_launches_only_selected_connection(monkeypatch, teams):
    from cos import server
    launched = []
    class Process:
        stdout = ['Sign-in instructions\n']
        def wait(self):
            return 0
    def launch(command, **options):
        launched.append((command, options))
        return Process()
    monkeypatch.setattr(server.subprocess, 'Popen', launch)
    monkeypatch.setattr(server, 'auth', {'running': False, 'output': ''})
    monkeypatch.setattr(server, 'skill_dir', lambda name: __import__('pathlib').Path('installed-skill'))
    class ImmediateThread:
        def __init__(self, target, daemon):
            self.target = target
        def start(self):
            self.target()
    monkeypatch.setattr(server.threading, 'Thread', ImmediateThread)
    assert server.start_auth_flow(teams) == {'ok': True}
    command, options = launched[0]
    if teams:
        assert command[-2:] == ['-m', 'cos.teams_auth']
    else:
        assert command[-1].endswith('auth.py')
        assert 'cos.teams_auth' not in command
    assert options['cwd'] == server.ROOT
    assert not server.auth['running']
    assert server.auth['output'] == 'Sign-in instructions\n'
