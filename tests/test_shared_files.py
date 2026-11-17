import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cos import integrations,shared_files


def test_shared_file_sources_exclude_unrelated_hosts_and_metadata_roots():
    content='''<a href="https://amd.sharepoint.com/:b:/g/guide?x=1&amp;y=2">Guide</a>
      https://attacker.example/guide.pdf https://amd.sharepoint.com/personal/person/
      https://amd.sharepoint.com/_api/transcripts/content'''
    assert shared_files.source_urls({'content':content})==['https://amd.sharepoint.com/:b:/g/guide?x=1&y=2']
    for url in ['http://amd.sharepoint.com/file','https://amd.sharepoint.com.evil.example/file',
                'https://user:password@amd.sharepoint.com/file','https://amd.sharepoint.com:444/file']:
        assert not shared_files.sharepoint_url(url)


def test_downloads_are_bounded_and_never_use_display_name_as_path(tmp_path,monkeypatch):
    calls=[]
    class Opener:
        def open(self,url,timeout):
            calls.append(url)
            return io.BytesIO(b'%PDF-synthetic')
    monkeypatch.setattr(shared_files.urllib.request,'build_opener',lambda *args:Opener())
    item={'id':'file','name':'../../outside.pdf','size':10,
          '@microsoft.graph.downloadUrl':'https://amd.sharepoint.com/download'}
    result=shared_files.download_item(item,tmp_path,None)
    path=Path(result['path'])
    assert path.parent==tmp_path and path.suffix=='.pdf'
    assert path.read_bytes()==b'%PDF-synthetic'
    assert shared_files.download_item({**item,'size':shared_files.MAX_FILE+1},tmp_path,None)['status']=='too_large'
    assert shared_files.download_item({**item,'name':'recording.mp4'},tmp_path,None)['status']=='metadata_only'
    assert shared_files.download_item({**item,'@microsoft.graph.downloadUrl':'https://evil.example/file'},tmp_path,None)['status']=='download_unavailable'
    assert len(calls)==1


def test_actual_size_and_redirect_checks(tmp_path,monkeypatch):
    class Opener:
        def open(self,*args,**kwargs):return io.BytesIO(b'x'*(shared_files.MAX_FILE+1))
    monkeypatch.setattr(shared_files.urllib.request,'build_opener',lambda *args:Opener())
    item={'id':'file','name':'file.txt','@microsoft.graph.downloadUrl':'https://amd.sharepoint.com/file'}
    assert shared_files.download_item(item,tmp_path,None)['status']=='too_large'
    assert not list(tmp_path.iterdir())
    with pytest.raises(ValueError,match='outside SharePoint'):
        shared_files.DownloadRedirect().redirect_request(None,None,302,'',{},'https://evil.example/file')


def test_source_aliases_resolve_once_and_failures_remain_visible(tmp_path,monkeypatch):
    monkeypatch.setattr(integrations,'skill_dir',lambda name:tmp_path)
    item={'id':'guide','name':'setup.pdf','webUrl':'https://amd.sharepoint.com/guide.pdf'}
    monkeypatch.setitem(sys.modules,'graph_client',SimpleNamespace(_ssl_context=lambda:None))
    monkeypatch.setitem(sys.modules,'meeting_artifacts',SimpleNamespace(resolve=lambda graph,url:None if 'missing' in url else item))
    calls=[]
    def download(item,target,context):
        calls.append(item['id']);path=target/'guide.pdf';path.write_bytes(b'%PDF')
        return {'status':'downloaded','path':str(path)}
    monkeypatch.setattr(shared_files,'download_item',download)
    result=shared_files.collect(object(),[{'id':'source','content':' '.join(
        'https://amd.sharepoint.com/'+x for x in ['share-one','share-two','missing'])}],tmp_path)
    assert calls==['guide']
    assert [r['status'] for r in result['artifacts']]==['downloaded','forbidden_or_missing']


def test_meeting_documents_are_registered_for_reading_without_transcript_authority(tmp_path,monkeypatch):
    from cos import tools
    from cos.db import Store
    monkeypatch.setattr(tools,'DATA',tmp_path/'data')
    store=Store(tmp_path/'meeting-files.db')
    job=store.create_job('Review meeting attachments','Communications')
    def bridge(action,args):
        assert action=='meeting_files'
        target=Path(args['target']);target.mkdir(parents=True)
        path=target/'guide.txt';path.write_text('Actual setup guide',encoding='utf-8')
        return {'artifacts':[{'source':'https://amd.sharepoint.com/guide','name':'Guide.txt',
            'path':str(path),'status':'downloaded'}],'scan_incomplete':True}
    monkeypatch.setattr(tools,'communication',bridge)
    executor=tools.Tools(store)
    result=executor.execute(job,'meeting_artifacts',{'chat_id':'meeting'})
    assert result['scan_incomplete']
    file=result['artifacts'][0]
    assert file['name']=='Guide.txt'
    assert executor.execute(job,'read_file',{'path':file['file_id']})['text']=='Actual setup guide'
    assert not store.state('meeting_sources:'+job['id'],{})
    unrelated=store.create_job('Other job','Technology')
    with pytest.raises(ValueError,match='not attached'):
        executor.execute(unrelated,'read_file',{'path':file['url']})
