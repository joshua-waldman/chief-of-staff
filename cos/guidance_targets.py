"""Exact, source-specific guidance bindings; display names never establish identity."""
import re

SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_sender_targets(
    knowledge_id TEXT PRIMARY KEY,source TEXT NOT NULL,sender_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS communication_sender_ids(
    job_id TEXT,source TEXT,message_id TEXT,sender_id TEXT NOT NULL,label TEXT NOT NULL,
    PRIMARY KEY(job_id,source,message_id));
"""


def sender_identity(source,value):
    if not isinstance(value,str):
        return ''
    value=value.strip().casefold()
    if source=='email':
        return value if len(value)<=320 and re.fullmatch(r"[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+",value) else ''
    if source=='teams':
        # Raw Teams contact identity, never imdisplayname or message text.
        value=value.rsplit('/contacts/',1)[-1]
        if re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',value):
            value='8:orgid:'+value
        return value if len(value)<=512 and re.fullmatch(r'\d+:[a-z0-9][a-z0-9:._@+-]*',value) else ''
    return ''


def validate_target(target):
    if not isinstance(target,dict):
        raise ValueError('Choose a general, conversation, or sender binding.')
    if not target:
        return {}
    source=target.get('source')
    if not isinstance(source,str) or source not in {'email','teams'}:
        raise ValueError('Choose email or teams as the binding source.')
    if set(target)=={'source','thread_id'}:
        tid=target['thread_id']
        if isinstance(tid,str) and tid.strip() and len(tid)<=4096:
            return {'source':source,'thread_id':tid.strip()}
    if set(target)=={'source','sender_id'}:
        identity=sender_identity(source,target['sender_id'])
        if identity:
            return {'source':source,'sender_id':identity}
    raise ValueError('Provide one exact conversation ID or stable sender identity; display names are not sender IDs.')


def targets(c):
    result={r['knowledge_id']:{'source':r['source'],'thread_id':r['thread_id']}
            for r in c.execute('SELECT * FROM knowledge_targets')}
    result.update({r['knowledge_id']:{'source':r['source'],'sender_id':r['sender_id']}
                   for r in c.execute('SELECT * FROM knowledge_sender_targets')})
    return result


def save_target(c,id,target):
    c.execute('DELETE FROM knowledge_targets WHERE knowledge_id=?',(id,))
    c.execute('DELETE FROM knowledge_sender_targets WHERE knowledge_id=?',(id,))
    if target:
        field='sender_id' if 'sender_id' in target else 'thread_id'
        table='knowledge_sender_targets' if field=='sender_id' else 'knowledge_targets'
        c.execute('INSERT INTO '+table+' VALUES(?,?,?)',(id,target['source'],target[field]))


def candidates(c,job_id):
    # Older email caches may still contain the exact address. Teams display
    # labels cannot be distinguished from contact IDs in old caches. Never
    # derive an identity from prose, quoted headers, or a display name.
    rows=c.execute('SELECT s.source,s.message_id,s.sender,i.sender_id,i.label\n        FROM communication_sources s LEFT JOIN communication_sender_ids i\n        ON i.job_id=s.job_id AND i.source=s.source AND i.message_id=s.message_id\n        WHERE s.job_id=? ORDER BY s.received,s.message_id',(job_id,)).fetchall()
    found={}
    for row in rows:
        identity=row['sender_id'] or (sender_identity('email',row['sender']) if row['source']=='email' else '')
        if identity:
            found[(row['source'],identity)]={'source':row['source'],'sender_id':identity,
                'label':row['label'] or row['sender'] or identity}
    return sorted(found.values(),key=lambda r:(r['source'],r['sender_id']))


def applies(target,conversations=(),senders=()):
    if not target:
        return True
    if 'sender_id' in target:
        return (target['source'],target['sender_id']) in senders
    return (target['source'],target['thread_id']) in conversations
