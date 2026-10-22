import json
import pytest
from cos.db import Store
from cos.engine import Engine
from cos.tools import Tools,file_record
from cos.trace import bundle,context,emit,model_call,failure
from test_workflows import Scripted,perform

def test_job_tool_and_artifact_provenance_export_without_private_content(tmp_path,monkeypatch):
    from cos import tools
    monkeypatch.setattr(tools,'DATA',tmp_path/'data')
    s=Store(tmp_path/'trace.sqlite3')
    private='Private message body and api_key=never-print-this'
    job=s.create_job('Create a private diagnostic artifact','Engineering')
    e=Engine(s,Scripted(perform('write_file',{'path':'evidence.txt','content':private}),perform('finish',{'summary':'Created the evidence file.'})))
    e.run(job['id'])
    trace=bundle(s,job['id'])
    phases=[t['phase'] for t in trace['traces']]
    assert {'job.started','tool.started','tool.succeeded','artifact.recorded','policy.decided','job.stopped'}<=set(phases)
    assert len(trace['files'][0]['provenance']['sha256'])==64
    serialized=json.dumps(trace)
    assert private not in serialized
    assert 'never-print-this' not in serialized
    assert any(t.get('trace_id') for t in trace['traces'])
    e.stop()

def test_model_failure_traces_capture_status_code_stack_not_secrets(tmp_path):
    s=Store(tmp_path/'trace.sqlite3');job=s.create_job('Test','Strategy')
    class Fake:
        @model_call
        def complete(self,messages,role='worker',tools=None):
            raise RuntimeError('Graph API 400: {"error":{"code":"ErrorInvalidRecipients","message":"api_key=do-not-log me@amd.com"}}')
    with context(store=s,job_id=job['id'],trace_id='test-trace'):
        with pytest.raises(RuntimeError):Fake().complete([{'role':'user','content':'Private request'}])
    traces=bundle(s,job['id'])['traces']
    error=next(t for t in traces if t['phase']=='model.failed')
    assert error['details']['http_status']==400
    assert error['details']['service_code']=='ErrorInvalidRecipients'
    assert error['details']['stack']
    assert 'do-not-log' not in json.dumps(traces)
    assert 'Private request' not in json.dumps(traces)

def test_trace_failure_cannot_turn_successful_tool_into_retry(tmp_path,monkeypatch):
    from cos import tools
    monkeypatch.setattr(tools,'DATA',tmp_path/'data')
    s=Store(tmp_path/'trace.sqlite3');j=s.create_job('Send approved reply','Communications')
    original=s.write
    def fail_trace(sql,args=()):
        if 'trace_events' in sql:raise OSError('Log unavailable')
        return original(sql,args)
    monkeypatch.setattr(s,'write',fail_trace)
    calls=[]
    monkeypatch.setattr(tools,'run_skill',lambda *args:calls.append(args) or {'message':'Email sent'})
    result=Tools(s).execute(j,'send_email',{'to':'a@amd.com','subject':'Subject','body':'Private body'})
    assert result['message']=='Email sent' and len(calls)==1

def test_diagnostics_export_includes_children_and_requires_local_session(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from cos import server
    s=Store(tmp_path/'trace.sqlite3')
    parent=s.create_job('Private owner request','Strategy')
    child=s.create_job('Private child request','Engineering',parent_id=parent['id'])
    monkeypatch.setattr(server,'store',s)
    c=TestClient(server.app)
    path='/api/jobs/'+parent['id']+'/diagnostics'
    assert c.get(path).status_code==401
    r=c.get('/')
    assert r.headers.get('x-request-id')
    exported=c.get(path)
    assert exported.status_code==200
    assert 'attachment' in exported.headers['content-disposition']
    assert {parent['id'],child['id']}=={j['id'] for j in exported.json()['jobs']}
    assert 'Private owner request' not in exported.text
    assert 'Private child request' not in exported.text


def test_jira_receipt_is_exported_without_body_or_url_secrets(tmp_path,monkeypatch):
    from cos import tools
    s=Store(tmp_path/'receipt.sqlite3');j=s.create_job('Post approved comment','Engineering')
    monkeypatch.setattr(tools,'run_skill',lambda *args:{'id':'123456','url':'https://amd.atlassian.net/browse/TEST-123?token=never-log-query'})
    Tools(s).execute(j,'jira_write',{'operation':'comment','id':'TEST-123','body':'Private approved comment'})
    export=bundle(s,j['id'])
    success=next(t for t in export['traces'] if t['phase']=='tool.succeeded')
    assert success['details']['provenance']['external_receipt']['id']=='123456'
    assert success['details']['provenance']['external_receipt']['url']=='https://amd.atlassian.net/browse/TEST-123'
    assert 'never-log-query' not in json.dumps(export)
    assert 'Private approved comment' not in json.dumps(export)
