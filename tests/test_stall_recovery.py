"""Exercise bounded replanning with real receipts and existing action gates."""
import json

from cos import progress
from cos.engine import Engine, session
from test_scheduling import env, perform, Scripted


def stopped(store,org='Technology'):
    job=store.create_job('Synthesize verified findings',org)
    state={'messages':[], 'stalled_steps':12, 'file_reads':{'key':{'source':'source.txt','sha256':'v1','ranges':[[0,8]],'next_offset':8,'characters':8,'complete':True}}}
    store.update(job['id'],status='blocked',question='COS is repeatedly receiving unchanged results or errors from read_file.',messages=json.dumps(state))
    return job


def test_recovery_synthesizes_and_finishes_without_rereading(env):
    job=stopped(env)
    env.write('INSERT INTO approvals(id,job_id,tool,args,status,created,result) VALUES(?,?,?,?,?,?,?)',
        ('finding',job['id'],'update_brief',json.dumps({'context':'Source v1 offsets 0-8: verified milestone October 30.'}),'executed','now','{}'))
    captured=[]
    class Provider:
        def complete(self,messages,**kwargs):
            captured.append(json.dumps(messages))
            return perform('finish',{'summary':'Verified milestone: October 30.'}),{}
    engine=Engine(env,Provider())
    try:
        engine.recover_stalled_jobs()
        engine.run(job['id'])
        assert env.job(job['id'])['status']=='completed'
        assert 'verified milestone October 30' in captured[0]
        assert 'Automatic progress recovery:' in captured[0]
        assert not env.one("SELECT 1 FROM approvals WHERE job_id=? AND tool='read_file'",(job['id'],))
        assert session(env.job(job['id']))['file_reads']['key']['complete']
    finally:
        engine.stop()


def test_pending_or_uncertain_actions_and_other_blocks_are_never_auto_resumed(env):
    jobs=[stopped(env) for _ in range(3)]
    for index,status in enumerate(['pending','executing','uncertain']):
        env.write('INSERT INTO approvals(id,job_id,tool,status) VALUES(?,?,?,?)',(str(index),jobs[index]['id'],'send_teams',status))
    other=stopped(env)
    env.update(other['id'],question='Missing credentials')
    engine=Engine(env,Scripted())
    try:
        engine.recover_stalled_jobs()
        assert all(env.job(j['id'])['status']=='blocked' for j in jobs+[other])
    finally:
        engine.stop()


def test_recovery_still_requires_exact_external_action_approval(env):
    job=stopped(env,'Communications')
    brief={'context':'Known source','status_summary':'Reply needed','next_step':'Review reply',
        'response_needed':'yes','response_reason':'Direct question','action_needed':'no','action_reason':'No other work'}
    env.write('INSERT INTO job_briefs VALUES(?,?,?)',(job['id'],json.dumps(brief),'now'))
    engine=Engine(env,Scripted(perform('send_teams',{'chat_id':'chat','body':'Proposed reply'})))
    try:
        engine.recover_stalled_jobs()
        engine.run(job['id'])
        assert env.job(job['id'])['status']=='awaiting_approval'
        assert env.one("SELECT status FROM approvals WHERE job_id=?",(job['id'],))['status']=='pending'
    finally:
        engine.stop()


def test_recovery_budget_survives_receipt_hydration_and_restart(env):
    job=stopped(env)
    attempt={'version':progress.STALL_RECOVERY_VERSION,'active':True,'remaining':2}
    state={'stall_recovery':dict(attempt),'stalled_steps':12}
    env.write('INSERT INTO approvals(id,job_id,tool,args,status,created,result) VALUES(?,?,?,?,?,?,?)',
        ('page',job['id'],'read_file','{"path":"source.txt"}','executed','now',json.dumps({'text':'evidence','source':'source.txt','sha256':'v1'})))
    progress.hydrate_reads(env,job['id'],state)
    assert state['stall_recovery']==attempt
    assert state['stalled_steps']==12
    assert progress.recovery_step(state,12)=='recovering'
    state['stall_recovery']['remaining']=0
    assert progress.recovery_step(json.loads(json.dumps(state)),12)=='exhausted'
    env.update(job['id'],messages=json.dumps({'messages':[],**state}))
    engine=Engine(env,Scripted())
    try:
        engine.recover_stalled_jobs()
        assert env.job(job['id'])['status']=='blocked'
    finally:
        engine.stop()


def test_new_evidence_can_end_recovery_but_brief_churn_cannot():
    state={'stalled_steps':12}
    assert progress.recovery_step(state,12)=='started'
    for index in range(6):
        progress.observe(state,'update_brief',{}, {'status_summary':str(index),'action_plan':str(index)})
        assert state['stall_recovery']['active']
    progress.observe(state,'read_file',{'path':'new.txt'},{'source':'new.txt','sha256':'v2','text':'new evidence'})
    assert not state['stall_recovery']['active']
    state['stalled_steps']=12
    assert progress.recovery_step(state,12)=='exhausted'


def test_findings_are_bounded_and_do_not_load_recovery_files(env):
    job=stopped(env)
    for index in range(30):
        args={key:str(index)+' source facts '*200 for key in ('context','status_summary','next_step')}
        env.write('INSERT INTO approvals(id,job_id,tool,args,status,created,result) VALUES(?,?,?,?,?,?,?)',
            (str(index),job['id'],'update_brief',json.dumps(args),'executed','now','{}'))
    notes=progress.saved_findings(env,job['id'])
    assert len(json.dumps(notes))<=12000
    assert notes[-1]['context'].startswith('29 ')
    assert notes[0]['context'].startswith('0 ')


def test_recovery_cannot_spend_budget_by_reopening_history(env,monkeypatch):
    job=stopped(env)
    engine=Engine(env,Scripted(perform('read_file',{'path':'context/history-old.txt'}),perform('finish',{'summary':'Synthesis from retained findings.'})))
    def unexpected(*args):
        raise AssertionError('Must not execute the archive read')
    monkeypatch.setattr(engine.tools,'execute',unexpected)
    try:
        engine.recover_stalled_jobs()
        engine.run(job['id'])
        assert env.job(job['id'])['status']=='completed'
        assert any('Do not reread recovery archives' in event['body'] for event in env.rows("SELECT body FROM events WHERE job_id=? AND kind='tool_error'",(job['id'],)))
    finally:
        engine.stop()


def test_completed_source_recovery_limits_verification_and_finishes(env,monkeypatch):
    job=stopped(env)
    state=session(env.job(job['id']))
    # Real file receipt: coverage is already complete before verification begins.
    result={'source':'source.txt','sha256':'v1','text':'evidence','truncated':False}
    state['file_reads']={}
    progress.observe(state,'read_file',{'path':'source.txt'},result)
    state['stalled_steps']=12
    env.update(job['id'],messages=json.dumps(state))
    reads=[]
    def execute(job,action,args):
        if action=='read_file':
            reads.append(args)
            return result
        return args
    monkeypatch.setitem(__import__('cos.config',fromlist=['DEFAULTS']).DEFAULTS,'max_steps',10)
    class Provider:
        def complete(self,messages,**kwargs):
            schema=kwargs['tools'][0]['function']['parameters']['properties']['action']
            if 'enum' in schema:
                assert 'read_file' not in schema['enum']
                assert 'finish' in schema['enum']
                assert 'Verification reads remaining: 0' in json.dumps(messages)
                return perform('finish',{'summary':'Synthesis using verified evidence.'}),{}
            return perform('read_file',{'path':'source.txt'}),{}
    engine=Engine(env,Provider())
    monkeypatch.setattr(engine.tools,'execute',execute)
    try:
        engine.recover_stalled_jobs()
        engine.run(job['id'])
        assert env.job(job['id'])['status']=='completed'
        assert len(reads)==2
    finally:
        engine.stop()


def test_recovery_read_limit_is_enforced_even_if_provider_ignores_schema():
    state={'stalled_steps':12}
    progress.recovery_step(state,12)
    progress.limit_recovery_read(state,'read_file')
    progress.limit_recovery_read(state,'read_teams')
    import pytest
    with pytest.raises(ValueError,match='Reading is disabled'):
        progress.limit_recovery_read(state,'read_mail')
    progress.limit_recovery_read(state,'finish')


def test_cached_primary_messages_are_available_without_reading_archives(env):
    from cos import recovery
    from pathlib import Path
    job=stopped(env,'Communications')
    env.write('INSERT INTO communication_sources VALUES(?,?,?,?,?,?,?,?)',
        (job['id'],'teams','message-1','Speaker','','2026-10-22','Actual retained conversation text',1))
    state={}
    recovery.conversation_evidence(env,job,state)
    record=state['conversation_evidence']
    assert 'Actual retained conversation text' in Path(record['path']).read_text(encoding='utf-8')
    assert not progress.bookkeeping_read(record['path'])
    assert env.one('SELECT job_id FROM files WHERE id=?',(record['file_id'],))['job_id']==job['id']
    recovery.conversation_evidence(env,job,state)
    assert env.one('SELECT COUNT(*) count FROM files WHERE job_id=?',(job['id'],))['count']==1
    env.write('UPDATE communication_sources SET body=? WHERE job_id=?',('Updated source evidence',job['id']))
    recovery.conversation_evidence(env,job,state)
    assert state['conversation_evidence']['digest']!=record['digest']
    assert 'Updated source evidence' in Path(record['path']).read_text(encoding='utf-8')
    empty=stopped(env,'Communications')
    other={}
    recovery.conversation_evidence(env,empty,other)
    assert not other
