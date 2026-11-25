import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get('COS_DATA_DIR', str(ROOT / 'data'))).resolve()
DATA.mkdir(parents=True, exist_ok=True)
DEFAULTS = {
    'gateway_url': 'https://llm-api.amd.com/Unified/v1',
    'models': {'chief': 'gpt-6-luna', 'director': 'gpt-6.1-sol', 'worker': 'gpt-6-sol', 'triage': 'gpt-6-luna'},
    'model_routing': {'enabled': False, 'roles': ['worker', 'director'],
                      'router_model': 'Microsoft-Decision-1.1', 'min_confidence': 0.7,
                      'profiles': {}, 'budget_aware': True, 'budget_profiles': {}},
    'parallel_agents': 4, 'max_steps': 30, 'monitor_enabled': False,
    'queue_paused': False, 'timing_assessment_enabled': False,
    'approval_mode': 'auto_review',
    'conversation_settle_seconds': 15, 'conversation_max_wait_seconds': 60,
    'max_stalled_steps': 12,
    'capacity_recovery_seconds': 900,
    'model_parallel_requests': 1,
    # Conservative fallback, not a claim about the gateway's model capacity.
    'context_window_tokens': 128000, 'model_context_windows': {},
    'poll_seconds': 60, 'command_prefix': '!cos', 'self_chat_id': '48:notes',
    'self_email': '', 'self_name': '',
    'projects': {},
    'github_accounts': {},
    'stash_servers': {},
    'teams_auth': {},
    'engineering_workflows': {},
    'skill_roots': [str(Path.home()/'.claude/skills'), str(Path.home()/'.codex/skills')],
    'mcp_configs': [str(Path(os.environ.get('LOCALAPPDATA', '')) / 'Packages/Claude_pzs8sxrjxfjjc/LocalCache/Roaming/Claude-3p/claude_desktop_config.json')],
    'mcp_enabled': [], 'research_access': 'public', 'research_hosts': ['amd.com', 'llm.amd.com'],
}

def settings():
    result = json.loads(json.dumps(DEFAULTS))
    p = ROOT / 'config.local.json'
    if p.exists():
        saved = json.loads(p.read_text(encoding='utf-8'))
        result.update(saved)
        result['models'] = {**DEFAULTS['models'], **saved.get('models', {})}
        result['model_routing'] = {**DEFAULTS['model_routing'], **saved.get('model_routing', {})}
    return result

def save_settings(changes):
    allowed = set(DEFAULTS)
    if set(changes) - allowed:
        raise ValueError('Unknown setting')
    s = {**settings(), **changes}
    if isinstance(changes.get('model_routing'), dict):
        s['model_routing'] = {**settings()['model_routing'], **changes['model_routing']}
    if 'models' in changes:
        s['models'] = {**settings()['models'], **changes['models']} if isinstance(changes['models'], dict) else changes['models']
    from .model_routing import PROFILES
    if (not isinstance(s['models'], dict) or set(s['models']) != set(DEFAULTS['models']) or
            any(not isinstance(v, str) or not v.strip() or len(v) > 200 for v in s['models'].values())):
        raise ValueError('Assign a model ID to each Chief of Staff role.')
    s['models'] = {k: v.strip() for k, v in s['models'].items()}
    routing = s['model_routing']
    if not isinstance(routing, dict) or set(routing) - set(DEFAULTS['model_routing']):
        raise ValueError('Invalid automatic model routing settings.')
    routing = {**DEFAULTS['model_routing'], **routing}
    if type(routing['enabled']) is not bool:
        raise ValueError('Automatic model routing must be true or false.')
    if type(routing['budget_aware']) is not bool:
        raise ValueError('Budget-aware routing must be true or false.')
    if (not isinstance(routing['roles'], list) or any(not isinstance(r, str) or r not in DEFAULTS['models'] for r in routing['roles']) or
            (routing['enabled'] and not routing['roles'])):
        raise ValueError('Choose roles eligible for automatic model routing.')
    if not isinstance(routing['router_model'], str) or not routing['router_model'].strip() or len(routing['router_model']) > 200:
        raise ValueError('Choose a decision model for automatic routing.')
    routing['router_model'] = routing['router_model'].strip()
    confidence = routing['min_confidence']
    if type(confidence) not in (float, int) or not .5 <= confidence <= 1:
        raise ValueError('Routing confidence must be between 0.5 and 1.')
    if (not isinstance(routing['profiles'], dict) or set(routing['profiles']) - set(PROFILES) or
            any(not isinstance(v, str) or not v.strip() or len(v) > 200 for v in routing['profiles'].values())):
        raise ValueError('Routing profiles must map task profiles to model IDs.')
    routing['profiles'] = {k: v.strip() for k, v in routing['profiles'].items()}
    if (not isinstance(routing['budget_profiles'], dict) or set(routing['budget_profiles']) - set(PROFILES) or
            any(not isinstance(v, str) or not v.strip() or len(v) > 200 for v in routing['budget_profiles'].values())):
        raise ValueError('Budget alternatives must map task profiles to model IDs.')
    routing['budget_profiles'] = {k: v.strip() for k, v in routing['budget_profiles'].items()}
    s['model_routing'] = routing
    # Validate known capabilities without requiring a live gateway to save settings.
    from .model_catalog import snapshot, supports
    catalog = {m['id']: m for m in snapshot(s)}
    for model in [*s['models'].values(), *routing['profiles'].values(), *routing['budget_profiles'].values()]:
        if model in catalog and not supports(catalog[model], 'chat'):
            raise ValueError('The selected model does not support chat: ' + model)
    if routing['enabled'] and routing['router_model'] in catalog and not supports(catalog[routing['router_model']], 'decision'):
        raise ValueError('The routing model must support decision scoring.')
    if s['research_access'] not in ('public', 'restricted'):
        raise ValueError('Research access must be public or restricted.')
    if not isinstance(s['research_hosts'], list) or any(
            not isinstance(host, str) or not host.strip() or
            any(char in host for char in '/:@?#*') or any(char.isspace() for char in host)
            for host in s['research_hosts']):
        raise ValueError('Research hosts must be a list of exact hostnames.')
    s['research_hosts'] = sorted({host.lower().rstrip('.') for host in s['research_hosts']})
    from .engineering import validate_workflows
    validate_workflows(s['engineering_workflows'],s['projects'])
    from .github import validate_accounts
    validate_accounts(s['github_accounts'])
    from .stash import validate_servers
    validate_servers(s['stash_servers'])
    from .teams_auth import validate_config
    validate_config(s['teams_auth'])
    if s['gateway_url'] != DEFAULTS['gateway_url']:
        raise ValueError('Only the verified AMD Unified gateway is enabled')
    if not isinstance(s['self_name'],str) or len(s['self_name'])>160:
        raise ValueError('Owner display name must be text of at most 160 characters.')
    s['self_name']=s['self_name'].strip()
    s['parallel_agents'] = max(1, min(12, int(s['parallel_agents'])))
    if type(s['queue_paused']) is not bool:
        raise ValueError('Queue paused must be true or false.')
    if not isinstance(s['approval_mode'],str) or s['approval_mode'] not in {'auto_review','manual'}:
        raise ValueError('Approval mode must be auto_review or manual.')
    if type(s['timing_assessment_enabled']) is not bool:
        raise ValueError('Timing assessment enabled must be true or false.')
    s['poll_seconds'] = max(30, int(s['poll_seconds']))
    s['max_steps'] = max(5, min(100, int(s['max_steps'])))
    s['capacity_recovery_seconds'] = max(30,min(86400,int(s['capacity_recovery_seconds'])))
    s['model_parallel_requests'] = max(1,min(12,int(s['model_parallel_requests'])))
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
