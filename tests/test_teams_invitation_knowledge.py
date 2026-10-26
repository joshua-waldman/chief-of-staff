import json

import pytest

from cos.db import Store
from cos.knowledge import Knowledge, clean


def invitation(link='https://teams.microsoft.com/meet/123456789?p=example', separator=' '):
    return separator.join([
        'Microsoft Teams meeting', 'Join: '+link,
        'Meeting ID: 123 456 789', 'Passcode: Example123',
        'Need help? | System reference',
    ])


@pytest.mark.parametrize('link', [
    'https://teams.microsoft.com/meet/123456789?p=example',
    'https://teams.microsoft.com/l/meetup-join/example',
    '[https://teams.microsoft.com/meet/123456789?p=example](https://teams.microsoft.com/meet/123456789?p=example)',
])
@pytest.mark.parametrize('separator', [' ', '\n', '\r\n'])
def test_teams_invitation_passcode_is_allowed(link, separator):
    text=invitation(link,separator)
    assert clean(text)==text


def test_invitation_context_search_and_save(tmp_path):
    store=Store(tmp_path/'knowledge.sqlite3')
    knowledge=Knowledge(store)
    entry=knowledge.save({'title':'Knowledge base sync',
        'content':'Review the knowledge base sync agenda.'},user=True)
    request='Knowledge base sync\n'+invitation()+'\n'+invitation()
    job=store.create_job(request,'Communications')
    assert entry['id'] in {row['id'] for row in json.loads(knowledge.context(job))}
    saved=knowledge.save({'title':'Meeting details','content':invitation()},user=True)
    assert saved['content']==invitation()


@pytest.mark.parametrize('secret', [
    'password=hunter2', 'api_key=example-secret',
    'access_token=example-secret', 'client_secret=example-secret',
    'refresh_token=example-secret', 'Bearer abcdefghijklmnopqrstuvwxyz',
    'sk-abcdefghijklmnopqrstuvwxyz', 'Passcode: unrelated-account-code',
])
@pytest.mark.parametrize('position', ['before', 'between', 'after'])
def test_invitation_does_not_exempt_other_credentials(secret, position):
    text=invitation()
    if position=='before':
        text=secret+'\n'+text
    elif position=='between':
        text=text.replace('Meeting ID:',secret+'\nMeeting ID:')
    else:
        text+='\n'+secret
    with pytest.raises(ValueError,match='Credentials must not be stored'):
        clean(text)


@pytest.mark.parametrize('text', [
    'Passcode: example-account-code',
    'Microsoft Teams meeting. Passcode: example-account-code',
    invitation('https://teams.microsoft.com.evil.example/meet/123'),
    invitation('https://example.com/meet/123'),
    invitation().replace('Meeting ID: 123 456 789 ',''),
])
def test_passcode_exception_requires_teams_invitation_structure(text):
    with pytest.raises(ValueError,match='Credentials must not be stored'):
        clean(text)
