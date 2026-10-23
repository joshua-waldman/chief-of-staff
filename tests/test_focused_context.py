"""Regressions for archive loops and context isolation between job workers."""
import json
from pathlib import Path

import pytest

from cos import progress
from cos.context import prepare
from cos.engine import Engine,SCHEMA,session
from cos.tools import Tools,file_record,workspace
from test_context import env,perform


def test_checkpoint_does_not_embed_historical_results_or_recovery_file_index(env):
    job=env.create_job('Synthesize findings','Technology')
    payload=json.dumps({'text':'large prior result '*2000})
    for i in range(300):
        env.write('INSERT INTO approvals(id,job_id,tool,args,status,created,result) VALUES(?,?,?,?,?,?,?)',
                  (str(i),job['id'],'read_file','{}','executed',str(i).zfill(4),payload))
    env.write('INSERT INTO approvals(id,job_id,tool,args,status,created,result) VALUES(?,?,?,?,?,?,?)',
              ('uncertain',job['id'],'send_teams','{"body":"reviewed draft"}','uncertain','0000','unknown'))
    root=workspace(job['id'])
    for i in range(30):
        p=root/'context'/f'old-{i}.txt'
        p.parent.mkdir(exist_ok=True)
        p.write_text('archive',encoding='utf-8')
        file_record(env,job['id'],p)
    state={'messages':[{'role':'system','content':'Policy'},{'role':'user','content':job['request']},
                       perform('read_file',{'path':'source.txt'}),{'role':'tool','tool_call_id':'call-read_file','content':'source '*4000}]}
    assert prepare(Engine(env),job,state,'worker',SCHEMA)
    checkpoint=Path(state['checkpoint_path'])
    saved=json.loads(checkpoint.read_text(encoding='utf-8'))
    assert checkpoint.stat().st_size<25000
    assert len(saved['actions'])==13
    assert saved['actions'][0]['status']=='uncertain'
    assert saved['files']==[]
    assert all(payload!=r.get('result') for r in saved['actions'])
    # Even an old receipt outside the compact index remains available exactly.
    reader=Tools(env)
    parts=[]
    offset=0
    while True:
        page=reader.execute(job,'read_action_receipt',{'id':'0','offset':offset})
        parts.append(page['text'])
        if page['next_offset'] is None:
            break
        offset=page['next_offset']
    assert json.loads(''.join(parts))['result']==payload
    other=env.create_job('Other job','Technology')
    with pytest.raises(ValueError,match='belong'):
        reader.execute(other,'read_action_receipt',{'id':'0'})


def test_archive_reads_with_new_names_and_limits_do_not_reset_stall_guard():
    state={}
    for i in range(20):
        progress.observe(state,'read_file',{'path':f'workspace/context/checkpoint-{i}.txt','limit':4000+i},
                         {'source':f'workspace/context/checkpoint-{i}.txt','text':str(i),'sha256':str(i)})
    assert state['stalled_steps']==20
    assert not state.get('file_reads')
    assert not progress.recovery_path('project/context/domain.txt')
    assert progress.recovery_path('workspace/CONTEXT/history.txt')
    progress.observe(state,'list_files',{'path':'context'},[{'path':'workspace/context/new-checkpoint.txt'}])
    assert state['stalled_steps']==21
    progress.observe(state,'read_file',{'path':'inputs/../context/history.txt'},
                     {'source':'workspace/context/history.txt','text':'new wrapper','sha256':'new'})
    assert state['stalled_steps']==22


def test_file_coverage_survives_changed_page_sizes_and_many_other_results():
    state={}
    def read(offset,text,sha='v1'):
        progress.observe(state,'read_file',{'path':'source.txt','offset':offset,'limit':len(text)},
                         {'source':'source.txt','sha256':sha,'text':text,'truncated':False})
    read(0,'abcdefgh')
    for i in range(300):
        progress.observe(state,'list_files',{},[str(i)])
    read(0,'abcd')
    read(4,'efgh')
    assert state['stalled_steps']==2
    read(6,'ghij')
    assert state['stalled_steps']==0
    entry=list(state['file_reads'].values())[0]
    assert entry['ranges']==[[0,10]]
    assert entry['complete'] and entry['next_offset']==10
    read(0,'new','v2')
    assert state['stalled_steps']==0 and len(state['file_reads'])==2


def test_large_archive_read_is_not_archived_into_another_content_wrapper(env):
    job=env.create_job('Verify one fact','Technology')
    source=str(workspace(job['id'])/'context'/'old-content.txt')
    result=json.dumps({'source':source,'sha256':'same','text':'evidence '*5000,'next_offset':40000})
    state={'messages':[{'role':'system','content':'Policy'},{'role':'user','content':job['request']},
                       perform('read_file',{'path':source}),{'role':'tool','tool_call_id':'call-read_file','content':result}]}
    assert prepare(Engine(env),job,state,'worker',SCHEMA)
    compact=json.loads(next(m['content'] for m in state['messages'] if m['role']=='tool'))
    assert compact['source']==source
    assert 'full_content_path' not in compact
    assert not list((workspace(job['id'])/'context').glob('content-*'))
    assert not prepare(Engine(env),job,state,'worker',SCHEMA)


def test_file_pages_are_bounded_even_when_model_requests_a_large_limit(env):
    job=env.create_job('Read source','Technology')
    path=workspace(job['id'])/'source.txt'
    path.write_text('facts '*10000,encoding='utf-8')
    result=Tools(env).execute(job,'read_file',{'path':str(path),'limit':80000})
    assert len(result['text'])==8000 and result['next_offset']==8000


def test_delegation_has_only_selected_inputs_and_returns_only_declared_outputs(env):
    parent=env.create_job('Coordinate independent analysis','Technology')
    env.update(parent['id'],messages=json.dumps({'messages':[{'role':'user','content':'Parent private history'}]}))
    root=workspace(parent['id'])
    ids=[]
    for name in ['a.txt','unrelated.txt','context/checkpoint.txt']:
        p=root/name
        p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(name,encoding='utf-8')
        ids.append(file_record(env,parent['id'],p)['file_id'])
    engine=Engine(env)
    engine.delegate(parent,{'tasks':[{'org':'Technology','request':'Analyze A; return a supported conclusion.','file_ids':[ids[0]]},
                                     {'org':'Technology','request':'Independent task; no files required.'}]})
    children=env.rows('SELECT * FROM jobs WHERE parent_id=? ORDER BY created',(parent['id'],))
    child=children[0]
    assert child['request']=='Analyze A; return a supported conclusion.' and session(child)['messages']==[]
    assert [f['name'] for f in env.rows('SELECT * FROM files WHERE job_id=?',(child['id'],))]==['a.txt']
    assert not env.rows('SELECT * FROM files WHERE job_id=?',(children[1]['id'],))
    output=workspace(child['id'])/'findings.txt'
    output.write_text('Verified findings',encoding='utf-8')
    output_id=file_record(env,child['id'],output)['file_id']
    archive=workspace(child['id'])/'context'/'checkpoint.txt'
    archive.parent.mkdir(exist_ok=True)
    archive.write_text('Child private history',encoding='utf-8')
    file_record(env,child['id'],archive)
    env.update(child['id'],status='completed',result='Conclusion',messages=json.dumps({'messages':[],'deliverable_file_ids':[output_id]}))
    env.update(children[1]['id'],status='completed',result='Other conclusion')
    env.update(parent['id'],status='waiting_children',messages=json.dumps({'messages':[],'waiting_call':'delegate-call'}))
    engine.release_parents()
    result=json.loads(session(env.job(parent['id']))['messages'][-1]['content'])
    assert len(result[0]['files'])==1 and result[0]['files'][0]['name']=='findings.txt'
    assert 'Child private history' not in json.dumps(result)
    transferred=env.rows("SELECT name FROM files WHERE job_id=? AND path LIKE '%delegated%'",(parent['id'],))
    assert [r['name'] for r in transferred]==['findings.txt']


def test_delegation_validates_all_selections_before_starting_any_children(env):
    parent=env.create_job('Coordinate','Technology')
    archive=workspace(parent['id'])/'context'/'history.txt'
    archive.parent.mkdir()
    archive.write_text('Private recovery state',encoding='utf-8')
    fid=file_record(env,parent['id'],archive)['file_id']
    tasks=[{'org':'Technology','request':'Valid small task'},
           {'org':'Technology','request':'Invalid archive handoff','file_ids':[fid]}]
    with pytest.raises(ValueError,match='Recovery archives'):
        Engine(env).delegate(parent,{'tasks':tasks})
    assert not env.rows('SELECT * FROM jobs WHERE parent_id=?',(parent['id'],))


def test_legacy_read_receipts_restore_coverage_without_replaying_tools(env):
    job=env.create_job('Resume saved analysis','Technology')
    result={'source':'transcript.txt','sha256':'verified','text':'transcript','truncated':False,'next_offset':None}
    env.write('INSERT INTO approvals(id,job_id,tool,args,status,created,result) VALUES(?,?,?,?,?,?,?)',
              ('read-once',job['id'],'read_file','{"path":"transcript.txt"}','executed','now',json.dumps(result)))
    state={'stalled_steps':12,'last_progress_action':'read_file','reads_since_brief':4}
    before=env.one('SELECT COUNT(*) AS n FROM events')['n']
    progress.hydrate_reads(env,job['id'],state)
    assert state['stalled_steps']==12 and state['reads_since_brief']==4
    assert list(state['file_reads'].values())[0]['complete']
    progress.hydrate_reads(env,job['id'],state)
    assert env.one('SELECT COUNT(*) AS n FROM events')['n']==before
    progress.observe(state,'read_file',{'path':'transcript.txt'},result)
    assert state['stalled_steps']==13


def test_reading_batches_require_saved_findings_before_more_source_pages(env,monkeypatch):
    from test_workflows import Scripted
    calls=[perform('read_file',{'path':f'source-{i}.txt'}) for i in range(13)]
    calls.extend([perform('update_brief',{'context':'Verified findings for sources 0 through 11','next_step':'Read source 12'}),
                  perform('read_file',{'path':'source-12.txt'}),perform('finish',{'summary':'Analysis complete'})])
    engine=Engine(env,Scripted(*calls))
    original=engine.tools.execute
    reads=[]
    def execute(job,action,args):
        if action=='read_file':
            reads.append(args['path'])
            return {'source':args['path'],'sha256':'stable','text':'Source fact','truncated':False}
        return original(job,action,args)
    monkeypatch.setattr(engine.tools,'execute',execute)
    job=env.create_job('Analyze independent sources','Technology')
    engine.run(job['id'])
    assert env.job(job['id'])['status']=='completed'
    assert reads==[f'source-{i}.txt' for i in range(13)]
    assert any('Save this reading batch first' in e['body'] for e in env.rows("SELECT body FROM events WHERE kind='tool_error'"))
