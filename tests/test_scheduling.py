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


@pytest.mark.parametrize('agents',[2,4,12])
def test_scheduler_reserves_communications_slot_even_with_no_messages(env,monkeypatch,agents):
    monkeypatch.setitem(config.DEFAULTS,'parallel_agents',agents)
    engine=Engine(env,Scripted())
    for _ in range(agents+2):
        env.create_job('Research','Technology')
    try:
        selected=engine.dispatch_jobs()
        assert len(selected)==agents-1
        engine.active.update(job['id'] for job in selected)
        assert engine.dispatch_jobs()==[]
        mail=env.create_job('Triage this incoming email','Communications')
        assert [job['id'] for job in engine.dispatch_jobs()]==[mail['id']]
    finally:
        engine.stop()


def test_scheduler_keeps_general_work_moving_with_a_message_backlog(env,monkeypatch):
    monkeypatch.setitem(config.DEFAULTS,'parallel_agents',4)
    engine=Engine(env,Scripted())
    for _ in range(30):
        env.create_job('Triage this incoming Teams message','Communications')
    for _ in range(5):
        env.create_job('Research','Technology')
    try:
        selected=engine.dispatch_jobs()
        assert len(selected)==4
        assert sum(job['org']=='Communications' for job in selected)==1
        engine.active.update(job['id'] for job in selected)
        assert engine.dispatch_jobs()==[]
        mail=next(job for job in selected if job['org']=='Communications')
        engine.active.remove(mail['id'])
        env.update(mail['id'],status='awaiting_approval')
        assert engine.dispatch_jobs()[0]['org']=='Communications'
        research=next(job for job in selected if job['org']=='Technology')
        engine.active.remove(research['id'])
        env.update(research['id'],status='completed')
        assert [job['org'] for job in engine.dispatch_jobs()]==['Communications','Technology']
    finally:
        engine.stop()


@pytest.mark.parametrize('general_count',[0,1,2,3])
def test_communications_uses_all_spare_slots(env,monkeypatch,general_count):
    monkeypatch.setitem(config.DEFAULTS,'parallel_agents',4)
    engine=Engine(env,Scripted())
    for _ in range(6):
        env.create_job('Triage this incoming email','Communications')
    for _ in range(general_count):
        env.create_job('Research','Technology')
    try:
        selected=engine.dispatch_jobs()
        assert len(selected)==4
        assert sum(job['org']=='Communications' for job in selected)==4-general_count
        assert sum(job['org']=='Technology' for job in selected)==general_count
        assert len({job['id'] for job in selected})==4
        engine.active.update(job['id'] for job in selected)
        assert engine.dispatch_jobs()==[]
    finally:
        engine.stop()


def test_borrowed_communications_slots_return_to_ready_general_work(env,monkeypatch):
    monkeypatch.setitem(config.DEFAULTS,'parallel_agents',4)
    engine=Engine(env,Scripted())
    for _ in range(6):
        env.create_job('Triage this incoming Teams message','Communications')
    try:
        selected=engine.dispatch_jobs()
        engine.active.update(job['id'] for job in selected)
        research=env.create_job('New research','Technology')
        assert engine.dispatch_jobs()==[]
        finished=selected[0]
        engine.active.remove(finished['id'])
        env.update(finished['id'],status='completed')
        assert [job['id'] for job in engine.dispatch_jobs()]==[research['id']]
        engine.active.add(research['id'])
        env.update(research['id'],status='completed')
        engine.active.remove(research['id'])
        assert engine.dispatch_jobs()[0]['org']=='Communications'
    finally:
        engine.stop()


def test_scheduler_skips_unsettled_messages_without_using_their_slot(env,monkeypatch,clock):
    monkeypatch.setitem(config.DEFAULTS,'parallel_agents',4)
    engine=Engine(env,Scripted())
    seconds,stamp=clock
    chat=env.create_job('Busy Teams chat','Communications')
    activity(env,chat,stamp())
    for _ in range(5):
        env.create_job('Research','Technology')
    try:
        selected=engine.dispatch_jobs()
        assert len(selected)==3
        assert all(job['org']=='Technology' for job in selected)
        engine.active.update(job['id'] for job in selected)
        seconds[0]=60
        assert [job['id'] for job in engine.dispatch_jobs()]==[chat['id']]
    finally:
        engine.stop()


def test_single_agent_scheduler_serves_both_queues(env,monkeypatch):
    monkeypatch.setitem(config.DEFAULTS,'parallel_agents',1)
    engine=Engine(env,Scripted())
    research=env.create_job('Research','Technology')
    mail=env.create_job('Triage this incoming email','Communications')
    env.write('UPDATE jobs SET updated=? WHERE id=?',('2026-01-01',research['id']))
    env.write('UPDATE jobs SET updated=? WHERE id=?',('2026-01-02',mail['id']))
    try:
        assert [job['id'] for job in engine.dispatch_jobs()]==[research['id']]
        env.update(research['id'],status='completed')
        assert [job['id'] for job in engine.dispatch_jobs()]==[mail['id']]
    finally:
        engine.stop()


def test_routing_communications_returns_to_its_reserved_slot(env,monkeypatch):
    engine=Engine(env,Scripted())
    existing=env.create_job('Email already running','Communications')
    job=env.create_job('Unrouted request','Chief of Staff')
    engine.active.update([existing['id'],job['id']])
    env.update(existing['id'],status='running')
    monkeypatch.setattr(engine,'route',lambda row:env.update(row['id'],org='Communications'))
    try:
        engine.run(job['id'])
        assert env.job(job['id'])['status']=='queued'
        assert env.job(job['id'])['org']=='Communications'
        assert [row['id'] for row in engine.dispatch_jobs()]==[job['id']]
        engine.active.remove(existing['id'])
        assert [row['id'] for row in engine.dispatch_jobs()]==[job['id']]
    finally:
        engine.stop()


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
    for _ in range(6):
        engine.run(job['id'])
        if env.job(job['id'])['status']=='blocked': break
    current=env.job(job['id'])
    assert current['status']=='blocked'
    assert 'unchanged results' in current['question'] and 'step budget' not in current['question']
    assert len(captured)==11
    assert any('Progress check:' in json.dumps(messages) for messages in captured)
    assert any('Automatic progress recovery:' in json.dumps(messages) for messages in captured)
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


def test_disconnect_during_intake_blocks_dispatch_until_reconnected(env,monkeypatch):
    monkeypatch.setitem(config.DEFAULTS,'queue_paused',False)
    engine=Engine(env,Scripted())
    job=env.create_job('Pending work','Technology')
    submitted=[]
    class Ticks:
        count=0
        def wait(self,seconds):
            self.count+=1
            return self.count>1
    engine.stop_event=Ticks()
    monkeypatch.setattr(engine.queue_intake,'check',lambda jobs:config.DEFAULTS.update(queue_paused=True))
    monkeypatch.setattr(engine.pool,'submit',lambda *args:submitted.append(args))
    engine.loop()
    assert not submitted and not engine.active
    assert env.job(job['id'])['status']=='queued'
    monkeypatch.setitem(config.DEFAULTS,'queue_paused',False)
    monkeypatch.setattr(engine.queue_intake,'check',lambda jobs:None)
    engine.stop_event=Ticks()
    engine.loop()
    assert submitted==[(engine.run,job['id'])]
    engine.pool.shutdown(wait=True)


def test_disconnect_does_not_interrupt_active_worker(env,monkeypatch):
    class Provider:
        def complete(self,*args,**kwargs):
            monkeypatch.setitem(config.DEFAULTS,'queue_paused',True)
            return perform('finish',{'summary':'Finished active work'}),{}
    engine=Engine(env,Provider())
    job=env.create_job('Already dispatched','Technology')
    try:
        engine.run(job['id'])
        assert env.job(job['id'])['status']=='completed'
    finally:
        engine.stop()


@pytest.mark.parametrize('pause_during',['model','tool'])
@pytest.mark.parametrize('child',[False,True])
def test_disconnect_checkpoints_between_agent_steps_and_resumes_without_replay(env,monkeypatch,pause_during,child):
    monkeypatch.setitem(config.DEFAULTS,'queue_paused',False)
    calls=[]
    class Provider:
        count=0
        def complete(self,messages,**kwargs):
            self.count+=1
            if self.count==1:
                if pause_during=='model':
                    config.DEFAULTS['queue_paused']=True
                return perform('write_file',{'path':'result.txt','content':'Saved evidence'}),{}
            assert any(m.get('role')=='tool' and 'Saved evidence' in m.get('content','') for m in messages)
            return perform('finish',{'summary':'Resumed from saved evidence'}),{}
    provider=Provider()
    engine=Engine(env,provider)
    parent=env.create_job('Parent','Technology') if child else None
    job=env.create_job('Perform work','Technology',parent_id=parent['id'] if parent else None)
    def execute(job,action,args):
        calls.append(action)
        if pause_during=='tool':
            config.DEFAULTS['queue_paused']=True
        return {'text':'Saved evidence'}
    monkeypatch.setattr(engine.tools,'execute',execute)
    try:
        engine.run(job['id'])
        paused=env.job(job['id'])
        assert paused['status']=='queued' and paused['step']==1
        assert paused['question']=='' and provider.count==1
        assert calls==['write_file']
        saved=session(paused)
        assert 'waiting_call' not in saved and 'waiting_action' not in saved
        assert env.one('SELECT status FROM approvals WHERE job_id=?',(job['id'],))['status']=='executed'
        assert env.one("SELECT 1 FROM events WHERE job_id=? AND kind='paused'",(job['id'],))
        # A submitted worker that reaches the engine while paused cannot run.
        engine.run(job['id'])
        assert provider.count==1 and calls==['write_file']
        monkeypatch.setitem(config.DEFAULTS,'queue_paused',False)
        engine.run(job['id'])
        assert provider.count==2 and calls==['write_file']
        assert env.job(job['id'])['status']=='completed'
        assert env.job(job['id'])['step']==2
        assert len(env.rows('SELECT * FROM approvals WHERE job_id=?',(job['id'],)))==1
    finally:
        engine.stop()


def test_disconnect_before_routing_does_not_call_model(env,monkeypatch):
    monkeypatch.setitem(config.DEFAULTS,'queue_paused',True)
    provider=Scripted()
    engine=Engine(env,provider)
    job=env.create_job('Unrouted request','Chief of Staff')
    try:
        engine.run(job['id'])
        assert env.job(job['id'])['status']=='queued'
        assert env.job(job['id'])['step']==0
        assert not env.rows('SELECT * FROM approvals')
    finally:
        engine.stop()
