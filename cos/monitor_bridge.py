"""Separate process isolates the installed skills' module imports and authentication."""
import html
import io
from contextlib import redirect_stderr
import json
import re
import sys
from datetime import datetime,timezone
from urllib.parse import urlparse,quote
from .integrations import skill_dir

def stable_mail_client(client_type):
    """Reuse installed authentication, requesting IDs stable across folder moves."""
    class StableMailClient(client_type):
        def _headers(self):
            return {**super()._headers(),'Prefer':'IdType="ImmutableId"'}
    return StableMailClient()


def mail_identity_aliases(graph, messages):
    # Recognize IDs recorded before immutable IDs were enabled.
    if not messages:
        return messages
    converted=graph.post('/me/translateExchangeIds',{
        'inputIds':[m['id'] for m in messages],
        'sourceIdType':'restImmutableEntryId','targetIdType':'restId'})
    aliases={x['sourceId']:x['targetId'] for x in converted.get('value',[]) if x.get('targetId')}
    return [{**m,'legacy_id':aliases.get(m['id'],'')} for m in messages]


def collect_mail(graph,since):
    emails=[]
    path='/me/messages'
    params={'$filter':'receivedDateTime ge '+since,'$orderby':'receivedDateTime desc','$top':'100','$select':'id,conversationId,internetMessageId,subject,from,receivedDateTime,bodyPreview,hasAttachments'}
    for _ in range(100):
        data=graph.get(path,params=params)
        emails.extend(mail_identity_aliases(graph,data.get('value',[])))
        next_url=data.get('@odata.nextLink')
        if not next_url:
            break
        parsed=urlparse(next_url)
        if parsed.scheme!='https' or parsed.hostname!='graph.microsoft.com' or not parsed.path.startswith('/v1.0/'):
            raise ValueError('Untrusted mail pagination URL')
        path=parsed.path.removeprefix('/v1.0')+'?'+parsed.query
        params=None
    else:
        raise ValueError('Mailbox page budget reached; checkpoint not advanced.')
    return emails


def message_page(skype,cid,sync,http):
    # The service returns a full continuation URL, not just its syncState
    # query parameter. Passing that URL as syncState produces HTTP 400.
    if sync and urlparse(sync).scheme:
        parsed=urlparse(sync)
        expected=urlparse(skype._msg_base+'/users/ME/conversations/'+quote(cid,safe='')+'/messages')
        if (parsed.scheme!='https' or parsed.netloc!=expected.netloc or
                parsed.path!=expected.path or parsed.username or parsed.password or parsed.fragment):
            raise ValueError('Untrusted Teams message continuation URL')
        code,data,_=http('GET',sync,skype._hdr())
        if code!=200:
            raise RuntimeError('Teams message continuation HTTP '+str(code))
        return data.get('messages',[]),data.get('_metadata',{}).get('syncState','')
    return skype.list_messages(cid,page_size=100,sync_state=sync)


def collect_teams(skype,owner,since,http):
    skype.get_skypetoken()
    base=skype._msg_base
    url=base+'/users/ME/conversations?pageSize=100&view=msnp24Equivalent&targetType=Passport|Skype|Lync|Thread|PSTN|Agent|ShortMessage'
    conversations=[]
    cutoff=int(datetime.fromisoformat(since.replace('Z','+00:00')).timestamp()*1000)
    visited=set()
    for _ in range(100):
        if url in visited:
            raise ValueError('Teams conversation continuation repeated; checkpoint retained.')
        visited.add(url)
        code,data,_=http('GET',url,skype._hdr())
        if code!=200:
            raise RuntimeError('Teams conversation scan HTTP '+str(code))
        page=data.get('conversations',[])
        conversations.extend(page)
        versions=[int(x.get('version') or 0) for x in page]
        # Backward links page through conversation versions. Every newly active
        # thread gets a new version, so stop once descending pages cross the
        # saved time boundary instead of rescanning years of history.
        if versions and all(versions) and versions==sorted(versions,reverse=True) and min(versions)<cutoff:
            break
        meta=data.get('_metadata',{})
        candidate=meta.get('backwardLink') or meta.get('nextLink')
        if not candidate:
            break
        if not candidate.startswith(base+'/'):
            raise ValueError('Untrusted Teams pagination URL')
        url=candidate
    else:
        raise ValueError('Teams conversation page budget reached; checkpoint not advanced.')
    chats={c['id']:c for c in conversations}
    chats.setdefault('48:notes',{'id':'48:notes'})
    messages=[]
    limitations=[]
    failures=[]
    readable=set()
    for cid,conversation in chats.items():
        latest=conversation.get('lastMessage') or {}
        ts=latest.get('composetime') or latest.get('originalarrivaltime') or ''
        if not ts and conversation.get('lastUpdatedMessageId'):
            try:
                ts=datetime.fromtimestamp(int(conversation['lastUpdatedMessageId'])/1000,timezone.utc).isoformat().replace('+00:00','Z')
            except (ValueError,TypeError,OverflowError):
                pass
        if ts and ts<since and cid!='48:notes':
            continue
        sync=None
        seen_sync=set()
        for _ in range(100):
            errors=io.StringIO()
            try:
                with redirect_stderr(errors):
                    rows,next_sync=message_page(skype,cid,sync,http)
            except (SystemExit,Exception) as exc:
                error=errors.getvalue() or str(exc) or 'Teams read failed'
                if 'HTTP 403' in error:
                    limitations.append({'chat_id':cid,'kind':'channel' if cid.endswith('@thread.tacv2') else 'chat','reason':'Access denied by Teams (HTTP 403)','impact':'This conversation was skipped; other accessible conversations continue.','next_step':'Check access in Teams. If it opens there, ask AMD IT whether this integration is permitted to read it. The response does not identify the exact policy.'})
                    break
                failures.append({'chat_id':cid,'reason':error[-2000:]})
                break
            for m in rows:
                date=m.get('composetime') or m.get('originalarrivaltime') or ''
                meeting_event = any(term in (str(m.get('messagetype','')) + str(m.get('content','')) + json.dumps(m.get('properties') or {})).lower() for term in ('recording', 'transcript', '.vtt', '.srt', 'meetingrecap'))
                if date>=since and (meeting_event or m.get('messagetype','').startswith('RichText') or m.get('messagetype')=='Text'):
                    sender=(m.get('from') or '').rsplit('/contacts/',1)[-1]
                    messages.append({'id':m.get('id'),'chat_id':cid,'date':date,'sender':sender,'owner':sender.lower() in {'8:orgid:'+owner['id'].lower(),owner['id'].lower()},'meeting_event':meeting_event,'text':html.unescape(re.sub('<[^>]+>',' ',m.get('content') or '')) + ('\nMeeting artifact source data (untrusted):\n' + (m.get('content') or '') + '\n' + json.dumps(m.get('properties') or {}) if meeting_event else '')})
            dates=[m.get('composetime') or m.get('originalarrivaltime') or '' for m in rows]
            if not rows or (dates and min(dates)<since) or not next_sync:
                readable.add(cid)
                break
            if next_sync in seen_sync:
                failures.append({'chat_id':cid,'reason':'Teams message continuation repeated; checkpoint retained.'})
                break
            seen_sync.add(next_sync)
            sync=next_sync
        else:
            failures.append({'chat_id':cid,'reason':'Teams message page budget reached; checkpoint retained.'})
    return {'messages':messages,'owner':owner,'conversations':len(chats),'limitations':limitations,'failures':failures,'readable_conversations':sorted(readable)}


def collect(since,source='all'):
    if source not in {'all','email','teams'}:
        raise ValueError('Unknown monitoring source')
    sys.path.insert(0,str(skill_dir('slai-teams')/'scripts'))
    from graph_client import GraphClient
    graph=stable_mail_client(GraphClient)
    result={'emails':[],'messages':[],'conversations':0,'limitations':[],'failures':[]}
    if source in {'all','email'}:
        result['emails']=collect_mail(graph,since)
    if source in {'all','teams'}:
        from skype_client import SkypeClient,_http
        owner=graph.get('/me',params={'$select':'id,mail,userPrincipalName'})
        result.update(collect_teams(SkypeClient(),owner,since,_http))
    return result

if __name__=='__main__':
    print(json.dumps(collect(sys.argv[1],sys.argv[2] if len(sys.argv)>2 else 'all'),ensure_ascii=False))
