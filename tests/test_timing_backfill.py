import json
import pytest
from cos.db import Store
from cos.timing import TimingAssessor, validate


class AssessorProvider:
    def __init__(self, change=None, bad=False):
        self.change=change
        self.bad=bad
        self.calls=0

    def complete(self,messages,**kwargs):
        self.calls+=1
        assert kwargs['role']=='triage'
        assert not kwargs.get('tools')
        items=json.loads(messages[-1]['content'])['items']
        assert 'no explicit timeline' in messages[0]['content']
        if self.change:
            self.change(items)
        results=[dict(id=i['id'],time_sensitive='yes',timing_reason='Assessment: team is blocked on this decision.',deadline_at='') for i in items]
        if self.bad:
            results[0]['id']='wrong-id'
        return {'content':json.dumps({'assessments':results})}, {}


def attention_job(store):
    job=store.create_job('The team cannot proceed without my decision.', 'Technology')
    store.update(job['id'],status='waiting_input',question='Which option?')
    return job['id']


def test_existing_attention_gets_assessed_without_resuming_or_changing_approval(tmp_path):
    store=Store(tmp_path/'timing.db')
    identity=attention_job(store)
    store.write('INSERT INTO job_briefs VALUES(?,?,?)',(identity,json.dumps({'context':'Preserved summary'}),'before'))
    store.write('INSERT INTO approvals(id,job_id,tool,args,status) VALUES(?,?,?,?,?)',('approval',identity,'send_email','{}','pending'))
    before=store.job(identity)
    approval=store.one('SELECT * FROM approvals')
    provider=AssessorProvider()
    assessor=TimingAssessor(store,provider)
    assert assessor.assess_pending()==1
    assert store.job(identity)==before
    assert store.one('SELECT * FROM approvals')==approval
    brief=store.detail(identity)['brief']
    assert brief['time_sensitive']=='yes'
    assert brief['deadline_at']==''
    assert brief['context']=='Preserved summary'
    assert assessor.assess_pending()==0
    assert provider.calls==1


@pytest.mark.parametrize('change',['status','brief','source','approval'])
def test_changed_evidence_discards_stale_model_result(tmp_path,change):
    store=Store(tmp_path/'stale.db')
    identity=attention_job(store)
    def mutate(items):
        if change=='status':
            store.update(identity,status='completed')
        elif change=='brief':
            store.write('INSERT INTO job_briefs VALUES(?,?,?)',(identity,json.dumps({'context':'New information'}),'after'))
        elif change=='source':
            store.write('INSERT INTO communication_sources VALUES(?,?,?,?,?,?,?,?)',(identity,'email','one','sender@example.com','Update','today','Already resolved',1))
        else:
            store.write('INSERT INTO approvals(id,job_id,tool,args,status) VALUES(?,?,?,?,?)',('approval',identity,'send_email','{}','pending'))
    assessor=TimingAssessor(store,AssessorProvider(change=mutate))
    assert assessor.assess_pending()==0
    assert not store.detail(identity)['brief'].get('time_sensitive')


def test_invalid_model_output_and_gateway_error_remain_unassessed(tmp_path):
    store=Store(tmp_path/'invalid.db')
    identity=attention_job(store)
    with pytest.raises(ValueError):
        TimingAssessor(store,AssessorProvider(bad=True)).assess_pending()
    assert not store.detail(identity)['brief'].get('time_sensitive')
    class Broken:
        def complete(self,*args,**kwargs):
            raise RuntimeError('Gateway unavailable')
    with pytest.raises(RuntimeError):
        TimingAssessor(store,Broken()).assess_pending()
    assert not store.detail(identity)['brief'].get('time_sensitive')


def test_stopped_assessor_does_not_write_late_result(tmp_path):
    store=Store(tmp_path/'stopped.db')
    identity=attention_job(store)
    assessor=TimingAssessor(store,AssessorProvider())
    assessor.provider.change=lambda items:assessor.stop_event.set()
    assert assessor.assess_pending()==0
    assert not store.detail(identity)['brief'].get('time_sensitive')


@pytest.mark.parametrize('value',['yes','no','unknown'])
def test_all_decisions_require_explanations(value):
    with pytest.raises(ValueError):
        validate({'assessments':[dict(id='one',time_sensitive=value,timing_reason='',deadline_at='')]},{'one'})
