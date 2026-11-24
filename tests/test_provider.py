import json
import pytest
from cos.provider import Gateway
from cos.engine import SCHEMA
from cos import provider

class FakeResponse:
    status_code=200
    def __init__(self,data):self.data=data
    def json(self):return self.data


@pytest.mark.parametrize('model',['gpt-6-sol','other-model'])
def test_reopened_legacy_finish_gets_receipt_before_recovery_directions(monkeypatch,model):
    import copy
    requests=[]
    monkeypatch.setattr(provider,'gateway_key',lambda:'test-secret')
    monkeypatch.setattr(provider,'settings',lambda:{**provider.DEFAULTS,'models':{'worker':model}})
    def post(url,**kwargs):
        requests.append(kwargs['json'])
        return FakeResponse({'status':'completed','output':[],
                             'choices':[{'message':{'role':'assistant','content':'OK'}}]})
    monkeypatch.setattr(provider.httpx,'post',post)
    arguments=json.dumps({'action':'finish','arguments':{'summary':'Prior report'}})
    history=[{'role':'assistant','content':None,'tool_calls':[{
        'id':'terminal','type':'function','function':{'name':'perform','arguments':arguments}}],
        '_responses_model':model,'_responses_output':[{'type':'function_call',
            'call_id':'terminal','name':'perform','arguments':arguments}]},
        {'role':'system','content':'Publication remains unfinished.'},
        {'role':'user','content':'Resume using saved results.'}]
    original=copy.deepcopy(history)
    Gateway().complete(history,tools=SCHEMA)
    items=requests[0].get('input',requests[0].get('messages'))
    receipt=items[1]
    assert receipt.get('call_id',receipt.get('tool_call_id'))=='terminal'
    result=json.loads(receipt.get('output',receipt.get('content')))
    assert result['prior_turn_ended'] is True and 'completed' not in result
    assert items[2]['role']=='system' and items[3]['role']=='user'
    assert history==original


def test_legacy_finish_repair_is_idempotent_and_never_invents_action_outcomes():
    def call(action,id):
        return {'role':'assistant','tool_calls':[{'id':id,'function':{
            'name':'perform','arguments':json.dumps({'action':action})}}]}
    history=[call('send_email','send'),{'role':'user','content':'Continue'},
             call('finish','done'),{'role':'tool','tool_call_id':'done','content':'{"completed":false}'},
             call('finish','legacy'),{'role':'system','content':'Reopened'}]
    balanced=provider.balance_legacy_finish(history)
    assert provider.balance_legacy_finish(balanced)==balanced
    assert [m['tool_call_id'] for m in balanced if m['role']=='tool']==['done','legacy']
    assert next(m for m in balanced if m.get('tool_call_id')=='done')['content']=='{"completed":false}'


@pytest.mark.parametrize('output',[[],[{'type':'function_call','call_id':'partial','name':'perform','arguments':'{"action":"write_file"'}]])
def test_incomplete_responses_are_marked_for_inference_recovery(monkeypatch,output):
    monkeypatch.setattr(provider,'gateway_key',lambda:'test-secret')
    monkeypatch.setattr(provider.httpx,'post',lambda *a,**kw:FakeResponse({'status':'incomplete','output':output}))
    answer,meta=Gateway().complete([{'role':'user','content':'Build artifact'}],tools=SCHEMA)
    assert meta['output_incomplete'] is True

def test_reasoning_tools_use_responses_and_replay_items(monkeypatch):
    requests=[]
    outputs=[
        {'status':'completed','output':[{'type':'reasoning','id':'rs_1','summary':[],'encrypted_content':'opaque'},{'type':'function_call','id':'fc_1','call_id':'call_1','name':'perform','arguments':json.dumps({'action':'list_files','arguments':'{}'})}]},
        {'status':'completed','output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':'Done'}]}]}
    ]
    def post(url,**kwargs):
        requests.append((url,kwargs))
        return FakeResponse(outputs.pop(0))
    monkeypatch.setattr(provider,'gateway_key',lambda:'test-secret')
    monkeypatch.setattr(provider.httpx,'post',post)
    answer,_=Gateway().complete([{'role':'user','content':'Inspect files'}],tools=SCHEMA)
    assert requests[0][0].endswith('/responses')
    assert requests[0][1]['json']['store'] is False
    history=[{'role':'user','content':'Inspect files'},answer,{'role':'tool','tool_call_id':'call_1','content':'[]'}]
    Gateway().complete(history,tools=SCHEMA)
    items=requests[1][1]['json']['input']
    assert any(x.get('encrypted_content')=='opaque' for x in items)
    assert any(x.get('type')=='function_call_output' and x['call_id']=='call_1' for x in items)

def test_gateway_credentials_only_sent_to_verified_endpoint(monkeypatch):
    monkeypatch.setattr(provider,'gateway_key',lambda:'never-print-me')
    monkeypatch.setattr(provider,'settings',lambda:{'gateway_url':'https://outside.example','models':{'worker':'gpt-6-sol'}})
    monkeypatch.setattr(provider.httpx,'post',lambda *a,**kw:pytest.fail('Must not send to outside endpoint'))
    with pytest.raises(provider.NotConfigured):
        Gateway().complete([{'role':'user','content':'AMD work'}])

def test_model_role_selects_small_triage_model(monkeypatch):
    calls=[]
    monkeypatch.setattr(provider,'gateway_key',lambda:'test-secret')
    def post(url,**kwargs):
        calls.append(kwargs['json'])
        return FakeResponse({'choices':[{'message':{'role':'assistant','content':'OK'}}]})
    monkeypatch.setattr(provider.httpx,'post',post)
    Gateway().complete([{'role':'user','content':'Triage'}],role='triage')
    assert calls[0]['model']=='gpt-6-luna'

@pytest.mark.parametrize('origin',['other-model',None])
def test_reasoning_is_not_replayed_across_models(monkeypatch,origin):
    seen=[]
    monkeypatch.setattr(provider,'gateway_key',lambda:'test-secret')
    def post(url,**kwargs):
        seen.extend(kwargs['json']['input'])
        return FakeResponse({'status':'completed','output':[]})
    monkeypatch.setattr(provider.httpx,'post',post)
    prior={'role':'assistant','content':None,'_responses_model':origin,'_responses_output':[{'type':'reasoning','encrypted_content':'must-not-cross-models'}],'tool_calls':[{'id':'call-old','type':'function','function':{'name':'perform','arguments':'{}'}}]}
    Gateway().complete([prior,{'role':'tool','tool_call_id':'call-old','content':'[]'}],tools=SCHEMA)
    assert not any(x.get('type')=='reasoning' for x in seen)
    assert any(x.get('type')=='function_call' for x in seen)


def test_intake_limits_timeout_tokens_and_disables_retries(monkeypatch):
    calls=[]
    monkeypatch.setattr(provider,'gateway_key',lambda:'test-secret')
    def post(url,**kwargs):
        calls.append(kwargs)
        raise provider.httpx.TimeoutException('synthetic timeout')
    monkeypatch.setattr(provider.httpx,'post',post)
    monkeypatch.setattr(provider.time,'sleep',lambda seconds:pytest.fail('Intake must not retry'))
    with pytest.raises(RuntimeError,match='Cannot reach'):
        Gateway().complete([{'role':'user','content':'Classify'}],role='triage',timeout=8,attempts=1,max_tokens=2000)
    assert len(calls)==1
    assert calls[0]['timeout']==8
    assert calls[0]['json']['max_completion_tokens']==2000
    assert 'tools' not in calls[0]['json']
