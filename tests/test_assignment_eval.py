"""Opt-in behavioral regression: COS_RUN_MODEL_EVALS=1 python -m pytest -q tests/test_assignment_eval.py.

Uses the configured AMD triage model; no source reads or proposed tools execute.
"""
import json
import os

import pytest

from cos.conversations import save_source
from cos.db import Store
from cos.engine import SCHEMA
from cos.message_triage import first_pass
from cos.provider import Gateway
from cos.teams_content import text


pytestmark = pytest.mark.skipif(os.environ.get('COS_RUN_MODEL_EVALS') != '1',
                                reason='Opt-in live AMD model evaluation')

@pytest.mark.parametrize('body,action,response', [
    ('<p><span itemtype="http://schema.skype.com/Mention" itemid="0">Greer, Brian</span>'
     '&nbsp;assigned AVA-15121 to you, can you help validating this please. Thank You.</p>', 'no', 'no'),
    ('<p><at id="0">Greer, Brian</at> please validate AVA-15121 today. P2, release blocked.</p>', 'no', 'no'),
    ('<p>FYI Joshua: Poonam wrote to Brian: "Assigned AVA-15121 to you, can you help validating this please."</p>', 'no', 'no'),
    ('<p><at id="0">Joshua Waldman</at> Brian owns AVA-15121, but please independently validate the SQL test coverage.</p>', 'yes', 'no'),
    ('<p>Joshua, <at id="0">Greer, Brian</at> assigned the ticket to you. Please validate the SQL coverage.</p>', 'yes', 'no'),
    ('<p><at id="0">Joshua Waldman</at> can you reply with which release needs the fix? No validation work requested.</p>', 'no', 'yes'),
])
def test_named_request_is_attributed_to_original_addressee(tmp_path, monkeypatch, body, action, response):
    from cos import config
    real_settings = config.settings()
    monkeypatch.setattr(config, 'settings', lambda: {**real_settings, 'self_email': 'joshua.waldman@example.com'})
    store = Store(tmp_path/'eval.db')
    job = store.create_job('Triage this incoming Teams conversation.', 'Communications')
    with store.connect() as c:
        save_source(c, job['id'], 'teams', 'message', 'Luthra, Poonam', '', '', body, True)
    messages = first_pass(store, job, {'messages': []})
    # Include both representations actually used by ingestion and source reads.
    messages[-1]['content'] = json.dumps({**json.loads(messages[-1]['content']),
                                         'normalized_message': text(body)})
    answer, _ = Gateway(store).complete(messages, role='triage', tools=SCHEMA,
                                         timeout=60, attempts=1, max_tokens=2500)
    call = json.loads(answer['tool_calls'][0]['function']['arguments'])
    assert call['action'] == 'update_brief', answer
    args = call['arguments']
    if isinstance(args, str):
        args = json.loads(args)
    assert args['action_needed'] == action, args
    assert args['response_needed'] == response, args
    if action == response == 'no':
        assert args.get('time_sensitive', 'no') != 'yes', args
