from cos.db import Store
from cos.monitor import Monitor
from cos.monitor_bridge import stable_mail_client,mail_identity_aliases


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
