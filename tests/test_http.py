import re
from fastapi.testclient import TestClient
from cos.server import app,TOKEN

def test_local_auth_and_csrf():
    client=TestClient(app)
    assert client.get('/api/status').status_code==401
    r=client.get('/')
    assert r.status_code==200
    assert 'Chief of Staff' in r.text
    assert client.get('/api/status').status_code==200
    assert client.post('/api/jobs',json={'request':'x'}).status_code==403
    assert client.post('/api/jobs',json={'request':'x'},headers={'x-cos-token':TOKEN,'origin':'https://evil.example'}).status_code==403
    assert client.get('/api/status',headers={'host':'evil.example'}).status_code==403

def test_security_headers_and_static():
    client=TestClient(app)
    r=client.get('/')
    assert r.headers['x-frame-options']=='DENY'
    assert 'frame-ancestors' in r.headers['content-security-policy']
    assert client.get('/static/app.js').status_code==200
    assert client.get('/static/style.css').status_code==200

def test_reject_empty_request_and_missing_job():
    client=TestClient(app)
    client.get('/')
    assert client.post('/api/jobs',json={'request':''},headers={'x-cos-token':TOKEN}).status_code==400
    assert client.get('/api/jobs/not-a-job').status_code==404


def test_communication_source_endpoint_loads_text_without_changing_job(tmp_path,monkeypatch):
    from cos import server,integrations
    from cos.db import Store
    store=Store(tmp_path/'source.db')
    monkeypatch.setattr(server,'store',store)
    job=store.create_job('Email ID: example-message','Communications')
    monkeypatch.setattr(integrations,'run_skill',lambda *args:{'body':'Full source\nFinal line','from':'sender@example.com'})
    client=TestClient(app)
    client.get('/')
    assert client.post('/api/jobs/'+job['id']+'/source',json={}).status_code==403
    response=client.post('/api/jobs/'+job['id']+'/source',json={},headers={'x-cos-token':TOKEN})
    assert response.json()=={'errors':[]}
    detail=client.get('/api/jobs/'+job['id']).json()
    assert detail['source_messages'][0]['body']=='Full source\nFinal line'
    assert detail['status']=='queued'
    assert not detail['approvals']


def test_queue_toggle_persists_and_status_counts_inflight_workers(tmp_path,monkeypatch):
    from cos import config,server
    monkeypatch.setattr(config,'ROOT',tmp_path)
    client=TestClient(app)
    client.cookies.set('cos_session',TOKEN)
    headers={'x-cos-token':TOKEN}
    assert client.post('/api/settings',json={'queue_paused':True}).status_code==403
    try:
        with server.engine.lock:
            server.engine.active.add('synthetic-inflight-worker')
        response=client.post('/api/settings',json={'queue_paused':True},headers=headers)
        assert response.status_code==200 and response.json()['queue_paused'] is True
        state=client.get('/api/status').json()
        assert state['settings']['queue_paused'] is True
        assert state['queue_active']>=1
        assert config.settings()['queue_paused'] is True
        assert client.post('/api/settings',json={'queue_paused':'yes'},headers=headers).status_code==400
        assert client.post('/api/settings',json={'queue_paused':False},headers=headers).json()['queue_paused'] is False
    finally:
        with server.engine.lock:
            server.engine.active.discard('synthetic-inflight-worker')
