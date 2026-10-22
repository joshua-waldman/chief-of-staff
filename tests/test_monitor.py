import json
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from cos.db import Store
from cos.monitor import Monitor
from cos.monitor_bridge import collect_teams,message_page


BASE='https://amer.ng.msg.teams.microsoft.com/v1'
SINCE='2026-10-21T20:11:09+00:00'


class Skype:
    _msg_base=BASE

    def get_skypetoken(self):
        return 'test-only'

    def _hdr(self):
        return {'X-Skypetoken':'test-only'}


def test_continuation_url_is_followed_directly():
    skype=Skype()
    cid='19:a_b@unq.gbl.spaces'
    url=BASE+'/users/ME/conversations/'+quote(cid,safe='')+'/messages?startTime=1&syncState=token&pageSize=100'
    calls=[]
    def http(method,target,headers):
        calls.append(target)
        return 200,{'messages':[{'id':'older'}]},''
    assert message_page(skype,cid,url,http)==([{'id':'older'}],'')
    assert calls==[url]


@pytest.mark.parametrize('url',[
    'https://attacker.example/v1/users/ME/conversations/chat/messages?syncState=x',
    BASE+'/users/ME/conversations/different/messages?syncState=x',
    BASE+'/users/ME/conversations/chat/messages/extra',
    BASE.replace('https:','http:')+'/users/ME/conversations/chat/messages',
    BASE+'/users/ME/conversations/chat/messages#fragment',
])
def test_untrusted_continuation_never_receives_credentials(url):
    with pytest.raises(ValueError,match='Untrusted'):
        message_page(Skype(),'chat',url,lambda *args:pytest.fail('Must not send credentials'))


def test_partial_teams_failure_keeps_other_chats_and_first_page():
    skype=Skype()
    def page(cid,page_size,sync_state):
        if cid=='broken' and sync_state:
            raise RuntimeError('HTTP 400')
        if cid=='48:notes':
            return [],''
        return [{'id':cid,'composetime':'2026-10-22T01:00:00Z','messagetype':'Text','from':'other','content':'Hello'}],('token' if cid=='broken' else '')
    skype.list_messages=page
    def http(*args):
        return 200,{'conversations':[{'id':'broken'},{'id':'good'}]},''
    result=collect_teams(skype,{'id':'owner'},SINCE,http)
    assert {m['id'] for m in result['messages']}=={'broken','good'}
    assert result['failures']==[{'chat_id':'broken','reason':'HTTP 400'}]
    assert 'broken' not in result['readable_conversations']
    assert 'good' in result['readable_conversations']


def test_repeated_message_continuation_retains_checkpoint():
    skype=Skype()
    skype.list_messages=lambda cid,**kw: ([{'id':'m','composetime':'2026-10-22T01:00:00Z','messagetype':'Text'}],'repeated') if cid!='48:notes' else ([], '')
    result=collect_teams(skype,{'id':'owner'},SINCE,lambda *args:(200,{'conversations':[{'id':'chat'}]},''))
    assert 'repeated' in result['failures'][0]['reason']
    assert 'chat' not in result['readable_conversations']


def setup_monitor(tmp_path,monkeypatch):
    import cos.monitor as module
    monkeypatch.setattr(module,'gateway_key',lambda:'test-key')
    store=Store(tmp_path/'monitor.db')
    store.set_state('monitor_checkpoint',SINCE)
    return Monitor(store)


def response(emails=None,messages=None,failures=None):
    return SimpleNamespace(returncode=0,stdout=json.dumps({'emails':emails or [],'messages':messages or [],'conversations':2,'failures':failures or []}),stderr='')


def test_teams_failure_does_not_block_email_and_recovery_deduplicates(tmp_path,monkeypatch):
    m=setup_monitor(tmp_path,monkeypatch)
    email={'id':'mail','from':{'emailAddress':{'address':'other@amd.com'}},'subject':'An ask'}
    message={'id':'m','chat_id':'chat','owner':False,'text':'An ask'}
    broken=True
    calls=[]
    def run(args,**kwargs):
        calls.append(args[-1])
        if args[-1]=='email':
            return response(emails=[email])
        return response(messages=[message],failures=[{'chat_id':'broken','reason':'HTTP 400'}] if broken else [])
    monkeypatch.setattr('cos.monitor.subprocess.run',run)
    m.tick()
    assert calls==['email','teams']
    assert m.store.state('monitor_checkpoint_email')!=SINCE
    assert m.store.state('monitor_checkpoint_teams')==SINCE
    assert m.store.state('monitor_checkpoint')==SINCE
    assert m.store.state('monitor_status')['attention_required']
    assert len(m.store.rows('SELECT * FROM jobs'))==2
    broken=False
    m.tick()
    assert m.store.state('monitor_checkpoint_teams')!=SINCE
    assert not m.store.state('monitor_status')['attention_required']
    assert 'error' not in m.store.state('monitor_status')
    assert len(m.store.rows('SELECT * FROM jobs'))==2


def test_mail_process_failure_does_not_block_teams(tmp_path,monkeypatch):
    m=setup_monitor(tmp_path,monkeypatch)
    message={'id':'m','chat_id':'chat','owner':False,'text':'An ask'}
    def run(args,**kwargs):
        return SimpleNamespace(returncode=1,stderr='Mail unavailable',stdout='') if args[-1]=='email' else response(messages=[message])
    monkeypatch.setattr('cos.monitor.subprocess.run',run)
    m.tick()
    assert m.store.state('monitor_checkpoint_email')==SINCE
    assert m.store.state('monitor_checkpoint_teams')!=SINCE
    assert len(m.store.rows('SELECT * FROM jobs'))==1
    assert m.store.state('monitor_status')['state']=='needs_attention'


def test_retry_preserves_last_success_and_alert(tmp_path,monkeypatch):
    m=setup_monitor(tmp_path,monkeypatch)
    previous={'state':'needs_attention','attention_required':True,'error':'Old failure','last_poll':SINCE}
    s=m.publish_status(previous,{'email':{'state':'watching'}},0)
    assert s['attention_required']
    assert s['last_poll']==SINCE


def test_monitor_failure_is_counted_in_attention(tmp_path,monkeypatch):
    import cos.server as server
    store=Store(tmp_path/'status.db')
    monkeypatch.setattr(server,'store',store)
    monkeypatch.setattr(server,'settings',lambda:{'monitor_enabled':True})
    monkeypatch.setattr(server,'gateway_key',lambda:None)
    store.set_state('monitor_status',{'state':'scanning','attention_required':True})
    result=server.status()
    assert result['monitor_attention']
    assert result['needs_attention']==1
    store.set_state('monitor_status',{'state':'watching','attention_required':False})
    assert server.status()['needs_attention']==0
