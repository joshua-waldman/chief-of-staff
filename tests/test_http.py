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
