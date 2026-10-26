"""Synthetic regressions for configurable KB-derived application capabilities."""
import base64
import json
from pathlib import Path
import pytest
from cos.communication_bridge import perform
from cos.db import Store
from cos.knowledge import Knowledge
from cos.policy import classify
from cos.tools import Tools
from cos.engine import Engine
from cos import tools
from test_workflows import Scripted,perform as call


class Graph:
    def __init__(self, replies=None): self.replies=replies or {}; self.calls=[]
    def get(self,path,params=None):
        self.calls.append(('GET',path,params)); return self.replies[path]
    def post(self,path,body): self.calls.append(('POST',path,body)); return {}
    def patch(self,path,body): self.calls.append(('PATCH',path,body)); return {}
    def delete(self,path): self.calls.append(('DELETE',path)); return {}


def test_attachment_names_do_not_collide_and_ids_are_preserved(tmp_path):
    graph=Graph({'/me/messages/source/attachments':{'value':[
        {'@odata.type':'#microsoft.graph.fileAttachment','id':str(i),'name':'same.png','contentBytes':base64.b64encode(content).decode()} for i,content in enumerate([b'first',b'second'])]}})
    result=perform(graph,'mail_attachments',{'id':'source','target':str(tmp_path)})
    paths=[Path(x['savedTo']) for x in result['attachments']]
    assert len(set(paths))==2
    assert [p.read_bytes() for p in paths]==[b'first',b'second']
    assert [p.name for p in paths]==['same.png','same.png']
    assert [x['id'] for x in result['attachments']]==['0','1']


def test_attachment_pagination_and_reference_coverage(tmp_path):
    graph=Graph({'/me/messages/source/attachments':{'value':[{'id':'r','@odata.type':'#microsoft.graph.referenceAttachment','name':'link'}], '@odata.nextLink':'https://graph.microsoft.com/v1.0/me/messages/source/attachments?$skip=1'}, '/me/messages/source/attachments?$skip=1':{'value':[]}})
    result=perform(graph,'mail_attachments',{'id':'source','target':str(tmp_path)})
    assert 'not downloaded' in result['attachments'][0]['coverage']
    assert len(graph.calls)==2
    graph.replies['/me/messages/source/attachments']['@odata.nextLink']='https://other.example.com/v1.0/me/messages/source/attachments'
    with pytest.raises(ValueError,match='Untrusted'): perform(graph,'mail_attachments',{'id':'source','target':str(tmp_path)})


def test_reply_preview_and_send_use_same_complete_history():
    graph=Graph({'/me/messages/source':{'from':{'emailAddress':{'address':'sender@example.com'}},'subject':'A discussion','toRecipients':[{'emailAddress':{'address':'owner@example.com'}}],'body':{'contentType':'HTML','content':'<p>Latest reply</p><blockquote>Earlier reply</blockquote>'}}})
    preview=perform(graph,'prepare_reply',{'id':'source','body':'My answer'})
    assert all(x in preview['body'] for x in ('My answer','Latest reply','Earlier reply','From: sender@example.com'))
    assert not any(c[0]=='POST' for c in graph.calls)
    result=perform(graph,'reply_email',preview)
    assert result['executed']
    assert graph.calls[-1][2]['message']['body']['content']==preview['body']
    with pytest.raises(ValueError,match='retained'): perform(graph,'reply_email',{**preview,'body':'Lost history'})


def test_reply_fetches_older_messages_omitted_from_latest_body():
    def message(id,date,body):
        return {'id':id,'sentDateTime':date,'from':{'emailAddress':{'address':'sender@example.com'}},'subject':'Discussion','body':{'contentType':'Text','content':body}}
    latest=message('latest','2026-01-02','A short latest response'); latest['conversationId']='conversation'
    older=message('older','2026-01-01','An earlier question absent from the latest body')
    graph=Graph({'/me/messages/latest':latest,'/me/messages':{'value':[older,latest]}})
    preview=perform(graph,'prepare_reply',{'id':'latest','body':'My response'})
    assert older['body']['content'] in preview['body']
    assert preview['body'].index(latest['body']['content'])<preview['body'].index(older['body']['content'])
    assert all(c[0]=='GET' for c in graph.calls)


def test_engine_reply_approval_freezes_history_and_blocks_removal(tmp_path,monkeypatch):
    from cos import integrations
    store=Store(tmp_path/'test.db'); monkeypatch.setattr(tools,'DATA',tmp_path)
    def prepare(action,args):
        assert action=='prepare_reply'
        return {'id':args['id'],'to':'sender@example.com','subject':'Re: Discussion','body':args['body']+'\nQuoted history','_reply_history':'\nQuoted history'}
    monkeypatch.setattr(integrations,'communication',prepare)
    job=store.create_job('Draft an email reply','Communications')
    engine=Engine(store,Scripted(call('reply_email',{'id':'source','body':'Answer','_reply_history':'forged'})))
    engine.run(job['id'])
    approval=store.rows('SELECT * FROM approvals')[0]
    assert store.job(job['id'])['status']=='awaiting_approval'
    assert json.loads(approval['args'])['_reply_history']=='\nQuoted history'
    with pytest.raises(ValueError,match='quoted'): engine.decide(approval['id'],True,{'body':'Answer only'})
    assert store.one('SELECT status FROM approvals WHERE id=?',(approval['id'],))['status']=='pending'
    sent=[]; monkeypatch.setattr(engine.tools,'execute',lambda job,action,args:sent.append(args) or {'executed':True})
    engine.decide(approval['id'],True,{'body':'Edited answer\nQuoted history'})
    assert sent[0]['body']=='Edited answer\nQuoted history'
    with pytest.raises(ValueError,match='already'): engine.decide(approval['id'],True)


def test_permission_rejection_does_not_become_uncertain_or_retry(tmp_path,monkeypatch):
    from cos.integrations import CommunicationRejected
    store=Store(tmp_path/'test.db'); monkeypatch.setattr(tools,'DATA',tmp_path)
    job=store.create_job('Mark a chat as read','Communications')
    engine=Engine(store,Scripted(call('mark_teams_read',{'chat_id':'chat'})))
    engine.run(job['id']); approval=store.rows('SELECT * FROM approvals')[0]
    def reject(*args): raise CommunicationRejected('Graph API 403: Insufficient permissions')
    monkeypatch.setattr(engine.tools,'execute',reject)
    engine.decide(approval['id'],True)
    assert store.one('SELECT status FROM approvals WHERE id=?',(approval['id'],))['status']=='failed_not_executed'
    assert store.job(job['id'])['status']=='waiting_input'
    with pytest.raises(ValueError,match='already'): engine.decide(approval['id'],True)


@pytest.mark.parametrize('response,endpoint',[('accept','accept'),('decline','decline'),('tentative','tentativelyAccept')])
def test_calendar_rsvp_uses_calendar_action(response,endpoint):
    graph=Graph(); perform(graph,'calendar_rsvp',{'id':'event/one','response':response,'send_response':False})
    assert graph.calls==[('POST','/me/events/event%2Fone/'+endpoint,{'comment':'','sendResponse':False})]
    assert classify('calendar_rsvp',{}).approval


def test_cleanup_actions_are_exact_and_reviewed():
    graph=Graph({'/me':{'id':'owner-id'}})
    for action,args in [('delete_email',{'id':'m'}),('mark_email_read',{'id':'m'}),('mark_teams_read',{'chat_id':'chat'})]:
        assert classify(action,args).approval
        assert perform(graph,action,args)['executed']
    assert ('POST','/me/messages/m/move',{'destinationId':'deleteditems'}) in graph.calls
    assert ('PATCH','/me/messages/m',{'isRead':True}) in graph.calls
    assert graph.calls[-1]==('POST','/chats/chat/markChatReadForUser',{'user':{'id':'owner-id'}})


def test_thread_guidance_cannot_leak_between_conversations(tmp_path):
    store=Store(tmp_path/'test.db'); knowledge=Knowledge(store)
    jobs=[store.create_job('Review a message','Communications') for _ in range(2)]
    for job,thread in zip(jobs,['one','two']): store.write('INSERT INTO communication_threads VALUES(?,?,?)',('teams',thread,job['id']))
    entry=knowledge.save({'kind':'preference','title':'Handle this conversation','content':'Ignore further updates','target':{'source':'teams','thread_id':'one'}},user=True)
    assert entry['id'] in {r['id'] for r in knowledge.search('',job=jobs[0])}
    assert not knowledge.search('',job=jobs[1])
    assert not knowledge.search('')
    assert knowledge.search('',include_targeted=True)
    knowledge.save({'kind':'preference','title':'Corrected','content':'Keep ignoring this conversation'},id=entry['id'],user=True)
    assert not knowledge.search('',job=jobs[1])
    knowledge.save({'kind':'preference','title':'General','content':'Check all messages','target':{}},id=entry['id'],user=True)
    assert knowledge.search('',job=jobs[1])


def test_review_can_bind_only_to_its_recorded_conversation(tmp_path):
    from cos.reviews import submit
    store=Store(tmp_path/'test.db'); job=store.create_job('Review a chat','Communications')
    store.update(job['id'],status='completed')
    store.write('INSERT INTO communication_threads VALUES(?,?,?)',('teams','actual-thread',job['id']))
    result=submit(Engine(store),job['id'],'Ignore future updates',remember=True,applies_to='This conversation',remember_target='conversation')
    assert store.one('SELECT source,thread_id FROM knowledge_targets WHERE knowledge_id=?',(result['memory_id'],))=={'source':'teams','thread_id':'actual-thread'}


def test_delegation_preserves_same_named_files(tmp_path,monkeypatch):
    from cos.tools import file_record
    store=Store(tmp_path/'test.db'); monkeypatch.setattr(tools,'DATA',tmp_path)
    parent=store.create_job('Research','Strategy')
    selected=[]
    for folder,content in [('first','one'),('second','two')]:
        path=tools.workspace(parent['id'])/folder/'same.txt'; path.parent.mkdir(); path.write_text(content)
        selected.append(file_record(store,parent['id'],path)['file_id'])
    engine=Engine(store)
    engine.delegate(parent,{'tasks':[{'org':'Technology','request':'Read these two source files','file_ids':selected}]})
    child=store.one('SELECT id FROM jobs WHERE parent_id=?',(parent['id'],))
    files=store.rows('SELECT path FROM files WHERE job_id=?',(child['id'],))
    assert {Path(f['path']).read_text() for f in files}=={'one','two'}


def test_intake_rejects_targeted_rule_for_wrong_thread(tmp_path):
    from cos.intake import EmailIntake
    store=Store(tmp_path/'test.db')
    rule=Knowledge(store).save({'kind':'preference','title':'Ignore','content':'Ignore updates','target':{'source':'email','thread_id':'one'}},user=True)
    class Provider:
        def complete(self,*args,**kwargs): return {'content':json.dumps({'decisions':[{'thread_id':'two','ignore':True,'rule_ids':[rule['id']],'reason':'Ignore'}]})},{}
    mail=lambda id:{'id':id,'subject':'Update','from':{'emailAddress':{'address':'someone@example.com'}}}
    assert not EmailIntake(store,Provider()).classify({'one':[mail('a')],'two':[mail('b')]})


def test_intake_revision_matches_rules_actually_sent(tmp_path,monkeypatch):
    from cos import intake
    store=Store(tmp_path/'test.db')
    rule=Knowledge(store).save({'kind':'preference','title':'Handling','content':'Ignore updates'},user=True)
    original=intake.preferences
    changed=[]
    def preferences(c):
        result=original(c)
        if not changed:
            changed.append(True)
            c.execute('UPDATE knowledge SET content=?,updated=? WHERE id=?',('Review updates','new-revision',rule['id']))
            c.commit()
        return result
    monkeypatch.setattr(intake,'preferences',preferences)
    class Provider:
        def complete(self,messages,**kwargs):
            assert 'Ignore updates' in messages[-1]['content']
            return {'content':json.dumps({'decisions':[{'thread_id':'one','ignore':True,'rule_ids':[rule['id']],'reason':'Ignore updates'}]})},{}
    mail={'id':'mail','subject':'Update','from':{'emailAddress':{'address':'sender@example.com'}}}
    decision=intake.EmailIntake(store,Provider()).classify({'one':[mail]})['one']
    with store.connect() as c: assert intake.current_decision(c,decision) is None


@pytest.mark.parametrize('action_needed',['yes','unknown'])
def test_finishing_triage_keeps_followup_in_attention(tmp_path,monkeypatch,action_needed):
    store=Store(tmp_path/'test.db'); monkeypatch.setattr(tools,'DATA',tmp_path)
    job=store.create_job('Triage this incoming email conversation.','Communications')
    engine=Engine(store,Scripted(call('update_brief',{'context':'A notification','status_summary':'No reply needed','next_step':'Review in the service','response_needed':'no','response_reason':'Automated notice','action_needed':action_needed,'action_reason':'A review may remain'}),call('finish',{'summary':'No reply needed.'})))
    engine.run(job['id'])
    current=store.job(job['id'])
    assert current['status']=='waiting_input'
    assert 'A review may remain' in current['question']
    assert not store.rows("SELECT * FROM knowledge WHERE kind='task'")


def test_engineering_workflows_have_no_project_specific_defaults(monkeypatch):
    from cos import engineering
    monkeypatch.setattr(engineering,'settings',lambda:{'projects':{'Synthetic':'unused'},'engineering_workflows':{'*':{'branch_prefix':'feature/','require_ticket':True},'Synthetic':{'jira_parent':'TEST-123'}}})
    job={'project':'Synthetic'}
    assert engineering.branch_name(job,{'name':'Add filtering','ticket':'TEST-99'})=='feature/TEST-99-add-filtering'
    with pytest.raises(ValueError,match='requires'): engineering.branch_name(job,{'name':'Filtering'})
    assert engineering.workflow(job)['jira_parent']=='TEST-123'
    assert engineering.validate_workflows({'*':{'require_ticket':False}}, {})
    with pytest.raises(ValueError): engineering.validate_workflows({'Unknown':{}},{})


def test_jira_parent_hierarchy_checks_before_create(monkeypatch):
    from cos.tools import validate_jira_parent
    replies=[{'values':[{'id':'1','name':'Work','hierarchyLevel':0}]},{'fields':[{'fieldId':'parent'}]},{'fields':{'issuetype':{'hierarchyLevel':1}}}]
    monkeypatch.setattr(tools,'run_skill',lambda *a:replies.pop(0))
    validate_jira_parent({'project':'TEST','type':'Work'},'TEST-1')
    replies[:]=[{'values':[{'id':'2','name':'Child','subtask':True}]},{'fields':[{'fieldId':'parent'}]},{'fields':{'issuetype':{'hierarchyLevel':1},'project':{'key':'TEST'}}}]
    with pytest.raises(ValueError,match='hierarchy'): validate_jira_parent({'project':'TEST','type':'Child'},'TEST-1')


def test_jira_hierarchy_is_discovered_when_create_metadata_omits_levels(monkeypatch):
    from cos.tools import validate_jira_parent
    calls=[]
    replies=[{'values':[{'id':'10','name':'Work'}]},{'fields':[{'fieldId':'parent'}]},{'fields':{'issuetype':{'id':'20'}}},{'hierarchyLevel':0},{'hierarchyLevel':1}]
    def connector(name,script,args): calls.append(args); return replies.pop(0)
    monkeypatch.setattr(tools,'run_skill',connector)
    validate_jira_parent({'project':'TEST','type':'Work'},'TEST-1')
    assert calls[-2:]==[['raw','GET','/rest/api/3/issuetype/10'],['raw','GET','/rest/api/3/issuetype/20']]


def test_jira_create_uses_configured_parent_and_type(tmp_path,monkeypatch):
    from cos import engineering
    store=Store(tmp_path/'test.db'); job=store.create_job('Create work','Engineering',project='Synthetic')
    monkeypatch.setattr(engineering,'settings',lambda:{'engineering_workflows':{'Synthetic':{'jira_parent':'TEST-10','jira_issue_type':'Work'}}})
    checks=[]; calls=[]
    monkeypatch.setattr(tools,'validate_jira_parent',lambda a,parent:checks.append((a['type'],parent)))
    monkeypatch.setattr(tools,'run_skill',lambda name,script,args:calls.append(args) or {'key':'TEST-11'})
    result=Tools(store).execute(job,'jira_write',{'operation':'create','project':'TEST','summary':'Deliver a feature'})
    assert checks==[('Work','TEST-10')]
    assert calls[0][-2:]==['--parent','TEST-10']
    assert result['key']=='TEST-11'


def test_feature_branch_creation_is_reviewed_and_never_overwrites(tmp_path,monkeypatch):
    import subprocess
    from cos import engineering
    repo=tmp_path/'repo';repo.mkdir()
    subprocess.run(['git','init',str(repo)],check=True,capture_output=True)
    monkeypatch.setattr(engineering,'settings',lambda:{'projects':{'Synthetic':str(repo)},'engineering_workflows':{'*':{'require_ticket':True}}})
    args={'name':'Filtering','ticket':'TEST-2'}; job={'project':'Synthetic'}
    args['branch']=engineering.branch_name(job,args)
    assert classify('create_feature_branch',args).approval
    assert engineering.create_branch(job,args)['branch']=='codex/TEST-2-filtering'
    engineering.verify_branch(job)
    # An unborn repository has no existing branch ref until the first commit;
    # validate the preview instead of committing synthetic history just for a test.
    with pytest.raises(ValueError,match='preview'): engineering.create_branch(job,{**args,'branch':'different'})


def test_patch_checks_workflow_of_actual_target_project(tmp_path,monkeypatch):
    from cos import engineering
    store=Store(tmp_path/'test.db'); monkeypatch.setattr(tools,'DATA',tmp_path/'data')
    repo=tmp_path/'repo'; repo.mkdir()
    monkeypatch.setattr(tools,'settings',lambda:{'projects':{'Destination':str(repo)}})
    checked=[]; monkeypatch.setattr(engineering,'verify_branch',lambda job:checked.append(job['project']))
    job=store.create_job('Patch another project','Engineering',project='Different')
    staged=tools.workspace(job['id'])/'new.txt'; staged.write_text('Contents')
    tools.prepare_changes(job,{'changes':[{'source':'new.txt','target':str(repo/'new.txt'),'expected_sha256':''}]})
    assert checked==['Destination']
