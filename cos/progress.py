"""Separate scheduler time slices from a persisted no-progress safeguard."""
import hashlib
import json

LEGACY_STEP_QUESTION='The agent reached its step budget. Review its progress and provide instructions to continue.'


def reset(state):
    for key in ('progress_seen','stalled_steps','progress_warning','last_progress_action','decision_fingerprint'):
        state.pop(key,None)


def stable(value):
    if isinstance(value,dict):
        return {k:stable(v) for k,v in value.items() if k not in {'uses','helpful','retrieved_at','fetched_at'}}
    if isinstance(value,list):
        return [stable(v) for v in value]
    return value


def observe(state,action,args,result,*,error=False):
    """New evidence, file pages and actual changes count; cosmetic brief edits do not."""
    state['last_progress_action']=action
    if action=='update_brief' and not error:
        decision={k:result.get(k) for k in ('response_needed','action_needed','action_plan','proposed_body')}
        digest=hashlib.sha256(json.dumps(decision,sort_keys=True).encode()).hexdigest()
        fresh=state.get('decision_fingerprint')!=digest
        state['decision_fingerprint']=digest
    else:
        digest=hashlib.sha256(json.dumps([action,args,stable(result)],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        seen=state.setdefault('progress_seen',[])
        fresh=digest not in seen and not error
        if digest not in seen:
            seen.append(digest)
            del seen[:-256]
    state['stalled_steps']=0 if fresh else state.get('stalled_steps',0)+1
    if fresh:
        state.pop('progress_warning',None)


def warning(state):
    return ('Progress check: repeated calls are returning unchanged evidence or errors. '
        'Use the saved brief and verified findings. Do not reread unchanged screenshots, '
        'attachments, or conversations. Resolve the specific missing fact with a different '
        'approach, ask a concrete question if genuinely blocked, or update the brief and '
        'finish when no reply or unresolved work remains. Never bypass action approvals.')
