import pytest

from cos import tools
from cos.db import Store


@pytest.fixture
def reader(tmp_path,monkeypatch):
    store=Store(tmp_path/'test.db')
    monkeypatch.setattr(tools,'DATA',tmp_path/'data')
    job=store.create_job('Read referenced Jira comment','Communications')
    calls=[]
    result={'key':'EX-123','comments':[{'id':'12345','body':'CC: @Owner'}]}
    monkeypatch.setattr(tools,'run_skill',lambda name,script,args: calls.append(args) or result)
    return lambda command,args: tools.Tools(store).execute(job,'jira_read',{'command':command,'args':args}),calls,result


@pytest.mark.parametrize('command,args',[
    ('jira-issue',['EX-123','--comments']),
    ('jira-issue',['EX-123','EX-456','--fields','summary,comment','--expand=renderedFields','--comments']),
    ('jira-issue',['12345','--comments','--raw']),
    ('jira-comments',['EX-123']),
    ('jira-comments',['EX-123','--limit','20','--order','-created']),
    ('jira-comments',['EX-123','--limit=20','--order=-updated']),
])
def test_comment_reads_are_available_to_communications(reader,command,args):
    read,calls,result=reader
    assert read(command,args)==result
    assert calls==[[command,*args]]


@pytest.mark.parametrize('command,args',[
    ('jira-issue',['EX-123','comments','12345']),
    ('jira-issue',['EX-123','comment','12345']),
    ('jira-issue',['https://example.atlassian.net/browse/EX-123']),
    ('jira-issue',[]),
    ('jira-issue',['EX-123','--fields']),
    ('jira-issue',['EX-123','--fields=']),
    ('jira-issue',['EX-123','--fields','--comments']),
    ('jira-comments',['EX-123','12345']),
    ('jira-comments',['EX-123','--comment-id','12345']),
])
def test_malformed_comment_reads_fail_locally_with_correct_syntax(reader,command,args):
    read,calls,_=reader
    with pytest.raises(ValueError,match='Use jira-issue.*--comments.*jira-comments'):
        read(command,args)
    assert calls==[]


@pytest.mark.parametrize('args',['EX-123',None,[12345]])
def test_jira_read_rejects_non_string_argument_arrays(reader,args):
    read,calls,_=reader
    with pytest.raises(ValueError,match='list of strings'):
        read('jira-issue',args)
    assert calls==[]


def test_comment_reader_does_not_enable_comment_writes(reader):
    read,calls,_=reader
    with pytest.raises(ValueError,match='Unsupported read command'):
        read('jira-comment',['EX-123','--body','Example'])
    assert calls==[]
    assert not tools.allowed_action('Communications','jira_write')


def test_catalog_explains_comment_lookup():
    assert 'command:"jira-comments"' in tools.CATALOG
    assert 'never pass "comments" or a comment ID as extra issue keys' in tools.CATALOG
