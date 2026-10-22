import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get('COS_DATA_DIR', str(ROOT / 'data'))).resolve()
DATA.mkdir(parents=True, exist_ok=True)
DEFAULTS = {
    'gateway_url': 'https://llm-api.amd.com/Unified/v1',
    'models': {'chief': 'gpt-6-luna', 'director': 'gpt-6.1-sol', 'worker': 'gpt-6-sol', 'triage': 'gpt-6-luna'},
    'parallel_agents': 4, 'max_steps': 30, 'monitor_enabled': False,
    'queue_paused': False,
    'conversation_settle_seconds': 15, 'conversation_max_wait_seconds': 60,
    'max_stalled_steps': 12,
    # Conservative fallback, not a claim about the gateway's model capacity.
    'context_window_tokens': 128000, 'model_context_windows': {},
    'poll_seconds': 60, 'command_prefix': '!cos', 'self_chat_id': '48:notes',
    'self_email': '',
    'projects': {},
    'skill_roots': [str(Path.home()/'.claude/skills'), str(Path.home()/'.codex/skills')],
    'mcp_configs': [str(Path(os.environ.get('LOCALAPPDATA', '')) / 'Packages/Claude_pzs8sxrjxfjjc/LocalCache/Roaming/Claude-3p/claude_desktop_config.json')],
    'mcp_enabled': [], 'research_hosts': ['amd.com', 'llm.amd.com'],
}

def settings():
    result = json.loads(json.dumps(DEFAULTS))
    p = ROOT / 'config.local.json'
    if p.exists():
        saved = json.loads(p.read_text(encoding='utf-8'))
        result.update(saved)
        result['models'] = {**DEFAULTS['models'], **saved.get('models', {})}
    return result

def save_settings(changes):
    allowed = set(DEFAULTS)
    if set(changes) - allowed:
        raise ValueError('Unknown setting')
    s = {**settings(), **changes}
    if s['gateway_url'] != DEFAULTS['gateway_url']:
        raise ValueError('Only the verified AMD Unified gateway is enabled')
    s['parallel_agents'] = max(1, min(12, int(s['parallel_agents'])))
    if type(s['queue_paused']) is not bool:
        raise ValueError('Queue paused must be true or false.')
    s['poll_seconds'] = max(30, int(s['poll_seconds']))
    s['max_steps'] = max(5, min(100, int(s['max_steps'])))
    s['conversation_settle_seconds']=max(0,min(120,int(s['conversation_settle_seconds'])))
    s['conversation_max_wait_seconds']=max(s['conversation_settle_seconds'],min(300,int(s['conversation_max_wait_seconds'])))
    s['max_stalled_steps']=max(4,min(100,int(s['max_stalled_steps'])))
    if type(s['context_window_tokens']) is not int or s['context_window_tokens'] < 16384:
        raise ValueError('Context window must be an integer of at least 16384 tokens.')
    if not isinstance(s['model_context_windows'],dict) or any(not isinstance(k,str) or type(v) is not int or v<16384 for k,v in s['model_context_windows'].items()):
        raise ValueError('Model context windows must map model IDs to integer token limits of at least 16384.')
    if not s['command_prefix'].strip():
        raise ValueError('Teams command prefix cannot be empty')
    p = ROOT / 'config.local.json'
    temp = p.with_suffix('.tmp')
    temp.write_text(json.dumps(s, indent=2), encoding='utf-8')
    temp.replace(p)
    return s

def gateway_key():
    key = os.environ.get('LLM_GATEWAY_KEY', '')
    if not key:
        try:
            import keyring
            key = keyring.get_password('AMD-ChiefOfStaff', 'gateway') or ''
        except Exception:
            pass
    return key

def store_key(value):
    import keyring
    if not value.strip():
        raise ValueError('Key cannot be empty')
    if 'plaintext' in type(keyring.get_keyring()).__name__.lower():
        raise ValueError('A secure operating-system credential store is required')
    keyring.set_password('AMD-ChiefOfStaff', 'gateway', value.strip())
