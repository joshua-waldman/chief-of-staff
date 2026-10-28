"""Keep synthetic tests independent of the owner's local app settings."""
import pytest
import json
from cos import config
# Bind static asset paths before individual tests isolate configuration roots.
from cos import server  # noqa: F401


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path,monkeypatch):
    monkeypatch.setattr(config,'ROOT',tmp_path)
    monkeypatch.setitem(config.DEFAULTS,'self_email','owner@example.com')


@pytest.fixture(autouse=True)
def synthetic_teams_router(monkeypatch):
    """Existing workflow tests use related messages; never call a live gateway."""
    from cos import teams_topics
    class Provider:
        def __init__(self, store):
            pass

        def complete(self, messages, **kwargs):
            payload = json.loads(messages[-1]['content'])
            topic = next(iter(payload['existing_topics']), {'topic_id': 'new', 'title': 'Conversation topic'})
            return {'content': json.dumps({'topics': [{
                'topic_id': topic['topic_id'], 'title': topic['title'],
                'message_ids': [str(m['id']) for m in payload['messages']],
            }]})}, {}
    monkeypatch.setattr(teams_topics, 'Gateway', Provider)
