from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from cos import schedules, server, config
from cos.db import Store
from cos.schedules import Schedules, next_occurrence


def instant(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


@pytest.fixture
def scheduler(tmp_path, monkeypatch):
    monkeypatch.setattr(schedules, 'utcnow', lambda: instant('2026-01-01T00:00'))
    return Schedules(Store(tmp_path/'schedules.db'))


def spec(**changes):
    return dict(name='Weekly planning',request='Prepare a planning brief',project='',
                cadence='once',start_local='2026-01-02T09:00',timezone='America/Chicago',**changes)


def test_once_restart_and_deduplication(scheduler):
    row=scheduler.save(spec())
    assert scheduler.tick(instant('2026-01-02T14:59')) == []
    restarted=Schedules(Store(scheduler.store.path))
    jobs=restarted.tick(instant('2026-01-02T15:00'))
    assert len(jobs)==1
    assert restarted.tick(instant('2026-01-03T15:00'))==[]
    detail=restarted.list()[0]
    assert detail['status']=='completed' and detail['next_run'] is None
    assert detail['runs'][0]['job_id']==jobs[0]
    job=scheduler.store.job(jobs[0])
    assert job['status']=='queued' and job['request']==row['request']
    assert not scheduler.store.rows('SELECT * FROM approvals')


def test_missed_recurring_runs_coalesce_and_do_not_overlap(scheduler):
    body=spec();body['cadence']='daily'
    row=scheduler.save(body)
    first=scheduler.tick(instant('2026-02-01T17:00'))
    assert len(first)==1
    assert scheduler.list()[0]['next_run']=='2026-02-02T15:00:00+00:00'
    assert not scheduler.tick(instant('2026-02-03T17:00'))
    scheduler.store.update(first[0],status='awaiting_approval')
    assert not scheduler.tick(instant('2026-02-03T17:00'))
    scheduler.store.update(first[0],status='completed')
    assert len(scheduler.tick(instant('2026-02-03T17:00')))==1
    assert len(scheduler.list()[0]['runs'])==2


def test_pause_edit_resume_delete_and_stale_version(scheduler):
    row=scheduler.save(spec())
    scheduler.change(row['id'],{'action':'pause','version':row['version']})
    assert not scheduler.tick(instant('2026-01-03T15:00'))
    with pytest.raises(ValueError,match='changed'):
        scheduler.change(row['id'],{'action':'resume','version':row['version']})
    paused=scheduler.list()[0]
    edited=scheduler.save({**spec(),'name':'Edited','version':paused['version']},row['id'])
    assert edited['status']=='paused'
    scheduler.change(row['id'],{'action':'resume','version':edited['version']})
    jobs=scheduler.tick(instant('2026-01-03T15:00'))
    current=scheduler.list()[0]
    scheduler.change(row['id'],{'action':'delete','version':current['version']})
    assert not scheduler.list()
    assert scheduler.store.job(jobs[0])['status']=='queued'
    assert len(scheduler.store.rows('SELECT * FROM schedule_runs'))==1


@pytest.mark.parametrize('field,value', [('cadence','hourly'),('timezone','Invalid/Zone'),
    ('request',''),('project','Unregistered'),('start_local','2025-01-01T00:00'),
    ('start_local','2026-01-02T09:00+00:00'),('name',[]),('start_local','bad')])
def test_validation(scheduler,field,value):
    body=spec();body[field]=value
    with pytest.raises(ValueError):
        scheduler.save(body)


@pytest.mark.parametrize('cadence,start,after,expected',[
    ('daily','2026-03-07T09:00','2026-03-07T15:00','2026-03-08T14:00'),
    ('weekly','2026-03-06T09:00','2026-03-06T15:00','2026-03-13T14:00'),
    ('daily','2026-03-07T02:30','2026-03-07T09:00','2026-03-08T08:00'),
    ('daily','2026-10-31T01:30','2026-11-01T06:30','2026-11-02T07:30'),
])
def test_dst_and_weekly(cadence,start,after,expected):
    row={**spec(),'cadence':cadence,'start_local':start}
    assert next_occurrence(row,instant(after))==instant(expected)


def test_concurrent_stores_claim_once(scheduler):
    scheduler.save(spec())
    second=Schedules(Store(scheduler.store.path))
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(lambda s:s.tick(instant('2026-01-03T15:00')),[scheduler,second]))
    assert sum(map(len,results))==1


def test_tick_rolls_back_job_if_receipt_fails(scheduler):
    scheduler.save(spec())
    scheduler.store.write("CREATE TRIGGER reject_run BEFORE INSERT ON schedule_runs BEGIN SELECT RAISE(ABORT,'receipt failed'); END")
    with pytest.raises(Exception,match='receipt failed'):
        scheduler.tick(instant('2026-01-03T15:00'))
    assert not scheduler.store.rows('SELECT * FROM jobs')
    assert not scheduler.store.rows('SELECT * FROM events')
    assert scheduler.list()[0]['status']=='active'


def test_api_auth_and_management(scheduler,monkeypatch):
    monkeypatch.setattr(server,'store',scheduler.store)
    client=TestClient(server.app)
    assert client.get('/api/schedules').status_code==401
    client.get('/')
    assert client.post('/api/schedules',json=spec()).status_code==403
    headers={'x-cos-token':server.TOKEN}
    row=client.post('/api/schedules',json=spec(),headers=headers).json()
    assert row['status']=='active'
    assert len(client.get('/api/schedules').json())==1
    assert client.post('/api/schedules/'+row['id'],json={**spec(),'name':'New name','version':row['version']},headers=headers).status_code==200
    assert client.post('/api/schedules/'+row['id'],json={'action':'delete','version':row['version']},headers=headers).status_code==400
    assert client.post('/api/schedules',json={'request':'missing'},headers=headers).status_code==400


def test_engine_respects_disconnect(scheduler,monkeypatch):
    from cos.engine import Engine
    scheduler.save(spec())
    engine=Engine(scheduler.store)
    class Ticks:
        count=0
        def wait(self,seconds):
            self.count+=1
            return self.count>1
    called=[]
    monkeypatch.setattr(engine.schedules,'tick',lambda:called.append(True))
    monkeypatch.setitem(config.DEFAULTS,'queue_paused',True)
    engine.stop_event=Ticks()
    engine.loop()
    assert not called
    monkeypatch.setitem(config.DEFAULTS,'queue_paused',False)
    engine.stop_event=Ticks()
    engine.loop()
    assert called==[True]
    engine.pool.shutdown(wait=True)
