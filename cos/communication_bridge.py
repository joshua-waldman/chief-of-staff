"""Fixed Microsoft operations using installed skill authentication, in a subprocess.

No arbitrary endpoints or credentials cross the process boundary.
"""
import base64
import hashlib
import io
import json
import re
import sys
from html.parser import HTMLParser
from contextlib import redirect_stderr
from pathlib import Path
from urllib.parse import quote, urlparse
from .integrations import skill_dir


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts=[]; self.hidden=0

    def handle_starttag(self, tag, attrs):
        if tag in {'script','style'}: self.hidden+=1
        if tag in {'br','p','div','tr','li','blockquote'}: self.parts.append('\n')
        if tag in {'td','th'}: self.parts.append('\t')
        if tag=='a':
            href=dict(attrs).get('href')
            if href: self.parts.append('['+href+'] ')

    def handle_endtag(self, tag):
        if tag in {'script','style'}: self.hidden=max(0,self.hidden-1)
        if tag in {'p','div','tr','li','blockquote'}: self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden: self.parts.append(data)


def plain(body):
    if body.get('contentType','').lower()!='html': return body.get('content','')
    parser=PlainText(); parser.feed(body.get('content',''))
    return ''.join(parser.parts)


def addresses(items):
    return ', '.join(x['emailAddress']['address'] for x in items)


def ident(value):
    if not isinstance(value,str) or not value.strip() or len(value)>4096:
        raise ValueError('A concrete source ID is required.')
    return quote(value,safe='')


def verify_action(graph, payload):
    """Fixed read-only probes. A missing message never proves a send failed."""
    from datetime import datetime
    tool, args = payload['tool'], payload['arguments']
    unknown = {'outcome': 'unknown', 'evidence': 'Available source data does not establish the action outcome.'}
    if tool == 'mark_email_read':
        message = graph.get('/me/messages/'+ident(args['id']), params={'$select': 'id,isRead'})
        if message.get('isRead') is True:
            return {'outcome': 'executed', 'evidence': {'message_id': message['id'], 'isRead': True}}
        return unknown
    if tool == 'delete_email':
        message = graph.get('/me/messages/'+ident(args['id']), params={'$select':'id,parentFolderId'})
        folder = graph.get('/me/mailFolders/deleteditems', params={'$select':'id'})
        if folder.get('id') and message.get('parentFolderId') == folder['id']:
            return {'outcome':'executed', 'evidence':{'message_id':message['id'], 'parentFolderId':folder['id']}}
        return unknown
    started = payload.get('started')
    if not started:
        return unknown
    start = datetime.fromisoformat(started.replace('Z', '+00:00'))
    if tool == 'send_teams':
        sys.path.insert(0,str(skill_dir('slai-teams')/'scripts'))
        from skype_client import SkypeClient
        from .teams_content import text as teams_text
        owner = graph.get('/me', params={'$select':'id'})
        rows, _ = SkypeClient().list_messages(args['chat_id'], page_size=100)
        for message in rows:
            sender = (message.get('from') or '').rsplit('/contacts/',1)[-1].casefold()
            date = message.get('composetime') or message.get('originalarrivaltime')
            if (message.get('id') and sender in {owner['id'].casefold(), '8:orgid:'+owner['id'].casefold()} and
                    date and datetime.fromisoformat(date.replace('Z','+00:00')) >= start and
                    teams_text(message.get('content',''),message.get('messagetype','')).strip() == args['body'].strip()):
                return {'outcome':'executed', 'evidence':{'chat_id':args['chat_id'], 'message_id':message['id'], 'date':date}}
        return unknown
    if tool == 'calendar_rsvp':
        message = graph.get('/me/events/'+ident(args['id']), params={'$select': 'id,responseStatus'})
        response = message.get('responseStatus', {})
        expected = {'accept':'accepted','decline':'declined','tentative':'tentativelyAccepted'}.get(args.get('response'))
        # A response state alone cannot prove a notification was sent.
        if args.get('send_response', True) is False and response.get('response') == expected and response.get('time') and datetime.fromisoformat(response['time'].replace('Z','+00:00')) >= start:
            return {'outcome':'executed', 'evidence': {'event_id':message['id'], 'responseStatus':response}}
        return unknown
    if tool not in {'send_email', 'reply_email'}:
        return unknown
    conversation = None
    if tool == 'reply_email':
        original = graph.get('/me/messages/'+ident(args['id']), params={'$select':'conversationId'})
        conversation = original.get('conversationId')
        if not conversation:
            return unknown
    page = graph.get('/me/mailFolders/sentitems/messages', params={
        '$filter': 'sentDateTime ge '+start.isoformat(), '$top':'50',
        '$select':'id,conversationId,subject,body,sentDateTime,toRecipients,ccRecipients,bccRecipients'})
    def intended(value):
        if isinstance(value, list):
            return {str(x).strip().casefold() for x in value}
        return {x.strip().casefold() for x in re.split('[,;]', value or '') if x.strip()}
    for message in page.get('value', []):
        body = message.get('body', {})
        if conversation and message.get('conversationId') != conversation:
            continue
        if (not message.get('id') or message.get('subject') != args.get('subject') or
                str(body.get('contentType', '')).casefold() != 'text' or body.get('content') != args.get('body') or
                not message.get('sentDateTime') or datetime.fromisoformat(message['sentDateTime'].replace('Z','+00:00')) < start):
            continue
        if all({x['emailAddress']['address'].strip().casefold() for x in message.get(field+'Recipients', [])} == intended(args.get(field)) for field in ('to','cc','bcc')):
            return {'outcome':'executed', 'evidence': {'sent_message_id':message['id'], 'sentDateTime':message['sentDateTime']}}
    return unknown


def reply_history(graph, original):
    """Include accessible conversation messages even when the latest omits quotes."""
    messages=[original]
    conversation=original.get('conversationId')
    if conversation:
        path='/me/messages'
        params={'$filter':"conversationId eq '"+conversation.replace("'","''")+"'",'$select':'id,subject,from,toRecipients,ccRecipients,sentDateTime,body,uniqueBody', '$top':'100'}
        by_id={original['id']:original}
        for _ in range(100):
            page=graph.get(path,params=params)
            for message in page.get('value',[]): by_id.setdefault(message['id'],message)
            following=page.get('@odata.nextLink')
            if not following: break
            parsed=urlparse(following)
            if parsed.scheme!='https' or parsed.netloc!='graph.microsoft.com' or parsed.path!='/v1.0/me/messages':
                raise ValueError('Untrusted reply-history continuation.')
            path='/me/messages?'+parsed.query; params=None
        else: raise ValueError('Conversation history is incomplete; narrow the reply scope before preparing it.')
        messages=sorted(by_id.values(),key=lambda m:(m.get('sentDateTime',''),m['id']),reverse=True)
    blocks=[]
    for message in messages:
        if 'body' not in message: raise ValueError('A conversation message body is unavailable; no reply preview prepared.')
        blocks.append('\n\n----- Original message -----\nFrom: '+addresses([message['from']])+'\nSent: '+message.get('sentDateTime','')+'\nTo: '+addresses(message.get('toRecipients',[]))+'\nCc: '+addresses(message.get('ccRecipients',[]))+'\nSubject: '+message.get('subject','')+'\n\n'+plain(message.get('uniqueBody') or message['body']))
    return ''.join(blocks)


def perform(graph, action, a):
    if action=='verify_action':
        return verify_action(graph,a)
    if action=='teams_attachments':
        sys.path.insert(0,str(skill_dir('slai-teams')/'scripts'))
        from skype_client import SkypeClient
        from .teams_content import download_images
        return download_images(SkypeClient(),a['messages'],a['target'])
    if action=='prepare_reply':
        if not isinstance(a.get('body'),str) or not a['body'].strip() or type(a.get('reply_all',False)) is not bool:
            raise ValueError('Provide reply text and a boolean reply_all.')
        original=graph.get('/me/messages/'+ident(a['id']), params={'$select':'id,conversationId,subject,from,replyTo,toRecipients,ccRecipients,sentDateTime,body,uniqueBody'})
        history=reply_history(graph,original)
        to=addresses(original.get('replyTo') or [original['from']])
        cc=''
        if a.get('reply_all'):
            owner=graph.get('/me',params={'$select':'mail,userPrincipalName'})
            own={str(owner.get(k,'')).casefold() for k in ('mail','userPrincipalName')}
            cc=addresses([x for x in original.get('toRecipients',[])+original.get('ccRecipients',[]) if x['emailAddress']['address'].casefold() not in own|{x.strip().casefold() for x in to.split(',')}])
        subject=original.get('subject','')
        if not re.match(r'(?i)^re:',subject): subject='Re: '+subject
        return {'id':a['id'],'to':to,'cc':cc,'subject':subject,'body':a['body']+history,'_reply_history':history,'reply_all':bool(a.get('reply_all'))}
    if action=='reply_email':
        history=a.get('_reply_history')
        if not history or not a['body'].endswith(history):
            raise ValueError('Prepare a fresh reply preview; the reviewed conversation history must be retained.')
        message={'subject':a['subject'],'body':{'contentType':'Text','content':a['body']},'toRecipients':[{'emailAddress':{'address':x.strip()}} for x in a['to'].split(',') if x.strip()], 'ccRecipients':[{'emailAddress':{'address':x.strip()}} for x in a.get('cc','').split(',') if x.strip()]}
        digest=hashlib.sha256(a['body'].encode()).hexdigest()
        graph.post('/me/messages/'+ident(a['id'])+('/replyAll' if a.get('reply_all') else '/reply'),{'message':message})
        return {'executed':True,'operation':action,'source_id':a['id'],'body_sha256':digest}
    if action=='mail_attachments':
        target=Path(a['target']); target.mkdir(parents=True,exist_ok=True)
        path='/me/messages/'+ident(a['id'])+'/attachments'; params={'$top':'100'}; records=[]
        for _ in range(100):
            data=graph.get(path,params=params)
            for item in data.get('value',[]):
                record={k:item.get(k) for k in ('id','name','contentType','size','isInline','contentId')}
                if item.get('@odata.type')=='#microsoft.graph.fileAttachment':
                    content=item.get('contentBytes')
                    if content is None:
                        content=graph.get('/me/messages/'+ident(a['id'])+'/attachments/'+ident(item['id'])).get('contentBytes')
                    if content is None: raise ValueError('Attachment content was unavailable; download is incomplete.')
                    # IDs, rather than display names, identify attachments. Exclusive
                    # creation also prevents duplicates silently overwriting evidence.
                    folder=target/hashlib.sha256(item['id'].encode()).hexdigest()
                    folder.mkdir(exist_ok=True)
                    name=re.sub(r'[^a-zA-Z0-9._ -]','_',item.get('name') or 'attachment')[:120].strip(' .') or 'attachment'
                    if not Path(name).suffix:
                        import mimetypes
                        name+=mimetypes.guess_extension(item.get('contentType','')) or ''
                    destination=folder/name
                    with destination.open('xb') as f: f.write(base64.b64decode(content,validate=True))
                    record['savedTo']=str(destination)
                else: record['coverage']='Reference or item attachment; content not downloaded.'
                records.append(record)
            following=data.get('@odata.nextLink')
            if not following: return {'message_id':a['id'],'attachments':records}
            parsed=urlparse(following)
            if parsed.scheme!='https' or parsed.netloc!='graph.microsoft.com' or parsed.path!='/v1.0/me/messages/'+ident(a['id'])+'/attachments':
                raise ValueError('Untrusted attachment continuation.')
            path=parsed.path.removeprefix('/v1.0')+'?'+parsed.query; params=None
        raise ValueError('Attachment page budget exceeded; download is incomplete.')
    if action=='calendar_events':
        return graph.get('/me/calendarView',params={'startDateTime':a['start'],'endDateTime':a['end'],'$top':'50','$select':'id,subject,start,end,organizer,responseStatus,isCancelled,type'})
    if action=='calendar_event':
        return graph.get('/me/events/'+ident(a['id']))
    if action=='calendar_rsvp':
        operation={'accept':'accept','decline':'decline','tentative':'tentativelyAccept'}.get(a.get('response'))
        if not operation or type(a.get('send_response',True)) is not bool: raise ValueError('Choose accept, decline or tentative, and a boolean send_response.')
        graph.post('/me/events/'+ident(a['id'])+'/'+operation,{'comment':a.get('comment',''),'sendResponse':a.get('send_response',True)})
    elif action=='delete_email':
        graph.post('/me/messages/'+ident(a['id'])+'/move', {'destinationId':'deleteditems'})
    elif action=='mark_email_read':
        graph.patch('/me/messages/'+ident(a['id']),{'isRead':True})
    elif action=='mark_teams_read':
        owner=graph.get('/me',params={'$select':'id'})
        graph.post('/chats/'+ident(a['chat_id'])+'/markChatReadForUser',{'user':{'id':owner['id']}})
    else: raise ValueError('Unsupported communication action.')
    return {'executed':True,'operation':action,'source_id':a.get('id') or a.get('chat_id'),'response':a.get('response'),'send_response':a.get('send_response',True) if action=='calendar_rsvp' else None}


def main():
    from .monitor_bridge import stable_mail_client
    sys.path.insert(0,str(skill_dir('slai-email')/'scripts'))
    from graph_client import GraphClient
    payload=json.load(sys.stdin)
    errors=io.StringIO()
    try:
        with redirect_stderr(errors):
            result=perform(stable_mail_client(GraphClient),payload['action'],payload['arguments'])
    except (Exception,SystemExit) as error:
        message=errors.getvalue() or str(error)
        result={'bridge_error':message,'not_executed':isinstance(error,ValueError) or bool(re.search(r'Graph API (?:400|401|403|404|405|409|412|422):',message))}
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__': main()
