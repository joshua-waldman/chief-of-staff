"""Owner self-chat commitments; deliberately separate from transcript proposals."""
import hashlib
import json
import re

from .commitments import Commitments, digest, fields, history, normalized
from .db import now, uid
from .config import settings
from .knowledge import clean, words

STOP = {'the','and','for','with','from','this','that','your','please','commitment','reminder','remind','create','record','track','about','would','could','should','will','need','want','ask','when','then','there','have','into','some','what','which','where','after','before','meeting'}


def owner_source(store, job, ref):
    # Re-read durable server state. A model-provided job dictionary or auth flag is never evidence.
    root=store.job(job['id'])
    if not root or root['parent_id'] or root['org']!=job['org']:
        raise ValueError('Only the authenticated owner root self-chat can create an owner commitment.')
    state=json.loads(root['messages'])
    if (isinstance(state,dict) and any(state.get(x) for x in ('source_only','historical_import','research_only'))
            or store.one('SELECT 1 FROM communication_threads WHERE job_id=?',(root['id'],))
            or store.one('SELECT 1 FROM communication_sources WHERE job_id=?',(root['id'],))):
        raise ValueError('Source-bound or restricted jobs cannot authorize owner commitments.')
    if ref=='request':
        marker=store.state('owner_self_chat:'+root['id'],{})
        if marker.get('request_sha256')!=hashlib.sha256(root['request'].encode('utf-8')).hexdigest() or marker.get('created')!=root['created']:
            raise ValueError('This request lacks server-verified owner self-chat provenance. Cite an authenticated owner review message instead.')
        return root['request'],'request:'+root['id']
    if type(ref) is not int or ref<1:
        raise ValueError('Cite request or a numeric authenticated owner review message ID.')
    row=store.one("SELECT id,body FROM review_messages WHERE job_id=? AND id=? AND role='user'",(root['id'],ref))
    if not row:
        raise ValueError('Owner review message is not registered for this root job.')
    return row['body'],'review:'+root['id']+':'+str(row['id'])


def explicit_request(body, deliverable):
    # Merely quoting an outside source, acknowledging a proposal, or mentioning a
    # meeting is not a direct owner instruction to add a commitment.
    # Quoted source text and negated directions cannot provide affirmative intent.
    intent=re.sub(r'```[\s\S]*?```|`[^`]*`|"[^"]*"|“[^”]*”','',body)
    intent='\n'.join(line for line in intent.splitlines() if not line.lstrip().startswith('>'))
    negative=re.search(r"\b(?:do\s+not|don't|never|not\s+to)\s+(?:create|add|record|track|log|make|put|remind)\b.{0,120}\b(?:commitment|reminder|remind)\b|\b(?:do\s+not|don't|never|not\s+to)\s+remind\s+me\b",intent,re.I|re.S)
    if negative or not re.search(r'\b(?:create|add|record|track|log|make|put|remind|need|want)\b.{0,120}\b(?:commitment|reminder|remind)\b|\bremind\s+me\b',intent,re.I|re.S):
        raise ValueError('The cited owner self-chat message must explicitly request a commitment or reminder.')
    source_terms=set(words(body))-STOP
    deliverable_terms=set(words(deliverable))-STOP
    if not deliverable_terms or len(source_terms & deliverable_terms)/len(deliverable_terms)<.8:
        raise ValueError('The commitment deliverable must be grounded in the cited owner request.')


def create(store,job,args):
    if not isinstance(args,dict) or set(args)-{'deliverable','scope','owner_message_id'}:
        raise ValueError('Only deliverable, scope and owner_message_id are supported.')
    deliverable=clean(args.get('deliverable',''),1000)
    if not deliverable:
        raise ValueError('Provide the exact deliverable requested by the owner.')
    body,identity=owner_source(store,job,args.get('owner_message_id'))
    explicit_request(body,deliverable)
    scope=clean(args.get('scope') or job.get('project') or 'global',160)
    config=settings()
    owner=clean(config.get('self_name') or config.get('self_email',''),160)
    if not owner:
        raise ValueError('Configure the owner account before creating an owner commitment.')
    owner_aliases={normalized(value) for value in (owner,config.get('self_email','')) if value}
    data=fields({'owner':owner,'deliverable':deliverable,'scope':scope})
    fingerprint=digest('owner-request',normalized(owner),normalized(deliverable),scope)
    registry=Commitments(store)
    with store.lock,store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        existing=c.execute('SELECT id FROM commitments WHERE fingerprint=?',(fingerprint,)).fetchone()
        # A different owner request for identical work does not silently reopen
        # a completed, dismissed or merged record. Avoid near-duplicate creation
        # across *all* statuses until a human reconciles it.
        if not existing:
            terms=set(words(deliverable))-STOP
            for row in c.execute('SELECT id,owner,scope,deliverable,status FROM commitments').fetchall():
                if normalized(row['owner']) not in owner_aliases or row['scope']!=scope:
                    continue
                other=set(words(row['deliverable']))-STOP
                if (normalized(row['deliverable'])==normalized(deliverable) or
                        (terms and other and len(terms & other)/len(terms | other)>=.8)):
                    existing=row
                    break
        if existing:
            result=registry.detail(existing['id'])
            result.update(reused=True,duplicate_review_required=normalized(result['deliverable'])!=normalized(deliverable))
            return result
        stamp=now(); commitment_id=uid(); evidence_id=uid()
        c.execute('''INSERT INTO commitments(id,owner,deliverable,scope,deadline_text,deadline_date,deadline_certainty,deadline_basis,status,fingerprint,version,created,updated)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                  (commitment_id,*[data[k] for k in ('owner','deliverable','scope','deadline_text','deadline_date','deadline_certainty','deadline_basis')],'open',fingerprint,1,stamp,stamp))
        # Kind, identity and URL separate self-chat instructions from registered
        # transcripts. The full verified owner message is retained as evidence.
        source_fingerprint=digest(identity,body,'owner_request')
        c.execute('''INSERT INTO commitment_evidence
            (id,commitment_id,source_job,source_file_id,source_identity,source_url,meeting_date,source_timestamp,excerpt,kind,fingerprint,reviewed,created)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (evidence_id,commitment_id,job['id'],'',identity,'','','',body,'owner_request',source_fingerprint,1,stamp))
        history(c,commitment_id,'owner_requested','owner',details={'evidence_id':evidence_id,'owner_message_id':args['owner_message_id']})
    result=registry.detail(commitment_id)
    result.update(reused=False,duplicate_review_required=False)
    return result
