import json
from fastapi.testclient import TestClient
from cos import server
from cos.db import Store


def test_org_agents_include_full_roster_and_cross_org_delegation(tmp_path, monkeypatch):
    store = Store(tmp_path / 'agents.db')
    monkeypatch.setattr(server, 'store', store)
    parent = store.create_job('Investigate a request', 'Communications')
    child = store.create_job('Research the issue', 'Technology', parent_id=parent['id'])
    store.update(child['id'], status='running', messages='private model context')
    store.write('INSERT INTO job_briefs VALUES(?,?,?)', (child['id'], json.dumps({
        'status_summary': 'Comparing sources', 'next_step': 'Write findings',
        'proposed_body': 'Private draft',
    }), child['updated']))
    finished = store.create_job('Previous work', 'Technology')
    store.update(finished['id'], status='completed')
    # The org roster must not inherit the job tracker's 1,000-job limit.
    with store.connect() as connection:
        connection.executemany(
            'INSERT INTO jobs(id,title,org,status,created,updated) VALUES(?,?,?,?,?,?)',
            [(str(i), 'Task', 'Engineering', 'queued', '2099', '2099') for i in range(1001)],
        )
    client = TestClient(server.app)
    assert client.get('/api/organization-agents').status_code == 401
    client.get('/')
    response = client.get('/api/organization-agents')
    assert response.status_code == 200
    roster = {agent['id']: agent for agent in response.json()}
    assert len(roster) == 1004
    agent = roster[child['id']]
    assert agent['org'] == 'Technology'
    assert agent['parent_org'] == 'Communications'
    assert agent['parent_title'] == parent['title']
    assert agent['parent_id'] == parent['id']
    assert agent['status_summary'] == 'Comparing sources'
    assert agent['next_step'] == 'Write findings'
    assert roster[finished['id']]['status'] == 'completed'
    assert roster[parent['id']]['status_summary'] == ''
    assert not {'messages', 'request', 'brief_body', 'proposed_body'} & agent.keys()
