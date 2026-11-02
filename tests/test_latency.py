"""Keep owner interactions responsive without dropping stored history."""
import asyncio
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from cos import server
from cos.db import Store


def test_compact_responses_preserve_details_and_paginate_history(tmp_path, monkeypatch):
    store = Store(tmp_path / 'latency.db')
    monkeypatch.setattr(server, 'store', store)
    parent = store.create_job('Owner request', 'Engineering')
    child = store.create_job('Child request', 'Engineering', parent_id=parent['id'])
    store.update(parent['id'], result='Full result ' * 1000, question='Full question')
    store.update(child['id'], messages=json.dumps({'messages': [], 'history': 'large ' * 10000}))
    for number in range(65):
        store.event(parent['id'], 'note', str(number))
    store.event(parent['id'], 'model', 'Internal inference metadata')
    for status in ('executed', 'pending', 'executing', 'uncertain'):
        store.write('INSERT INTO approvals(id,job_id,tool,args,risk,reason,status,created,result) VALUES(?,?,?,?,?,?,?,?,?)',
                    (status,parent['id'],'send_email',json.dumps({'body':'Exact body'}),'high','Owner approval',status,'now','Receipt ' * 1000))
    client = TestClient(server.app)
    client.cookies.set('cos_session', server.TOKEN)

    listing = client.get('/api/jobs?compact=true').json()
    assert all('result' not in item and 'question' not in item for item in listing)
    assert client.get('/api/jobs').json()[1]['result'].startswith('Full result')
    detail = client.get('/api/jobs/' + parent['id'] + '?compact=true').json()
    assert detail['result'].startswith('Full result')
    assert detail['question'] == 'Full question'
    assert detail['event_count'] == 66  # Includes the original creation event.
    assert len(detail['events']) == 30
    assert all(event['kind'] != 'model' for event in detail['events'])
    assert 'messages' not in detail['children'][0]
    assert {approval['status'] for approval in detail['approvals']} == {'pending', 'executing', 'uncertain'}
    assert all(json.loads(approval['args'])['body'] == 'Exact body' for approval in detail['approvals'])
    assert len(client.get('/api/jobs/' + parent['id']).json()['approvals']) == 4
    assert store.job(child['id'])['messages'].count('large') == 10000

    events = detail['events']
    while True:
        page = client.get('/api/jobs/' + parent['id'] + '/events', params={'before': events[0]['id'], 'limit': 13}).json()
        events = page['events'] + events
        if not page['has_more']:
            break
    assert len(events) == len({event['id'] for event in events}) == 66
    assert [event['id'] for event in events] == sorted(event['id'] for event in events)
    assert len(client.get('/api/jobs/' + parent['id']).json()['events']) == 67
    assert client.get('/api/jobs/missing/events').status_code == 404
    assert TestClient(server.app).get('/api/jobs/' + parent['id'] + '/events').status_code == 401


def test_schedule_write_does_not_block_the_event_loop(monkeypatch):
    from cos.schedules import Schedules
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    loop_thread = threading.get_ident()

    def blocked_save(self, body):
        assert threading.get_ident() != loop_thread
        entered.set()
        try:
            assert release.wait(3), 'The event loop could not release the blocked write'
            return {'saved': True}
        finally:
            finished.set()

    async def body():
        return {}

    async def scenario():
        task = asyncio.create_task(server.schedule_create(SimpleNamespace(json=body)))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.sleep(0)
            assert not finished.is_set()
        finally:
            release.set()
            result = await task
        assert result == {'saved': True}

    monkeypatch.setattr(Schedules, 'save', blocked_save)
    asyncio.run(scenario())


def test_connections_close_and_transactions_still_commit_or_roll_back(tmp_path):
    store = Store(tmp_path / 'connections.db')
    with store.connect() as connection:
        connection.execute("INSERT INTO state VALUES('committed','true')")
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute('SELECT 1')
    assert store.state('committed') is True
    with pytest.raises(ValueError):
        with store.connect() as connection:
            connection.execute("INSERT INTO state VALUES('rolled-back','true')")
            raise ValueError('Abort transaction')
    assert store.state('rolled-back') is None


def test_compact_details_retain_model_capacity_diagnostics(tmp_path):
    store = Store(tmp_path / 'capacity.db')
    job = store.create_job('Waiting request', 'Engineering')
    store.update(job['id'], status='waiting_capacity', messages=json.dumps({
        'messages': [], 'capacity': {'model': 'example-model', 'busy': True}}))
    detail = store.detail(job['id'], compact=True)
    assert detail['capacity']['model'] == 'example-model'
    assert detail['capacity']['busy'] is True
    assert 'messages' not in detail
