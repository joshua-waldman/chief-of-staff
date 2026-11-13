import json
import pytest
from cos.mail import recipients,prepare,definitely_not_sent,SendNotSubmitted
from cos.db import Store
from cos.engine import Engine,session
from cos.tools import Tools
from cos.reviews import submit,REVIEW_POLICY
from test_workflows import Scripted,perform

def test_recipient_lists_preserve_each_intended_address():
    assert recipients('a@amd.com; b@amd.com, c@amd.com;')=='a@amd.com,b@amd.com,c@amd.com'
    assert recipients(['a@amd.com','b@amd.com; a@amd.com'])=='a@amd.com,b@amd.com'
    assert recipients('"Last, First" <first@amd.com>; Second <second@amd.com>')=='first@amd.com,second@amd.com'
    for bad in ['a@amd.com; not-an-address','a@amd.com\r\nBcc: secret@amd.com','bad@@amd.com','"unclosed <a@amd.com>','',[]]:
        with pytest.raises(SendNotSubmitted):recipients(bad)

def test_email_adapter_passes_comma_separated_recipients_to_installed_skill(tmp_path,monkeypatch):
    from cos import tools
    monkeypatch.setattr(tools,'DATA',tmp_path)
    s=Store(tmp_path/'mail.sqlite3');job=s.create_job('Send approved email','Communications')
    calls=[]
    monkeypatch.setattr(tools,'run_skill',lambda name,script,args:calls.append((name,script,args)) or {'message':'Email sent'})
    Tools(s).execute(job,'send_email',{'to':'a@amd.com; b@amd.com','cc':['c@amd.com'],'bcc':'d@amd.com; e@amd.com','subject':'Subject','body':'Approved text'})
    args=calls[0][2]
    assert args[args.index('--to')+1]=='a@amd.com,b@amd.com'
    assert args[args.index('--cc')+1]=='c@amd.com'
    assert args[args.index('--bcc')+1]=='d@amd.com,e@amd.com'

def test_graph_invalid_recipients_is_definitely_not_sent_but_timeouts_are_uncertain(tmp_path,monkeypatch):
    s=Store(tmp_path/'mail.sqlite3')
    e=Engine(s,Scripted(perform('send_email',{'to':'a@amd.com; b@amd.com','subject':'S','body':'B'})))
    job=s.create_job('Draft a reply','Communications');e.run(job['id'])
    approval=s.one("SELECT * FROM approvals WHERE job_id=? AND status='pending'",(job['id'],))
    assert json.loads(approval['args'])['to']=='a@amd.com,b@amd.com'
    def reject(*args):raise RuntimeError('ERROR: Graph API 400: {"error":{"code":"ErrorInvalidRecipients","message":"All recipients must be resolved before a message can be submitted."}}')
    monkeypatch.setattr(e.tools,'execute',reject)
    e.decide(approval['id'],True)
    assert s.one('SELECT status FROM approvals WHERE id=?',(approval['id'],))['status']=='failed_not_sent'
    assert s.job(job['id'])['status']=='waiting_input'
    assert 'not_sent' in json.dumps(session(s.job(job['id'])))
    assert not definitely_not_sent('send_email',TimeoutError('timeout'))
    assert not definitely_not_sent('send_email',RuntimeError('Graph API 503: ErrorInvalidRecipients'))
    e.stop()

def test_jira_review_feedback_is_owner_direction_not_a_conflicting_disclaimer(tmp_path):
    s=Store(tmp_path/'jira.sqlite3');e=Engine(s,Scripted())
    job=s.create_job('Post requested comment to TEST-123','Communications')
    s.update(job['id'],status='waiting_input')
    submit(e,job['id'],'Yes I approve posting this to the Jira ticket.')
    persisted=json.dumps(session(s.job(job['id'])))
    assert 'Yes I approve posting this to the Jira ticket.' in persisted
    assert 'This message is not send approval' not in persisted
    from cos.policy import classify
    assert classify('jira_write', {'operation': 'comment'}, 'Engineering').approval
    assert not s.rows('SELECT * FROM approvals')
    e.stop()


def test_jira_write_failure_never_blindly_repeats_a_comment(tmp_path,monkeypatch):
    s=Store(tmp_path/'jira-failure.sqlite3')
    e=Engine(s,Scripted(perform('jira_write',{'operation':'comment','id':'TEST-123','body':'Approved comment'})))
    called=[]
    def timeout(*args):
        called.append(True)
        raise TimeoutError('Response timed out')
    monkeypatch.setattr(e.tools,'execute',timeout)
    job=s.create_job('Post approved comment','Engineering');e.run(job['id'])
    assert not called
    assert s.job(job['id'])['status']=='awaiting_approval'
    approval=s.one("SELECT id FROM approvals WHERE job_id=? AND status='pending'",(job['id'],))
    e.decide(approval['id'],True)
    assert called==[True]
    assert s.job(job['id'])['status']=='blocked'
    assert s.one('SELECT status FROM approvals WHERE job_id=?',(job['id'],))['status']=='uncertain'
    assert session(s.job(job['id'])).get('waiting_call')
    e.stop()
