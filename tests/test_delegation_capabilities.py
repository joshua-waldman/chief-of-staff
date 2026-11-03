"""Capabilities must work in restricted readers without granting side effects."""
import json
import pytest
from cos.engine import Engine, session
from cos.tools import allowed_job_action, Tools
from cos.policy import classify
from test_context import env


def test_restricted_web_reader_can_reach_host_validation(env,monkeypatch):
    parent=env.create_job('Verify primary evidence','Technology')
    engine=Engine(env)
    engine.delegate(parent,{'tasks':[{'org':'Technology','request':'Read a primary page',
        'source_only':True,'required_tools':['fetch_url','browser_read']}]})
    child=env.one('SELECT * FROM jobs WHERE parent_id=?',(parent['id'],))
    assert session(child)['source_only'] and session(child)['research_only']
    for action in ('fetch_url','browser_read','jira_read','repository_read'):
        assert allowed_job_action(child,action)
    for action in ('delegate','send_email','send_teams','browser_action','jira_write','mcp_call','run_command','apply_changes'):
        assert not allowed_job_action(child,action)
    from cos import research
    def denied(url):
        raise ValueError('Add this exact research host in Connections before retrieval.')
    monkeypatch.setattr(research,'fetch_url',denied)
    with pytest.raises(ValueError,match='host in Connections'):
        engine.tools.execute(child,'fetch_url',{'url':'https://research.example/page'})
    assert classify('browser_action',{},research_only=True).approval


@pytest.mark.parametrize('required',[None,'fetch_url',[{}],['fetch_url']*33])
def test_invalid_capabilities_do_not_create_children(env,required):
    parent=env.create_job('Research','Technology')
    with pytest.raises(ValueError,match='required_tools'):
        Engine(env).delegate(parent,{'tasks':[{'org':'Technology','request':'Read','required_tools':required}]})
    assert not env.rows('SELECT id FROM jobs WHERE parent_id=?',(parent['id'],))


def test_entire_batch_preflight_rejects_unavailable_or_write_capabilities(env):
    parent=env.create_job('Research','Technology')
    for task in ({'org':'Communications','request':'Read web','source_only':True,'required_tools':['fetch_url']},
                 {'org':'Technology','request':'Interact','source_only':True,'required_tools':['browser_action']},
                 {'org':'Technology','request':'Read','required_tools':['unknown_tool']}):
        with pytest.raises(ValueError,match='cannot use required tools'):
            Engine(env).delegate(parent,{'tasks':[{'org':'Technology','request':'Valid first'},task]})
        assert not env.rows('SELECT id FROM jobs WHERE parent_id=?',(parent['id'],))


def test_legacy_source_session_and_normal_delegation_remain_supported(env):
    job=env.create_job('Existing reader','Technology')
    env.update(job['id'],messages=json.dumps({'messages':[],'source_only':True,'research_only':True}))
    assert allowed_job_action(env.job(job['id']),'fetch_url')
    parent=env.create_job('Research','Technology')
    Engine(env).delegate(parent,{'tasks':[{'org':'Technology','request':'Read sources'}]})
    assert env.one('SELECT id FROM jobs WHERE parent_id=?',(parent['id'],))
