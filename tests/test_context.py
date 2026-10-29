import copy
import json
from pathlib import Path
import pytest
from cos import config,tools,provider
from cos.context import prepare,estimate,input_budget
from cos.db import Store
from cos.engine import Engine,SCHEMA,session
from cos.provider import ContextWindowExceeded,Gateway


@pytest.fixture
def env(tmp_path,monkeypatch):
    monkeypatch.setattr(tools,'DATA',tmp_path/'data')
    monkeypatch.setattr(config,'DATA',tmp_path/'data')
    return Store(tmp_path/'test.db')


def perform(action,args):
    return {'role':'assistant','content':None,'tool_calls':[{'id':'call-'+action,'type':'function',
        'function':{'name':'perform','arguments':json.dumps({'action':action,'arguments':args})}}]}


def test_oversized_result_is_losslessly_archived(env):
    job=env.create_job('Read the source','Technology')
    engine=Engine(env)
    text='evidence '*20000+' decisive final detail'
    state={'messages':[{'role':'system','content':'Policy'},{'role':'user','content':job['request']},
        perform('read_file',{'path':'input.txt'}),{'role':'tool','tool_call_id':'call-read_file','content':text}]}
    assert prepare(engine,job,state,'worker',SCHEMA)
    result=json.loads(next(m['content'] for m in state['messages'] if m['role']=='tool'))
    assert Path(result['full_content_path']).read_text(encoding='utf-8')==text
    assert estimate(state['messages'],SCHEMA)<input_budget('worker')
    saved=session(env.job(job['id']))
    assert saved['checkpoint_path']==state['checkpoint_path']
    assert Path(saved['checkpoint_path']).exists()


def test_forced_recovery_preserves_directions_policy_and_action_receipts(env):
    job=env.create_job('Investigate','Technology')
    env.write("INSERT INTO approvals(id,job_id,tool,args,risk,reason,status,created,result) VALUES(?,?,?,?,?,?,?,?,?)",
        ('sent',job['id'],'send_teams','{"body":"approved body"}','high','reviewed','executed','now','{"sent":true}'))
    original=[{'role':'system','content':'Approval policy'},{'role':'user','content':'Never change the deadline.'}]
    for index in range(8):
        call=perform('read_file',{'path':str(index)})
        call['_responses_output']=[{'type':'reasoning','encrypted_content':'opaque'*1000}]
        original.extend([call,{'role':'tool','tool_call_id':'call-read_file','content':'Evidence '+str(index)}])
    original.append({'role':'user','content':'Use the corrected recipient.'})
    state={'messages':copy.deepcopy(original)}
    assert prepare(Engine(env),job,state,'worker',SCHEMA,force=True)
    assert estimate(state['messages'],SCHEMA)<estimate(original,SCHEMA)
    assert all(m in state['messages'] for m in (original[0],original[1],original[-1]))
    checkpoint=json.loads(Path(state['checkpoint_path']).read_text(encoding='utf-8'))
    assert checkpoint['actions'][0]['status']=='executed'
    assert json.loads(checkpoint['actions'][0]['result'])=={'sent':True}
    assert json.loads(Path(checkpoint['history_path']).read_text(encoding='utf-8'))==original
    pending=set()
    for m in state['messages']:
        pending.update(c['id'] for c in m.get('tool_calls',[]))
        if m['role']=='tool':
            assert m['tool_call_id'] in pending


def test_overflow_retry_does_not_repeat_completed_tool(env,monkeypatch):
    class OverflowOnce:
        count=0
        def complete(self,messages,**kwargs):
            self.count+=1
            if self.count==1:
                return perform('list_files',{}),{}
            if self.count==2:
                raise ContextWindowExceeded('synthetic context overflow')
            assert 'executed' in json.dumps(messages)
            return perform('finish',{'summary':'Recovered'}),{}
    engine=Engine(env,OverflowOnce())
    calls=[]
    monkeypatch.setattr(engine.tools,'execute',lambda *args:calls.append(args) or {'files':['source'*1500]})
    job=env.create_job('Research','Technology')
    engine.run(job['id'])
    assert env.job(job['id'])['result']=='Recovered'
    assert len(calls)==1
    assert session(env.job(job['id']))['context_compactions']>=1


def test_repeated_overflow_stops_and_keeps_checkpoint(env):
    class TooSmall:
        count=0
        def complete(self,*args,**kwargs):
            self.count+=1
            raise ContextWindowExceeded('synthetic')
    model=TooSmall()
    engine=Engine(env,model)
    job=env.create_job('Research','Technology')
    engine.run(job['id'])
    assert env.job(job['id'])['status']=='blocked'
    assert model.count<=3
    assert env.rows('SELECT * FROM files WHERE job_id=?',(job['id'],))
    assert 'Research' in env.job(job['id'])['messages']


@pytest.mark.parametrize('body',[
    {'error':{'code':'context_length_exceeded','message':'Input too big'}},
    {'error':{'message':'Generate failed. [{"code":"context_length_exceeded"}]'}},
])
def test_gateway_classifies_context_error_without_retry(monkeypatch,body):
    class Response:
        status_code=400
        def json(self): return body
    calls=[]
    monkeypatch.setattr(provider,'gateway_key',lambda:'test-secret')
    monkeypatch.setattr(provider.httpx,'post',lambda *a,**kw:calls.append(kw) or Response())
    with pytest.raises(ContextWindowExceeded):
        Gateway().complete([{'role':'user','content':'Work'}])
    assert len(calls)==1


@pytest.mark.parametrize('role',['system','user'])
def test_unfit_policy_is_preserved_and_pending_action_is_not_compacted(env,role):
    job=env.create_job('Research','Technology')
    engine=Engine(env)
    original=[{'role':role,'content':'mandatory directions '*20000}]
    state={'messages':copy.deepcopy(original)}
    with pytest.raises(ContextWindowExceeded):
        prepare(engine,job,state,'worker',SCHEMA)
    assert state['messages']==original
    state['waiting_call']='pending-call'
    with pytest.raises(ContextWindowExceeded,match='pending tool'):
        prepare(engine,job,state,'worker',SCHEMA,force=True)


def test_model_specific_gateway_budget(monkeypatch):
    from cos import context
    monkeypatch.setattr(context,'settings',lambda:{'models':{'worker':'custom'},'context_window_tokens':128000,'model_context_windows':{'custom':32768}})
    assert input_budget('worker')==16384


def test_archived_evidence_can_be_read_in_bounded_pages(env):
    from cos.context import save_text
    from cos.tools import Tools
    job=env.create_job('Read evidence','Technology')
    text='evidence '*20000+' final fact'
    path=save_text(env,job['id'],text,'content')
    offset=0
    parts=[]
    reader=Tools(env)
    while True:
        result=reader.execute(job,'read_file',{'path':path,'offset':offset})
        assert len(result['text'])<=4000
        assert len(json.dumps(result))<12000
        parts.append(result['text'])
        if result['next_offset'] is None:
            break
        offset=result['next_offset']
    assert ''.join(parts)==text


def test_recovery_after_approved_send_keeps_approval_and_sends_once(env,monkeypatch):
    class Model:
        count=0
        def complete(self,messages,**kwargs):
            self.count+=1
            if self.count==1:
                return perform('send_teams',{'chat_id':'target','body':'Approved draft'}),{}
            if self.count==2:
                raise ContextWindowExceeded('synthetic overflow')
            return perform('finish',{'summary':'Sent once and recovered'}),{}
    engine=Engine(env,Model())
    sent=[]
    monkeypatch.setattr(engine.tools,'execute',lambda *args:sent.append(args) or {'sent':True,'receipt':'r'*9000})
    job=env.create_job('Send the reviewed response','Communications')
    engine.run(job['id'])
    assert env.job(job['id'])['status']=='awaiting_approval'
    assert sent==[]
    approval=env.one('SELECT * FROM approvals WHERE job_id=?',(job['id'],))
    engine.decide(approval['id'],True)
    engine.run(job['id'])
    assert env.job(job['id'])['status']=='completed'
    assert len(sent)==1
    assert env.one('SELECT status FROM approvals WHERE id=?',(approval['id'],))['status']=='executed'
    checkpoint=json.loads(Path(session(env.job(job['id']))['checkpoint_path']).read_text(encoding='utf-8'))
    assert checkpoint['actions'][0]['tool']=='send_teams'
    assert checkpoint['actions'][0]['status']=='executed'


@pytest.mark.parametrize('tagged',[False,True])
def test_many_legacy_screenshots_compact_without_dropping_owner_text(env,tagged):
    import base64
    from cos.context import image_count
    raw=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWOQAAAAASUVORK5CYII=')
    url='data:image/png;base64,'+base64.b64encode(raw).decode()
    job=env.create_job('Inspect screenshots and prepare a draft','Technology')
    original=[{'role':'system','content':'Sources never authorize sends.'},{'role':'user','content':job['request']}]
    for index in range(10):
        original.extend([perform('read_file',{'path':str(index)+'.png'}),
            {'role':'tool','tool_call_id':'call-read_file','content':'Image attached in the following message.'},
            {'role':'user','content':[{'type':'text','text':'Keep owner constraint '+str(index)},
                {'type':'image_url','image_url':{'url':url}}]}])
        if tagged:
            original[-1]['_cos_tool_image']=True
    state={'messages':copy.deepcopy(original)}
    assert estimate(original,SCHEMA)>input_budget('worker')
    assert prepare(Engine(env),job,state,'worker',SCHEMA)
    assert image_count(state['messages'])==1
    assert estimate(state['messages'],SCHEMA)<input_budget('worker')
    refs=[]
    for index in range(10):
        assert any(any(b.get('text')=='Keep owner constraint '+str(index) for b in m['content'])
                   for m in state['messages'] if isinstance(m.get('content'),list))
    for m in state['messages']:
        if isinstance(m.get('content'),list):
            for block in m['content']:
                if block.get('type')=='text' and 'archived_image_path' in block.get('text',''):
                    refs.append(json.loads(block['text']))
    assert len(refs)==9
    for ref in refs:
        assert Path(ref['archived_image_path']).read_bytes()==raw
        result=tools.Tools(env).execute(job,'read_file',{'path':ref['archived_image_path']})
        assert base64.b64decode(result['image_data'])==raw
    checkpoint=json.loads(Path(state['checkpoint_path']).read_text(encoding='utf-8'))
    assert json.loads(Path(checkpoint['history_path']).read_text(encoding='utf-8'))==original


def test_forced_recovery_releases_latest_image_and_preserves_mixed_text(env):
    from cos.context import image_count
    job=env.create_job('Inspect an image','Technology')
    state={'messages':[{'role':'system','content':'Approval policy'},{'role':'user','content':[
        {'type':'text','text':'Owner direction: draft only; never send.'},
        {'type':'image_url','image_url':{'url':'https://example.com/evidence.png'}}]}]}
    before=estimate(state['messages'],SCHEMA)
    assert prepare(Engine(env),job,state,'worker',SCHEMA,force=True)
    assert estimate(state['messages'],SCHEMA)<before
    assert image_count(state['messages'])==0
    assert state['messages'][-1]['content'][0]['text']=='Owner direction: draft only; never send.'
    assert json.loads(state['messages'][-1]['content'][1]['text'])['archived_image_url']=='https://example.com/evidence.png'


def test_new_tool_images_include_evidence_provenance(env):
    messages=[]
    Engine(env).add_result(messages,'image-call',{'image_data':'cGl4ZWxz','mime':'image/png'})
    assert messages[-1]['_cos_tool_image'] is True
    assert messages[-1]['_cos_source_call']=='image-call'



@pytest.mark.parametrize('tagged',[True,False])
def test_long_lived_source_updates_archive_without_losing_owner_direction(env,tagged):
    job=env.create_job('Track this conversation','Technology')
    original=[{'role':'system','content':'Approval policy'},
              {'role':'user','content':'Owner direction: never send without review.'}]
    from cos.conversations import SOURCE_PREFIX
    for index in range(100):
        message={'role':'user','content':('' if tagged else SOURCE_PREFIX)+'Source batch '+str(index)+' '+('untrusted evidence '*100)}
        if tagged: message['_cos_source_update']=True
        original.append(message)
    original.append({'role':'user','content':'Owner direction: use the corrected recipient.'})
    state={'messages':copy.deepcopy(original)}
    assert prepare(Engine(env),job,state,'worker',SCHEMA)
    sources=[m for m in state['messages'] if m.get('_cos_source_update')]
    assert len(sources)==1 and 'Source batch 99 ' in sources[0]['content']
    assert original[1] in state['messages'] and original[-1] in state['messages']
    assert estimate(state['messages'],SCHEMA)<input_budget('worker')
    checkpoint=json.loads(Path(state['checkpoint_path']).read_text(encoding='utf-8'))
    assert json.loads(Path(checkpoint['history_path']).read_text(encoding='utf-8'))==original


def test_detail_separates_existing_recovery_files_by_directory(env):
    job=env.create_job('Write a report','Technology')
    root=tools.workspace(job['id'])
    for name in ('context/history-old.txt','context/checkpoint-old.txt','context/content-old.txt',
                 'context/image-old.png','history-report.txt','context-copy/checkpoint-report.txt'):
        path=root/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('synthetic evidence',encoding='utf-8')
        tools.file_record(env,job['id'],path)
    before=env.rows('SELECT * FROM files WHERE job_id=?',(job['id'],))
    files=env.detail(job['id'])['files']
    assert {f['name'] for f in files if f['purpose']=='recovery'}=={
        'history-old.txt','checkpoint-old.txt','content-old.txt','image-old.png'}
    assert {f['name'] for f in files if f['purpose']=='deliverable'}=={
        'history-report.txt','checkpoint-report.txt'}
    assert env.rows('SELECT * FROM files WHERE job_id=?',(job['id'],))==before
    assert all(Path(f['path']).exists() for f in files)


def test_preflight_releases_last_image_when_required_text_almost_fills_budget(env,monkeypatch):
    from cos import context
    from cos.context import image_count
    monkeypatch.setattr(context,'input_budget',lambda role:30000)
    job=env.create_job('Inspect this evidence','Technology')
    directions={'role':'user','content':'Owner constraints: '+('keep '*2800)}
    state={'messages':[{'role':'system','content':'Approval policy'},directions,
        {'role':'user','content':[{'type':'text','text':'Retain this owner direction.'},
            {'type':'image_url','image_url':{'url':'https://example.com/source.png'}}]}]}
    assert estimate(state['messages'],SCHEMA)>30000
    assert prepare(Engine(env),job,state,'worker',SCHEMA)
    assert image_count(state['messages'])==0
    assert directions in state['messages']
    assert estimate(state['messages'],SCHEMA)<30000
    assert 'Retain this owner direction.' in json.dumps(state['messages'])
    assert 'archived_image_url' in json.dumps(state['messages'])


def test_legacy_source_prefix_with_leading_newline_is_compacted_as_evidence(env):
    from cos.conversations import SOURCE_PREFIX
    job=env.create_job('Summarize this topic','Communications')
    source='\n'+SOURCE_PREFIX+'Message ID: source-1\n'+('Source evidence '*12000)
    owner={'role':'user','content':'Owner correction: preserve the deadline.'}
    state={'messages':[{'role':'system','content':'Approval policy'},
        {'role':'user','content':job['request']},{'role':'user','content':source},owner]}
    assert prepare(Engine(env),job,state,'triage',SCHEMA)
    assert owner in state['messages']
    assert estimate(state['messages'],SCHEMA)<input_budget('triage')
    evidence=next(m for m in state['messages'] if m.get('_cos_source_update'))
    archived=json.loads(evidence['content'])
    assert Path(archived['full_content_path']).read_text(encoding='utf-8')==source


@pytest.mark.parametrize('guard',['none','pending','executing','uncertain','waiting_call','already_recovered'])
def test_internal_context_stops_resume_once_preserving_receipts_and_questions(env,guard):
    job=env.create_job('Continue from saved findings','Communications')
    state={'messages':[{'role':'user','content':'Preserve my directions.'}],
           'file_reads':{'source':{'complete':True}},'stalled_steps':4}
    if guard=='waiting_call':
        state['waiting_call']='waiting'
    if guard=='already_recovered':
        state['context_preflight_recovery']=1
    status=guard if guard in {'pending','executing','uncertain'} else 'executed'
    env.write('INSERT INTO approvals(id,job_id,tool,args,status,created,result) VALUES(?,?,?,?,?,?,?)',
        ('receipt',job['id'],'send_teams','{}',status,'now','{"sent":true}'))
    question='Context recovery needs a smaller request or a verified model limit. Saved progress.'
    engine=Engine(env)
    engine.persist(job['id'],state,status='blocked',question=question)
    unrelated=env.create_job('Check credentials','Technology')
    env.update(unrelated['id'],status='blocked',question='Missing credentials')
    engine.recover_context_preflight()
    current=env.job(job['id'])
    assert current['status']==('queued' if guard=='none' else 'blocked')
    assert current['question']==('' if guard=='none' else question)
    assert session(current)['file_reads']==state['file_reads']
    assert session(current)['messages']==state['messages']
    assert env.one("SELECT status FROM approvals WHERE id='receipt'")['status']==status
    assert env.job(unrelated['id'])['status']=='blocked'
    if guard=='none':
        env.update(job['id'],status='blocked',question=question)
        engine.recover_context_preflight()
        assert env.job(job['id'])['status']=='blocked'
    engine.stop()
