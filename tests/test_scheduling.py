import copy
import json
from datetime import datetime,timedelta,timezone
import pytest
from cos import config,tools,conversations,progress
from cos.db import Store
from cos.engine import Engine,session
from cos.monitor import Monitor


def perform(action,args):
    return {'role':'assistant','content':None,'tool_calls':[{'id':'call-'+action,'type':'function',
        'function':{'name':'perform','arguments':json.dumps({'action':action,'arguments':args})}}]}


class Scripted:
    def __init__(self,*answers): self.answers=list(answers)
    def complete(self,*args,**kwargs): return self.answers.pop(0),{}


@pytest.fixture
def env(tmp_path,monkeypatch):
    monkeypatch.setattr(config,'ROOT',tmp_path)
    monkeypatch.setattr(config,'DATA',tmp_path/'data')
    monkeypatch.setattr(tools,'DATA',tmp_path/'data')
    monkeypatch.setitem(config.DEFAULTS,'max_steps',3)
    monkeypatch.setitem(config.DEFAULTS,'max_stalled_steps',4)
    return Store(tmp_path/'test.db')


@pytest.fixture
def clock(monkeypatch):
    import cos.monitor as monitor_module
    seconds=[0]
    def stamp(): return (datetime(2026,1,1,tzinfo=timezone.utc)+timedelta(seconds=seconds[0])).isoformat()
    monkeypatch.setattr(conversations,'now',stamp)
    monkeypatch.setattr(monitor_module,'now',stamp)
    return seconds,stamp


def activity(store,job,stamp):
    store.write("INSERT INTO conversation_activity VALUES(?,?,?) ON CONFLICT(job_id) DO UPDATE SET last_seen=excluded.last_seen",
        (job['id'],stamp,stamp))


def test_long_progress_continues_across_slices_without_owner_input(env,monkeypatch):
    answers=[perform('read_file',{'path':'evidence.txt','offset':i*4000}) for i in range(9)]
    answers.append(perform('finish',{'summary':'All pages processed'}))
    engine=Engine(env,Scripted(*answers))
    monkeypatch.setattr(engine.tools,'execute',lambda job,action,args:{'text':str(args['offset']),'next_offset':args['offset']+4000})
    job=env.create_job('Read all evidence','Technology')
    for index in range(3):
        engine.run(job['id'])
        current=env.job(job['id'])
        assert current['status']=='queued'
        assert current['question']==''
        assert current['step']==(index+1)*3
    engine.run(job['id'])
    current=env.job(job['id'])
    assert current['status']=='completed'
    assert session(current)['automatic_continuations']==3
    assert session(current)['total_model_steps']==10
    assert not env.rows("SELECT * FROM events WHERE kind='question'")
    engine.stop()


def test_old_step_count_does_not_consume_new_slice(env):
    engine=Engine(env,Scripted(perform('finish',{'summary':'Continued saved work'})))
    job=env.create_job('Continue','Technology')
    env.update(job['id'],step=60)
    engine.run(job['id'])
    assert env.job(job['id'])['status']=='completed'
    engine.stop()


def test_reply_at_slice_boundary_discards_stale_send_and_gets_fresh_budget(env,monkeypatch):
    monkeypatch.setitem(config.DEFAULTS,'max_steps',1)
    class Provider:
        count=0
        def complete(self,messages,**kwargs):
            self.count+=1
            if self.count==1:
                env.write('INSERT INTO communication_inbox(job_id,body,created) VALUES(?,?,?)',(job['id'],'Changed source data','now'))
                return perform('send_teams',{'chat_id':'chat','body':'Stale draft'}),{}
            assert 'Changed source data' in json.dumps(messages)
            return perform('finish',{'summary':'Reassessed the new reply'}),{}
    engine=Engine(env,Provider())
    monkeypatch.setattr(engine.tools,'execute',lambda *a:pytest.fail('Stale send executed'))
    job=env.create_job('Assess conversation','Technology')
    env.update(job['id'],step=60)
    engine.run(job['id'])
    assert env.job(job['id'])['status']=='queued'
    engine.run(job['id'])
    assert env.job(job['id'])['status']=='completed'
    assert env.job(job['id'])['step']==1
    assert session(env.job(job['id']))['total_model_steps']==2
    assert not env.rows('SELECT * FROM approvals')
    engine.stop()


def test_reply_burst_yields_worker_and_reassesses_once_after_quiet(env,clock,monkeypatch):
    seconds,stamp=clock
    monitor=Monitor(env)
    class Provider:
        count=0
        def complete(self,messages,**kwargs):
            self.count+=1
            if self.count==1:
                monitor.enqueue_conversation('teams','chat','source',[('reply-1',[],'First reply',True)])
                return perform('send_teams',{'chat_id':'chat','body':'Stale'}),{}
            assert 'First reply' in json.dumps(messages) and 'Second reply' in json.dumps(messages)
            return perform('finish',{'summary':'One reassessment for the burst'}),{}
    model=Provider()
    engine=Engine(env,model)
    monkeypatch.setattr(engine.tools,'execute',lambda *a:pytest.fail('Stale send executed'))
    job=env.create_job('Conversation analysis','Technology')
    env.write("INSERT INTO communication_threads VALUES('teams','chat',?)",(job['id'],))
    engine.run(job['id'])
    assert model.count==1 and env.job(job['id'])['status']=='queued'
    seconds[0]=10
    monitor.enqueue_conversation('teams','chat','source',[('reply-2',[],'Second reply',True)])
    seconds[0]=24
    engine.run(job['id'])
    assert model.count==1
    seconds[0]=25
    engine.run(job['id'])
    assert model.count==2
    assert env.job(job['id'])['status']=='completed'
    engine.stop()


def test_busy_chat_deadline_prevents_starvation_and_ready_jobs_skip_debounce(env,clock):
    seconds,stamp=clock
    chat=env.create_job('Busy chat','Communications')
    activity(env,chat,stamp())
    other=env.create_job('Independent work','Technology')
    engine=Engine(env)
    assert [j['id'] for j in engine.ready_jobs(1)]==[other['id']]
    for value in [10,20,30,40,50,59]:
        seconds[0]=value
        activity(env,chat,stamp())
        assert not conversations.ready(env,chat['id'])
    seconds[0]=60
    assert conversations.ready(env,chat['id'])
    engine.stop()


def test_consuming_batch_does_not_clear_new_pending_activity(env,clock):
    seconds,stamp=clock
    job=env.create_job('Chat','Technology')
    activity(env,job,stamp())
    env.write('INSERT INTO communication_inbox(job_id,body,created) VALUES(?,?,?)',(job['id'],'New reply',stamp()))
    conversations.consumed(env,job['id'])
    assert env.one('SELECT first_seen FROM conversation_activity')['first_seen']
    env.write('DELETE FROM communication_inbox')
    conversations.consumed(env,job['id'])
    assert env.one('SELECT first_seen FROM conversation_activity')['first_seen']==''


def test_new_activity_preserves_findings_retires_draft_and_resets_stall_guard(env):
    job=env.create_job('Chat','Communications')
    state={'messages':[{'role':'user','content':'Owner direction: draft only.'}],'stalled_steps':12,'progress_seen':['old']}
    env.update(job['id'],messages=json.dumps(state),status='completed')
    old={'context':'Verified prior findings','response_needed':'yes','action_needed':'no','proposed_body':'Old unsent draft','email_id':'source-id'}
    env.write('INSERT INTO job_briefs VALUES(?,?,?)',(job['id'],json.dumps(old),'now'))
    env.write('INSERT INTO communication_inbox(job_id,body,created) VALUES(?,?,?)',(job['id'],'New source reply','now'))
    assert conversations.deliver(env,job['id'])
    brief=json.loads(env.one('SELECT body FROM job_briefs')['body'])
    assert brief['context']==old['context'] and brief['email_id']=='source-id'
    assert brief['response_needed']=='unknown' and brief['proposed_body']==''
    state=session(env.job(job['id']))
    assert state['source_revision']==1 and not state.get('stalled_steps')
    assert state['messages'][0]['content']=='Owner direction: draft only.'
    assert state['messages'][-1]['_cos_source_update'] is True


def test_repeated_unchanged_reads_stop_across_slices_and_warn_first(env,monkeypatch):
    captured=[]
    class Provider:
        def complete(self,messages,**kwargs):
            captured.append(copy.deepcopy(messages))
            return perform('read_file',{'path':'same.txt'}),{}
    engine=Engine(env,Provider())
    monkeypatch.setattr(engine.tools,'execute',lambda *a:{'text':'Unchanged evidence'})
    job=env.create_job('Analyze','Technology')
    for _ in range(4):
        engine.run(job['id'])
        if env.job(job['id'])['status']=='blocked': break
    current=env.job(job['id'])
    assert current['status']=='blocked'
    assert 'unchanged results' in current['question'] and 'step budget' not in current['question']
    assert len(captured)==5
    assert any('Progress check:' in json.dumps(messages) for messages in captured)
    engine.stop()


def test_cosmetic_brief_changes_do_not_mask_repeated_evidence():
    state={}
    for index in range(6):
        progress.observe(state,'read_teams',{'chat_id':'chat'},{'messages':['unchanged'],'uses':index})
        progress.observe(state,'update_brief',{}, {'status_summary':str(index),'response_needed':'unknown','action_needed':'unknown'})
    assert state['stalled_steps']>=8


def test_start_recovers_only_legacy_budget_stops_without_replaying_approvals(env):
    old=env.create_job('Budget exhausted','Technology')
    actual=env.create_job('Missing a fact','Technology')
    uncertain=env.create_job('Outcome unresolved','Technology')
    env.update(old['id'],status='waiting_input',question=progress.LEGACY_STEP_QUESTION,step=60)
    env.update(actual['id'],status='waiting_input',question='Which deadline?')
    env.update(uncertain['id'],status='waiting_input',question=progress.LEGACY_STEP_QUESTION)
    env.write("INSERT INTO approvals(id,job_id,status) VALUES(?,?,?)",('uncertain',uncertain['id'],'uncertain'))
    engine=Engine(env,Scripted())
    engine.loop=lambda:None
    engine.start()
    engine.thread.join(timeout=2)
    engine.stop()
    assert env.job(old['id'])['status']=='queued' and env.job(old['id'])['question']==''
    assert env.job(actual['id'])['question']=='Which deadline?'
    assert env.job(uncertain['id'])['status']=='waiting_input'



def test_yield_does_not_resurrect_cancelled_job(env,monkeypatch):
    monkeypatch.setitem(config.DEFAULTS,'max_steps',1)
    engine=Engine(env,Scripted(perform('read_file',{'path':'evidence.txt'})))
    job=env.create_job('Read','Technology')
    def cancelled(*args):
        env.update(job['id'],status='cancelled')
        return {'text':'Evidence'}
    monkeypatch.setattr(engine.tools,'execute',cancelled)
    engine.run(job['id'])
    assert env.job(job['id'])['status']=='cancelled'
    engine.stop()


def test_handoff_preserves_pending_send_and_requires_approval(env,monkeypatch):
    monkeypatch.setitem(config.DEFAULTS,'max_steps',1)
    engine=Engine(env,Scripted(perform('read_file',{'path':'evidence.txt'}),
        perform('send_teams',{'chat_id':'target','body':'Reviewed draft'})))
    called=[]
    monkeypatch.setattr(engine.tools,'execute',lambda *args:called.append(args[1]) or {'text':'Evidence'})
    job=env.create_job('Prepare a response','Communications')
    engine.run(job['id'])
    assert env.job(job['id'])['status']=='queued'
    engine.run(job['id'])
    assert env.job(job['id'])['status']=='awaiting_approval'
    assert called==['read_file']
    assert env.one('SELECT status FROM approvals WHERE tool=?',('send_teams',))['status']=='pending'
    engine.stop()
