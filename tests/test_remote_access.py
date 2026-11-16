import json
import socket

import pytest
from fastapi.testclient import TestClient
from cos import server
from scripts import connect_cos


def test_tunnel_plan_is_direct_loopback_and_ignores_extra_ssh_forwards():
    cmd = connect_cos.command('desktop.example.com', r'EXAMPLE\owner', 18765)
    assert cmd[cmd.index('-L') + 1] == '127.0.0.1:18765:127.0.0.1:8765'
    assert cmd[cmd.index('-F') + 1] == 'none'
    assert 'StrictHostKeyChecking=yes' in cmd
    assert 'ExitOnForwardFailure=yes' in cmd
    assert 'GatewayPorts=no' in cmd
    assert '-a' in cmd and '-x' in cmd
    assert cmd[-3:] == ['-l', r'EXAMPLE\owner', 'desktop.example.com']


@pytest.mark.parametrize('host,user', [('-oProxyCommand=bad', 'owner'),
    ('desktop bad', 'owner'), ('desktop', '-root'), ('desktop', 'owner\ncommand')])
def test_tunnel_rejects_options_and_command_text(host, user):
    with pytest.raises(ValueError):
        connect_cos.command(host, user)


def test_preview_neither_connects_nor_requires_ssh(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError('Preview must not open a connection or launch a process')
    monkeypatch.setattr(connect_cos.socket, 'socket', forbidden)
    monkeypatch.setattr(connect_cos.subprocess, 'call', forbidden)
    monkeypatch.setattr(connect_cos.shutil, 'which', forbidden)
    assert connect_cos.main(['--host', 'desktop.example.com', '--user', 'owner']) == 0
    assert json.loads(capsys.readouterr().out)['mode'] == 'preview'


def test_tunnel_conflicting_port_never_launches_ssh(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Must not launch SSH when port is in use')
    monkeypatch.setattr(connect_cos.subprocess, 'call', forbidden)
    monkeypatch.setattr(connect_cos.shutil, 'which', lambda _: 'ssh')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        with pytest.raises(SystemExit) as error:
            connect_cos.main(['--host', 'desktop.example.com', '--user', 'owner',
                             '--local-port', str(listener.getsockname()[1]), '--connect'])
        assert error.value.code == 1


@pytest.mark.parametrize('local_port', [8765, 18765])
def test_forwarded_browser_origin_preserves_session_and_csrf(local_port, monkeypatch, tmp_path):
    from cos.db import Store
    monkeypatch.setattr(server, 'store', Store(tmp_path / 'synthetic.db'))
    monkeypatch.setattr(server, 'ROOT', __import__('pathlib').Path(__file__).resolve().parents[1])
    base = f'http://127.0.0.1:{local_port}'
    client = TestClient(server.app, base_url=base)
    assert client.get('/api/status').status_code == 401
    assert client.get('/').status_code == 200
    assert client.get('/static/app.js').status_code == 200
    assert client.get('/api/status', headers={'origin': base}).status_code == 200
    # Invalid input tests the authorized write boundary without executing a job.
    assert client.post('/api/jobs', json={'request': ''}, headers={'origin': base}).status_code == 403
    assert client.post('/api/jobs', json={'request': ''},
        headers={'origin': base, 'x-cos-token': server.TOKEN}).status_code == 400
    other = 'http://127.0.0.1:27665'
    assert client.post('/api/jobs', json={'request': ''},
        headers={'origin': other, 'x-cos-token': server.TOKEN}).status_code == 403


@pytest.mark.parametrize('path', ['/', '/static/app.js', '/api/status', '/api/files/missing'])
def test_proxy_hostname_and_forwarded_header_cannot_bypass_local_boundary(path):
    client = TestClient(server.app, base_url='https://cos.example.com')
    assert client.get(path, headers={'x-forwarded-host': '127.0.0.1',
                                    'x-forwarded-for': '127.0.0.1'}).status_code == 403
