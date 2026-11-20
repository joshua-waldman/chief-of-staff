import pytest
from fastapi.testclient import TestClient

from cos import server
from cos.db import Store


@pytest.mark.parametrize('configured', [False, True])
def test_connections_on_installation_without_local_setup_document(tmp_path, monkeypatch, configured):
    store = Store(tmp_path/'connections.db')
    monkeypatch.setattr(server, 'store', store)
    monkeypatch.setattr(server, 'ROOT', tmp_path)
    # Isolate unrelated optional integrations; exercise both requests made by navigation.
    monkeypatch.setattr(server, 'auth_status', lambda: {'authenticated': configured})
    monkeypatch.setattr(server, 'teams_read_status', lambda: {'configured': configured})
    monkeypatch.setattr(server, 'skill_inventory', lambda: [])
    monkeypatch.setattr(server, 'mcp_inventory', lambda: [])
    previous = {'ok': True, 'output': 'Existing verified connection', 'checked': '2026-01-01'}
    if configured:
        store.set_state('atlassian_check', previous)
    client = TestClient(server.app)
    client.cookies.set('cos_session', server.TOKEN)
    assert not (tmp_path/'docs/ATLASSIAN_SETUP.txt').exists()
    assert client.get('/api/connections').status_code == 200
    response = client.get('/api/atlassian')
    assert response.status_code == 200
    result = response.json()
    assert 'Open local credential setup' in result['setup_help']
    assert 'slai-atlassian' in result['setup_help']
    if configured:
        assert result['check'] == previous
        assert store.state('atlassian_check') == previous
    else:
        assert result['check']['ok'] is False
        assert 'Test Atlassian connection' in result['check']['output']
    assert not (tmp_path/'docs').exists()
