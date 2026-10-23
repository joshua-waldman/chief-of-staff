import json
from email.utils import formatdate
import httpx
import pytest
from cos import capacity, config, provider
from cos.capacity import Capacity, CapacityDelayed, retry_delay
from cos.db import Store
from cos.engine import Engine, session


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(provider,'gateway_key',lambda:'synthetic-secret')
    monkeypatch.setattr(capacity.time,'time',lambda:1000)
    return Store(tmp_path/'capacity.db')


def test_retry_headers(env):
    assert retry_delay({'retry-after':'30'})==30
    assert retry_delay({'retry-after-ms':'1500'})==1.5
    assert retry_delay({'retry-after':formatdate(1040,usegmt=True)})==40
    for value in ('bad','nan','inf','-3'):
        assert retry_delay({'retry-after':value}) is None


def test_gateway_429_defers_without_sleep_and_survives_restart(env,monkeypatch):
    calls=[]
    def post(*args,**kwargs):
        calls.append(kwargs)
        return httpx.Response(429,headers={'retry-after':'45'},json={'error':{'message':'synthetic-secret token rate limit'}})
    monkeypatch.setattr(provider.httpx,'post',post)
    monkeypatch.setattr(provider.time,'sleep',lambda _:pytest.fail('No sleeping on 429'))
    with pytest.raises(CapacityDelayed) as error:
        provider.Gateway(env).complete([{'role':'user','content':'test'}])
    assert error.value.state['retry_at']==1045
    assert 'synthetic-secret' not in error.value.state['detail']
    with pytest.raises(CapacityDelayed):
        provider.Gateway(Store(env.path)).complete([])
    assert len(calls)==1
    # Different models have independent capacity.
    Capacity(env).check(config.settings()['models']['chief'])


def test_backoff_and_bounded_recovery(env,monkeypatch):
    monkeypatch.setattr(capacity.random,'uniform',lambda *args:1)
    c=Capacity(env)
    assert c.limited('test',{},'detail',60).state['retry_at']==1005
    assert c.limited('test',{},'detail',60).state['retry_at']==1010
    monkeypatch.setattr(capacity.time,'time',lambda:1060)
    assert c.limited('test',{},'detail',60).state['paused']
    c.reset('test')
    c.check('test')


def test_saved_tool_receipts_resume_without_replay(env,monkeypatch):
    model=config.settings()['models']['director']
    receipt={'role':'tool','tool_call_id':'already-sent','content':'message delivered receipt'}
    job=env.create_job('Continue work','Technology')
    env.update(job['id'],messages=json.dumps({'messages':[{'role':'user','content':'Continue'},receipt]}),step=5)
    class Fake:
        calls=0
        def complete(self,messages,**kwargs):
            self.calls+=1
            assert receipt in messages
            if self.calls==1:
                raise Capacity(env).limited(model,{'retry-after':'30'},'rate limit',900)
            return {'role':'assistant','content':None,'tool_calls':[{'id':'done','function':{'name':'perform','arguments':json.dumps({'action':'finish','arguments':{'summary':'Done'}})}}]},{}
    fake=Fake()
    engine=Engine(env,fake)
    monkeypatch.setattr(engine.tools,'execute',lambda *args:pytest.fail('Completed tool must not replay'))
    try:
        engine.run(job['id'])
        saved=env.job(job['id'])
        assert saved['status']=='waiting_capacity'
        assert saved['question']=='' and saved['step']==5
        assert receipt in session(saved)['messages']
        assert not engine.active
        assert engine.ready_jobs(5)==[]
        restarted=Engine(Store(env.path),fake)
        try:
            monkeypatch.setattr(capacity.time,'time',lambda:1031)
            assert restarted.ready_jobs(5)[0]['id']==job['id']
            restarted.run(job['id'])
            assert env.job(job['id'])['status']=='completed'
            assert fake.calls==2
        finally:
            restarted.stop()
    finally:
        engine.stop()


def test_shared_pause_cancel_and_manual_retry(env):
    engine=Engine(env)
    model=config.settings()['models']['director']
    c=Capacity(env)
    c.limited(model,{},'rate limit',0)
    first=env.create_job('One','Technology')
    second=env.create_job('Two','Technology')
    try:
        assert engine.ready_jobs(5)==[]
        assert env.job(first['id'])['status']=='capacity_paused'
        engine.cancel(second['id'])
        engine.retry_capacity(first['id'])
        assert engine.ready_jobs(5)[0]['id']==first['id']
        assert env.job(second['id'])['status']=='cancelled'
    finally:
        engine.stop()


def test_concurrent_model_calls_defer_instead_of_waiting(env,monkeypatch):
    gateway=provider.Gateway(env)
    def post(*args,**kwargs):
        with pytest.raises(CapacityDelayed) as error:
            provider.Gateway(env).complete([])
        assert error.value.state['busy']
        return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'OK'}}]})
    monkeypatch.setattr(provider.httpx,'post',post)
    gateway.complete([])


def test_long_retry_after_pauses_at_recovery_deadline(env,monkeypatch):
    model=config.settings()['models']['director']
    c=Capacity(env)
    c.limited(model,{'retry-after':'3600'},'rate limit',30)
    engine=Engine(env)
    job=env.create_job('Wait','Technology')
    try:
        engine.ready_jobs(5)
        monkeypatch.setattr(capacity.time,'time',lambda:1031)
        assert engine.ready_jobs(5)==[]
        assert env.job(job['id'])['status']=='capacity_paused'
    finally:
        engine.stop()


def test_legacy_rate_failures_recover_but_uncertain_actions_do_not(env):
    engine=Engine(env)
    safe=env.create_job('Safe','Technology')
    unsafe=env.create_job('Uncertain','Technology')
    for job in (safe,unsafe):
        env.update(job['id'],status='failed',question='AMD gateway returned HTTP 429: token rate limit')
    env.write("INSERT INTO approvals(id,job_id,status) VALUES(?,?,'uncertain')",('uncertain',unsafe['id']))
    try:
        engine.recover_rate_failures()
        assert env.job(safe['id'])['status']=='queued'
        assert env.job(unsafe['id'])['status']=='failed'
    finally:
        engine.stop()


def test_inflight_success_does_not_clear_newer_cooldown(env):
    c=Capacity(env)
    observed=c.get('model')
    c.limited('model',{'retry-after':'30'},'throttled',900)
    c.succeeded('model',observed=observed)
    with pytest.raises(CapacityDelayed):
        c.check('model')


def test_retry_endpoint_is_authenticated_and_preserves_receipts(env,monkeypatch):
    from fastapi.testclient import TestClient
    from cos import server
    engine=Engine(env)
    monkeypatch.setattr(server,'store',env)
    monkeypatch.setattr(server,'engine',engine)
    job=env.create_job('Continue','Technology')
    model=config.settings()['models']['director']
    delayed=Capacity(env).limited(model,{'retry-after':'30'},'throttled',900)
    env.update(job['id'],status='running',messages=json.dumps({'messages':[{'role':'tool','content':'receipt','tool_call_id':'done'}]}))
    engine.defer_capacity(job['id'],delayed)
    client=TestClient(server.app)
    try:
        path='/api/jobs/'+job['id']+'/retry-capacity'
        assert client.post(path,json={}).status_code==401
        client.get('/')
        assert client.post(path,json={}).status_code==403
        detail=client.get('/api/jobs/'+job['id']).json()
        assert detail['capacity']['retry_at']==1030
        assert client.post(path,json={},headers={'x-cos-token':server.TOKEN}).status_code==200
        assert env.job(job['id'])['status']=='queued'
        assert session(env.job(job['id']))['messages'][0]['content']=='receipt'
        assert client.post(path,json={},headers={'x-cos-token':server.TOKEN}).status_code==400
    finally:
        engine.stop()
