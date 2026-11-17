from cos.db import Store
from cos.monitor import Monitor
from cos.monitor_bridge import stable_mail_client,mail_identity_aliases
import json
import pytest
from cos.communication_bridge import perform
from cos.conversations import save_source,cache_source_read,read_email,recover_email_questions


def test_mail_headers_retain_skill_authentication():
    class Existing:
        def _headers(self):
            return {'Authorization':'test-auth','Content-Type':'application/json'}
    headers=stable_mail_client(Existing)._headers()
    assert headers['Authorization']=='test-auth'
    assert headers['Prefer']=='IdType="ImmutableId"'


def test_legacy_migration_and_folder_moves_do_not_queue_again(tmp_path):
    store=Store(tmp_path/'test.db'); monitor=Monitor(store)
    assert monitor.enqueue_once('email','old-id','Original','Communications')
    assert not monitor.enqueue_once('email','stable-id','Migrated','Communications',aliases=['old-id'])
    assert not monitor.enqueue_once('email','stable-id','Moved','Communications',aliases=['new-folder-id'])
    assert not monitor.enqueue_once('email','new-folder-id','Legacy lookup','Communications')
    assert len(store.rows('SELECT * FROM jobs'))==1
    assert monitor.enqueue_once('email','different-id','Different message','Communications')


def test_alias_translation_uses_exact_ids_and_keeps_untranslatable_messages():
    class Fake:
        def post(self,path,body):
            assert path=='/me/translateExchangeIds'
            assert body=={'inputIds':['stable-a','stable-b'],'sourceIdType':'restImmutableEntryId','targetIdType':'restId'}
            return {'value':[{'sourceId':'stable-a','targetId':'legacy-a'},{'sourceId':'stable-b','errorDetails':{'code':'NotFound'}}]}
    messages=mail_identity_aliases(Fake(),[{'id':'stable-a'},{'id':'stable-b'}])
    assert messages==[{'id':'stable-a','legacy_id':'legacy-a'},{'id':'stable-b','legacy_id':''}]
    assert mail_identity_aliases(Fake(),[])==[]


def mail(identifier='stable', internet='<notice@example.com>'):
    return {'id':identifier,'internetMessageId':internet,'subject':'Update',
            'from':{'emailAddress':{'address':'sender@example.com'}},
            'body':{'contentType':'text','content':'Full body'},'receivedDateTime':'2026-01-01T12:00:00Z',
            'attachments':[{'id':'attachment','name':'notes.txt','size':20}]}


def test_bridge_reads_encoded_immutable_id_with_complete_metadata():
    class Graph:
        def get(self,path,params):
            assert path=='/me/messages/stable%2F%2B%3D'
            assert 'body' in params['$select'] and 'internetMessageId' in params['$select']
            assert params['$expand'].startswith('attachments(')
            return mail('stable/+=')
    result=perform(Graph(),'read_mail',{'id':'stable/+='})
    assert result['id']=='stable/+='
    assert result['body']=='Full body'
    assert result['attachments'][0]['id']=='attachment'


@pytest.mark.parametrize('error',['ErrorInvalidIdMalformed','ErrorItemNotFound','Graph API 404: missing'])
def test_moved_email_recovers_by_exact_internet_id(error):
    class Graph:
        def __init__(self): self.calls=[]
        def get(self,path,params):
            self.calls.append(path)
            if path=='/me/messages/old': raise RuntimeError(error)
            if path=='/me/messages':
                assert params['$filter']=="internetMessageId eq '<notice@example.com>'"
                return {'value':[{'id':'stable','internetMessageId':'<notice@example.com>'}]}
            assert path=='/me/messages/stable'
            return mail()
    graph=Graph()
    assert perform(graph,'read_mail',{'id':'old','internet_message_id':'<notice@example.com>'})['id']=='stable'
    assert len(graph.calls)==3


@pytest.mark.parametrize('matches',[
    {'value':[]},
    {'value':[{'id':'a','internetMessageId':'<notice@example.com>'},{'id':'b','internetMessageId':'<notice@example.com>'}]},
    {'value':[{'id':'a','internetMessageId':'<other@example.com>'}]},
    {'value':[{'id':'a','internetMessageId':'<notice@example.com>'}],'@odata.nextLink':'more'},
])
def test_recovery_rejects_ambiguous_or_different_messages(matches):
    class Graph:
        def get(self,path,params):
            if path=='/me/messages/old': raise RuntimeError('ErrorItemNotFound')
            assert path=='/me/messages'
            return matches
    with pytest.raises(ValueError,match='uniquely'):
        perform(Graph(),'read_mail',{'id':'old','internet_message_id':'<notice@example.com>'})


@pytest.mark.parametrize('error',['Graph API 403: denied','Graph API 401: expired','Graph API 429: throttled','Graph API 500: unavailable'])
def test_other_failures_do_not_trigger_identity_search(error):
    calls=[]
    class Graph:
        def get(self,path,params):
            calls.append(path)
            raise RuntimeError(error)
    with pytest.raises(RuntimeError,match=error):
        perform(Graph(),'read_mail',{'id':'old','internet_message_id':'<notice@example.com>'})
    assert calls==['/me/messages/old']


def test_alternate_id_read_clears_original_gate_without_merging_similar_notices(tmp_path,monkeypatch):
    from cos.briefs import update_brief
    from cos.message_triage import read_targets
    from cos import recovery
    store=Store(tmp_path/'test.db'); job=store.create_job('Triage this incoming email conversation.','Communications')
    with store.connect() as c:
        for identifier in ['stable','similar']:
            save_source(c,job['id'],'email',identifier,'sender@example.com','Update','2026-01-01','Preview',False)
    monkeypatch.setattr('cos.integrations.communication',lambda action,args:{'id':'stable','body':'Full body','subject':'Update'})
    result=read_email(store,job['id'],'legacy')
    assert store.one("SELECT complete FROM communication_sources WHERE message_id='stable'")['complete']==1
    assert store.one("SELECT complete FROM communication_sources WHERE message_id='similar'")['complete']==0
    assert len(store.detail(job['id'])['source_messages'])==2
    state={}
    recovery.record(store,job['id'],state,'read_mail',{'id':'legacy'},'ErrorInvalidIdMalformed',error=True)
    recovery.record(store,job['id'],state,'read_mail',{'id':'stable'},result)
    assert not state['active_errors']
    # The unrelated unread notice still blocks dismissal.
    assert update_brief(store,job,{'response_needed':'no','action_needed':'no','triage_category':'reading'})['triage_category']=='review'
    cache_source_read(store,job['id'],'read_mail',{'id':'similar'},{'id':'similar','body':'Another body'})
    update_brief(store,job,{'response_needed':'no','action_needed':'no','triage_category':'reading'})
    store.update(job['id'],status='completed')
    assert {t['args']['id'] for t in read_targets(store,store.job(job['id']))}=={'stable','similar'}
    store.write('INSERT INTO approvals(id,job_id,tool,args,status) VALUES(?,?,?,?,?)',
                ('read',job['id'],'mark_email_read',json.dumps({'id':'legacy'}),'executed'))
    assert {t['args']['id'] for t in read_targets(store,store.job(job['id']))}=={'similar'}


def test_monitor_retains_internet_identity_for_moved_message_recovery(tmp_path,monkeypatch):
    from cos import integrations
    store=Store(tmp_path/'test.db'); monitor=Monitor(store)
    monkeypatch.setattr(monitor.email_intake,'classify',lambda threads:{})
    monitor.process({'messages':[],'emails':[{'id':'old','internetMessageId':'<notice@example.com>',
        'from':{'emailAddress':{'address':'sender@example.com'}},'subject':'Update'}]})
    job=store.one('SELECT * FROM jobs')
    calls=[]
    monkeypatch.setattr(integrations,'communication',lambda action,args:calls.append((action,args)) or {'id':'stable','body':'Full body'})
    read_email(store,job['id'],'old')
    assert calls==[('read_mail',{'id':'old','internet_message_id':'<notice@example.com>'})]
    read_email(store,job['id'],'old')
    assert calls[-1][1]['id']=='stable'


def waiting_job(store,question='Can you provide a valid Outlook message ID? The original source ID is malformed.'):
    job=store.create_job('Triage this incoming email conversation.','Communications')
    call={'id':'ask','type':'function','function':{'name':'perform','arguments':json.dumps({'action':'ask_user','arguments':json.dumps({'question':question})})}}
    state={'messages':[{'role':'assistant','tool_calls':[call]}],'waiting_call':'ask'}
    store.update(job['id'],status='waiting_input',question=question,messages=json.dumps(state))
    store.event(job['id'],'tool_error','Cannot finish while source bodies remain unread. Sources: stable')
    return store.job(job['id'])


@pytest.mark.parametrize('approval',[None,'pending','executing','uncertain'])
def test_recovery_preserves_approvals_and_retries_only_once(tmp_path,approval):
    store=Store(tmp_path/'test.db');job=waiting_job(store)
    if approval:
        store.write('INSERT INTO approvals(id,job_id,status) VALUES(?,?,?)',('approval',job['id'],approval))
    recover_email_questions(store)
    current=store.job(job['id'])
    assert current['status']==('waiting_input' if approval else 'queued')
    if not approval:
        assert not current['question']
        state=json.loads(current['messages'])
        assert state['messages'][-1]['role']=='tool'
        assert 'user_answer' not in state['messages'][-1]['content']
        store.update(job['id'],status='waiting_input')
        recover_email_questions(store)
        assert store.job(job['id'])['status']=='waiting_input'


def test_successful_source_load_resumes_gate_question_but_preserves_substantive_questions(tmp_path):
    from cos.recovery import target
    store=Store(tmp_path/'test.db');job=waiting_job(store)
    other=waiting_job(store,'Which release deadline should I use?')
    state=json.loads(store.job(job['id'])['messages'])
    state['active_errors']={
        target('read_mail',{'id':'stable'}):{'tool':'read_mail','message':'ErrorInvalidIdMalformed'},
        target('finish',{}):{'tool':'finish','message':'Cannot finish while source bodies remain unread.'},
        'unrelated':{'tool':'calendar_events','message':'Calendar unavailable'}}
    store.update(job['id'],messages=json.dumps(state))
    cache_source_read(store,job['id'],'read_mail',{'id':'stable'},{'id':'stable','body':'Full body'})
    recover_email_questions(store)
    assert store.job(job['id'])['status']=='queued'
    assert list(json.loads(store.job(job['id'])['messages'])['active_errors'])==['unrelated']
    assert store.job(other['id'])['status']=='waiting_input'


def test_server_translation_recovers_legacy_id_without_subject_search():
    class Graph:
        def post(self,path,body):
            assert path=='/me/translateExchangeIds'
            assert body=={'inputIds':['legacy'],'sourceIdType':'restId','targetIdType':'restImmutableEntryId'}
            return {'value':[{'sourceId':'unrelated','targetId':'wrong'},
                             {'sourceId':'legacy','targetId':'stable'}]}
        def get(self,path,params):
            if path=='/me/messages/legacy': raise RuntimeError('ErrorItemNotFound')
            assert path=='/me/messages/stable'
            return mail()
    assert perform(Graph(),'read_mail',{'id':'legacy'})['id']=='stable'


def test_missing_body_is_not_cached_as_a_success(tmp_path,monkeypatch):
    store=Store(tmp_path/'test.db');job=store.create_job('Mail','Communications')
    with store.connect() as c:
        save_source(c,job['id'],'email','stable','','','','Preview',False)
    monkeypatch.setattr('cos.integrations.communication',lambda *args:{'id':'stable','body':None})
    with pytest.raises(ValueError,match='Missing message body'):
        read_email(store,job['id'],'stable')
    assert not store.one('SELECT complete FROM communication_sources')['complete']


def test_later_full_source_load_can_resolve_a_failed_automatic_retry(tmp_path):
    store=Store(tmp_path/'test.db');job=waiting_job(store)
    with store.connect() as c:
        save_source(c,job['id'],'email','stable','','','','Preview',False)
    recover_email_questions(store)
    state=store.job(job['id'])['messages']
    # The automatic retry still could not retrieve the body; a later UI source
    # load succeeds and can now resume without another restart or owner answer.
    store.update(job['id'],status='waiting_input',question=job['question'],messages=state)
    cache_source_read(store,job['id'],'read_mail',{'id':'stable'},{'id':'stable','body':'Full body'})
    assert store.job(job['id'])['status']=='queued'
    assert json.loads(store.job(job['id'])['messages'])['email_id_recovery']==2


def envelope(store,job,identifier='original',internet='<notice@example.com>',sender='sender@example.com',date='2026-01-01T12:00:00Z'):
    with store.connect() as c:
        save_source(c,job['id'],'email',identifier,sender,'Update',date,'Preview',False,internet_id=internet)


def full_read(store,job,identifier='search-result',internet='<notice@example.com>'):
    result={'id':identifier,'internetMessageId':internet,'from':'sender@example.com',
            'subject':'Update','date':'2026-01-01T12:00:00Z','body':'Full body'}
    cache_source_read(store,job['id'],'read_mail',{'id':identifier},result)


def test_search_result_reconciles_original_source_and_read_receipt(tmp_path):
    from cos.recovery import record
    store=Store(tmp_path/'test.db'); job=store.create_job('Triage this incoming email conversation.','Communications')
    envelope(store,job)
    envelope(store,job,'other','<other@example.com>')
    full_read(store,job)
    original=store.one("SELECT * FROM communication_sources WHERE message_id='original'")
    assert original['complete']==1 and original['body']=='Full body'
    assert store.one("SELECT canonical_id FROM email_source_ids WHERE message_id='original'")['canonical_id']=='search-result'
    assert store.one("SELECT complete FROM communication_sources WHERE message_id='other'")['complete']==0
    assert len(store.detail(job['id'])['source_messages'])==2
    state={}
    record(store,job['id'],state,'read_mail',{'id':'original'},'ErrorInvalidIdMalformed',error=True)
    record(store,job['id'],state,'read_mail',{'id':'search-result'},{'id':'search-result'})
    assert not state['active_errors']


def test_new_envelope_reuses_previously_read_search_result(tmp_path):
    store=Store(tmp_path/'test.db');job=store.create_job('Mail','Communications')
    full_read(store,job)
    envelope(store,job)
    assert store.one("SELECT complete FROM communication_sources WHERE message_id='original'")['complete']==1
    assert store.one("SELECT canonical_id FROM email_source_ids WHERE message_id='original'")['canonical_id']=='search-result'


@pytest.mark.parametrize('internet,sender,date',[
    ('','sender@example.com','2026-01-01T12:00:00Z'),
    ('<other@example.com>','sender@example.com','2026-01-01T12:00:00Z'),
    ('<notice@example.com>','other@example.com','2026-01-01T12:00:00Z'),
    ('<notice@example.com>','sender@example.com','2026-01-02T12:00:00Z'),
])
def test_reconciliation_keeps_missing_or_conflicting_identity_unread(tmp_path,internet,sender,date):
    store=Store(tmp_path/'test.db'); job=store.create_job('Mail','Communications')
    envelope(store,job,internet=internet,sender=sender,date=date)
    full_read(store,job)
    assert store.one("SELECT complete FROM communication_sources WHERE message_id='original'")['complete']==0


def test_reconciliation_rejects_duplicate_mailbox_items_with_same_rfc_id(tmp_path):
    store=Store(tmp_path/'test.db'); job=waiting_job(store)
    envelope(store,job,internet='')
    full_read(store,job,'copy-one')
    full_read(store,job,'copy-two')
    envelope(store,job)
    recover_email_questions(store,job['id'],only_complete=True)
    assert store.one("SELECT complete FROM communication_sources WHERE message_id='original'")['complete']==0
    assert store.job(job['id'])['status']=='waiting_input'


@pytest.mark.parametrize('previous_retry',[False,True])
def test_startup_reconciles_saved_search_read_after_malformed_retry(tmp_path,previous_retry):
    store=Store(tmp_path/'test.db')
    job=waiting_job(store,'The four email IDs are malformed. Can you provide canonical item IDs?')
    # Existing jobs may have read the alternate ID before reconciliation existed.
    full_read(store,job)
    envelope(store,job)
    # Simulate an envelope left incomplete by the previous implementation.
    store.write("UPDATE communication_sources SET complete=0,body='Preview' WHERE job_id=? AND message_id='original'",(job['id'],))
    store.write("UPDATE email_source_ids SET canonical_id='' WHERE job_id=? AND message_id='original'",(job['id'],))
    state=json.loads(job['messages'])
    if previous_retry:
        state['email_id_recovery']=1
    store.update(job['id'],status='waiting_input',question=job['question'],messages=json.dumps(state))
    store.event(job['id'],'tool_error','ERROR: Graph API 400: ErrorInvalidIdMalformed')
    recover_email_questions(store)
    current=store.job(job['id'])
    assert current['status']=='queued' and not current['question']
    assert store.one("SELECT complete FROM communication_sources WHERE message_id='original'")['complete']==1
    recovered=json.loads(current['messages'])
    assert recovered['email_id_recovery']==2
    assert recovered['messages'][-1]['tool_call_id']=='ask'
    assert 'user_answer' not in recovered['messages'][-1]['content']
    store.update(job['id'],status='waiting_input',question=job['question'])
    recover_email_questions(store)
    assert store.job(job['id'])['status']=='waiting_input'


@pytest.mark.parametrize('blocker',['pending','executing','uncertain','waiting_action','unrelated_error','no_gate'])
def test_reconciliation_recovery_preserves_other_blockers(tmp_path,blocker):
    store=Store(tmp_path/'test.db');job=waiting_job(store)
    if blocker in {'pending','executing','uncertain'}:
        store.write('INSERT INTO approvals(id,job_id,status) VALUES(?,?,?)',('approval',job['id'],blocker))
    elif blocker=='waiting_action':
        state=json.loads(job['messages']);state['waiting_action']={'action':'reply_email'}
        store.update(job['id'],messages=json.dumps(state))
    elif blocker=='unrelated_error':
        store.event(job['id'],'tool_error','Graph API 403: denied')
    else:
        store.write('DELETE FROM events WHERE job_id=?',(job['id'],))
        store.event(job['id'],'tool_error','ErrorInvalidIdMalformed')
    recover_email_questions(store)
    assert store.job(job['id'])['status']=='waiting_input'
