"""Owner feedback resumes the same job and atomically retires stale approvals."""
import hashlib
import json
from .db import uid,now
from .knowledge import clean

REVIEW_POLICY = "Owner review messages are genuine user direction. Historical application-added disclaimers about 'not send approval' refer ONLY to email and Teams sending; they do not cancel the user's instructions to perform routine Jira work. Routine Jira comments, creates and permitted edits are already authorized under the user's policy. Do not request a magic approval phrase or repeated consent for those operations. Communications must delegate Jira writes to Engineering or Strategy, passing the exact issue key and requested comment/changes and the owner's instruction. Communications delegation requires its own exact-action approval before child jobs start, even for routine work requested by the owner. Propose it with delegate rather than asking for permission in chat. Call the actual tool and verify its result before reporting success. Email/Teams sends, Confluence writes and other approval-gated operations still require their dedicated exact-action approval buttons. Use the action tool to create that approval rather than repeatedly asking in chat. Discussing or revising an email never bypasses its send approval. Never repeat a previously executed send."

def submit(engine,id,message,remember=False,guidance='',applies_to='',draft=None):
    message=clean(message,8000)
    if not message:
        raise ValueError('Enter direction or a question for your Chief of Staff.')
    if type(remember) is not bool:
        raise ValueError('Choose whether to remember guidance.')
    if draft is not None and (not isinstance(draft,str) or len(draft)>30000):
        raise ValueError('The edited draft must be text under 30,000 characters.')
    content=''
    if remember:
        guidance=clean(guidance or message,3000)
        applies_to=clean(applies_to,500)
        if not applies_to or not guidance:
            raise ValueError('Describe which similar emails this guidance applies to.')
        content='Applies to: '+applies_to+'\nUser guidance: '+guidance
    store=engine.store
    stamp=now()
    with store.lock,store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT * FROM jobs WHERE id=?',(id,)).fetchone()
        if not row or row['org']!='Communications':
            raise ValueError('Choose a Communications job.')
        job=dict(row)
        if job['status'] not in {'awaiting_approval','waiting_input','blocked','failed','completed'}:
            raise ValueError('Wait for the current agent step to finish before sending more direction.')
        if c.execute("SELECT 1 FROM approvals WHERE job_id=? AND status IN ('executing','uncertain')",(id,)).fetchone():
            raise ValueError('Resolve the in-flight or uncertain action before revising this job.')
        raw=json.loads(job['messages'])
        s=raw if isinstance(raw,dict) else {'messages':raw}
        messages=s['messages']
        superseded=c.execute("UPDATE approvals SET status='superseded' WHERE job_id=? AND status='pending'",(id,)).rowcount
        if s.get('waiting_call'):
            engine.add_result(messages,s.pop('waiting_call'),{'not_executed':True,'review_requested':True,'instruction':'The owner requested discussion or revision. The previous pending action is superseded. Do not send it.'})
        # finish stops the engine without a tool result; balance that call when reopening.
        if messages and messages[-1].get('tool_calls'):
            call=messages[-1]['tool_calls'][0]
            action=json.loads(call['function']['arguments']).get('action')
            if action=='finish':
                engine.add_result(messages,call['id'],{'completed':True,'reopened_for_review':True})
        s.pop('waiting_action',None)
        instruction='Owner review feedback: '+message
        if draft is not None:
            instruction+='\nCurrent unsent draft edited by the owner (revise as directed):\n'+draft
        # Application policy belongs in system context, never appended as conflicting user text.
        if remember:
            instruction+='\nThe owner explicitly saved this guidance for future similar communications: '+content
        if messages:
            messages.append({'role':'user','content':instruction})
        else:
            s['followup']=instruction
        c.execute("UPDATE jobs SET messages=?,status='queued',question='',result='',step=0,updated=? WHERE id=?",(json.dumps(s),stamp,id))
        c.execute('INSERT INTO review_messages(job_id,role,body,created) VALUES(?,?,?,?)',(id,'user',message,stamp))
        c.execute('INSERT INTO events(job_id,kind,body,created) VALUES(?,?,?,?)',(id,'review','Owner requested discussion/revision. '+str(superseded)+' pending action(s) superseded.',stamp))
        old=c.execute('SELECT body FROM job_briefs WHERE job_id=?',(id,)).fetchone()
        brief=json.loads(old['body']) if old else {}
        brief.update(status_summary='Revising based on your direction',next_step='The Chief of Staff will return an answer or a revised draft for review.',proposed_body='')
        c.execute('INSERT OR REPLACE INTO job_briefs VALUES(?,?,?)',(id,json.dumps(brief),stamp))
        memory_id=None
        if remember:
            memory_id=uid()
            title='Email handling: '+applies_to[:130]
            fingerprint=hashlib.sha256((id+'preferenceCommunications'+title+content).encode()).hexdigest()
            c.execute("INSERT INTO knowledge VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(fingerprint) DO UPDATE SET title=excluded.title,content=excluded.content,status='active',source='user',updated=excluded.updated",(memory_id,'preference',title,content,'Communications','active','user',id,stamp,stamp,0,0,fingerprint))
            memory_id=c.execute('SELECT id FROM knowledge WHERE fingerprint=?',(fingerprint,)).fetchone()['id']
            c.execute('INSERT INTO review_messages(job_id,role,body,created) VALUES(?,?,?,?)',(id,'notice','Remembered for '+applies_to+': '+(guidance or message),stamp))
    from .trace import emit,fingerprint
    emit('review.accepted',{'superseded_actions':superseded,'memory_id':memory_id,'feedback':fingerprint(message)},store=store,job_id=id)
    return {'ok':True,'superseded':superseded,'memory_id':memory_id}

def reply(store,id,text):
    with store.lock,store.connect() as c:
        last=c.execute("SELECT role FROM review_messages WHERE job_id=? AND role<>'notice' ORDER BY id DESC LIMIT 1",(id,)).fetchone()
        if last and last['role']=='user':
            c.execute('INSERT INTO review_messages(job_id,role,body,created) VALUES(?,?,?,?)',(id,'assistant',str(text)[:8000],now()))
