"""Separate scheduler time slices from a persisted no-progress safeguard."""
import hashlib
import json
import re
from pathlib import PureWindowsPath


def recovery_path(path):
    """Reserved recovery directories are bookkeeping, never new task evidence."""
    parts=tuple(p.casefold() for p in PureWindowsPath(str(path)).parts)
    return bool(parts) and (parts[0]=='context' or any(parts[i:i+2]==('workspace','context') for i in range(len(parts)-1)))


def bookkeeping_read(path):
    """Saved research is primary evidence even in the legacy context directory."""
    return recovery_path(path) and not re.fullmatch(r'research-[0-9a-f]{20}\.txt',PureWindowsPath(str(path)).name.casefold())


def hydrate_reads(store,job_id,state):
    """Recover legacy read coverage without changing progress or replaying tools."""
    for row in store.rows("SELECT rowid AS cursor,args,result FROM approvals WHERE job_id=? AND tool='read_file' AND status='executed' AND rowid>? ORDER BY rowid",(job_id,state.get('file_read_cursor',0))):
        args=json.loads(row['args'])
        result=json.loads(row['result'])
        if not bookkeeping_read(args.get('path','')) and isinstance(result,dict) and result.get('sha256') and isinstance(result.get('text'),str):
            preserved=('stalled_steps','reads_since_brief','last_progress_action','progress_warning','stall_recovery')
            saved=json.loads(json.dumps({k:state[k] for k in preserved if k in state}))
            observe(state,'read_file',args,result)
            for key in preserved:
                state.pop(key,None)
            state.update(saved)
        state['file_read_cursor']=row['cursor']

LEGACY_STEP_QUESTION='The agent reached its step budget. Review its progress and provide instructions to continue.'


def reset(state):
    for key in ('progress_seen','stalled_steps','progress_warning','last_progress_action','decision_fingerprint','reads_since_brief','stall_recovery'):
        state.pop(key,None)


def saved_findings(store,job_id):
    """Carry bounded source notes through compaction instead of rereading archives.

    These are previous observations, not instructions or execution authority.
    Read the successful brief arguments, since the current brief overwrites them.
    """
    rows=store.rows("SELECT args FROM approvals WHERE job_id=? AND tool='update_brief' AND status='executed' ORDER BY rowid DESC LIMIT 8",(job_id,))
    notes=[]
    for row in reversed(rows):
        args=json.loads(row['args'])
        note={key:args[key][:size] for key,size in (('context',1200),('status_summary',600),('next_step',200)) if isinstance(args.get(key),str)}
        if note and note not in notes:
            notes.append(note)
        while len(json.dumps(notes))>12000:
            notes.pop(0)
    return notes


def recovery_step(state,limit):
    """One persisted replan per source/owner revision, with at most six actions."""
    attempt=state.get('stall_recovery')
    if attempt and attempt['active']:
        if attempt['remaining']<=0:
            return 'exhausted'
        return 'recovering'
    if state.get('stalled_steps',0)<limit:
        return 'continue'
    if attempt:
        return 'exhausted'
    state['stall_recovery']={'active':True,'remaining':6}
    return 'started'


def recovery_instruction():
    return ('Automatic progress recovery: stop the reading loop and replan from the owner request, '
        'saved source findings, latest brief and read coverage. There are at most six recovery actions. '
        'Synthesize and write the requested deliverable, then finish only if the task is actually complete. '
        'Do not restart completed sources or reconstruct the job from recovery archives. '
        'If a precise fact is missing, verify only that source range. If access genuinely fails, '
        'ask a specific question identifying the missing evidence and failure. '
        'Never fabricate evidence, completion or approvals; all existing action gates still apply.')


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
        state['reads_since_brief']=0
        decision={k:result.get(k) for k in ('response_needed','action_needed','action_plan','proposed_body')}
        digest=hashlib.sha256(json.dumps(decision,sort_keys=True).encode()).hexdigest()
        fresh=state.get('decision_fingerprint')!=digest
        state['decision_fingerprint']=digest
    elif (action=='read_action_receipt' or
          (action=='read_file' and bookkeeping_read(result.get('source',args.get('path','')) if isinstance(result,dict) else args.get('path',''))) or
          (action=='list_files' and recovery_path(args.get('path',''))) or
          (action=='list_files' and isinstance(result,list) and result and all(isinstance(f,dict) and recovery_path(f.get('path','')) for f in result))):
        # New checkpoint names and changing page sizes are not task progress.
        fresh=False
    elif action=='read_file' and not error and isinstance(result,dict) and result.get('sha256') and isinstance(result.get('text'),str):
        source=result.get('source',args.get('path',''))
        key=hashlib.sha256(json.dumps([source,result['sha256']]).encode()).hexdigest()
        reads=state.setdefault('file_reads',{})
        entry=reads.setdefault(key,{'source':source,'sha256':result['sha256'],'ranges':[]})
        start=args.get('offset',0)
        end=start+len(result['text'])
        ranges=entry['ranges']
        covered=sum(max(0,min(end,b)-max(start,a)) for a,b in ranges)
        fresh=end-start>covered
        merged=[]
        for a,b in sorted(ranges+[[start,end]]):
            if merged and a<=merged[-1][1]:
                merged[-1][1]=max(merged[-1][1],b)
            else:
                merged.append([a,b])
        entry['ranges']=merged
        if not result.get('truncated',False):
            entry['characters']=end
        entry['next_offset']=next((b for a,b in merged if a==0),0)
        entry['complete']=entry.get('characters')==entry['next_offset']
        # Refresh recency without forgetting coverage within this job.
        reads[key]=reads.pop(key)
        if fresh:
            state['reads_since_brief']=state.get('reads_since_brief',0)+1
    else:
        evidence={'url':result.get('url',result.get('source')), 'text':result.get('text')} if action in {'fetch_url','browser_read'} and isinstance(result,dict) and not error else stable(result)
        digest=hashlib.sha256(json.dumps([action,args,evidence],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        seen=state.setdefault('progress_seen',[])
        fresh=digest not in seen and not error
        if digest not in seen:
            seen.append(digest)
            del seen[:-256]
    state['stalled_steps']=0 if fresh else state.get('stalled_steps',0)+1
    if fresh:
        state.pop('progress_warning',None)
        if action!='update_brief' and state.get('stall_recovery',{}).get('active'):
            state['stall_recovery']['active']=False


def read_guidance(state):
    return {'recent_files':[{**entry,'ranges':entry['ranges'][:8],'range_count':len(entry['ranges'])} for entry in list(state.get('file_reads',{}).values())[-8:]],
            'reads_since_brief':state.get('reads_since_brief',0),
            'instruction':'Read ranges describe coverage, not retained understanding. Save findings and source offsets with update_brief after each batch. Continue from next_offset for incomplete files. Reuse saved findings for completed sources; reread a specific range only when a fact needs verification. Recovery archives are bookkeeping and do not count as new evidence. Delegate independent analysis with a scoped request and explicit file_ids; children have separate context.'}


def warning(state):
    return ('Progress check: repeated calls are returning unchanged evidence or errors. '
        'Use the saved brief and verified findings. Do not reread unchanged screenshots, '
        'attachments, or conversations. Resolve the specific missing fact with a different '
        'approach. For staged implementation, use edit_file with the current sha256 and '
        'one exact replacement rather than rereading whole files. Save concrete findings, '
        'ask a concrete question if genuinely blocked, or update the brief and '
        'finish when no reply or unresolved work remains. Never bypass action approvals.')
