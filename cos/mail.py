"""Normalize reviewed recipient lists and distinguish definite send rejection."""
import re
from email.utils import getaddresses

class SendNotSubmitted(ValueError):
    pass

ADDRESS=re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+\Z")

def recipients(value):
    if isinstance(value,list):
        if not value or not all(isinstance(x,str) for x in value):
            raise SendNotSubmitted('Recipients must be email addresses or a list of email address strings.')
        value=','.join(value)
    if not isinstance(value,str) or not value.strip() or len(value)>30000:
        raise SendNotSubmitted('Provide a non-empty recipient list.')
    if '\r' in value or '\n' in value:
        raise SendNotSubmitted('Recipient lists cannot contain header line breaks.')
    # Convert separators outside quoted display names. Do not split "Last, First".
    quoted=False;escaped=False;chars=[]
    for ch in value:
        if ch=='"' and not escaped:quoted=not quoted
        if ch==';' and not quoted:ch=','
        chars.append(ch)
        escaped=(ch=='\\' and not escaped)
    if quoted:
        raise SendNotSubmitted('Unclosed quote in recipient display name.')
    parsed=getaddresses([''.join(chars).strip(' ,')])
    result=[];seen=set()
    for name,address in parsed:
        if not name and not address:
            continue
        if not ADDRESS.fullmatch(address) or len(address)>254 or len(address.split('@')[0])>64 or address.startswith('.') or '..' in address.split('@')[0] or address.split('@')[0].endswith('.'):
            raise SendNotSubmitted('Invalid email recipient: '+(address or name)[:200]+'. Use complete email addresses.')
        if address.casefold() not in seen:
            seen.add(address.casefold());result.append(address)
    if not result or len(result)>500:
        raise SendNotSubmitted('Provide between 1 and 500 distinct email recipients.')
    return ','.join(result)

def prepare(a):
    result=dict(a)
    result['to']=recipients(a.get('to'))
    for field in ('cc','bcc'):
        if a.get(field):
            result[field]=recipients(a[field])
        else:
            result.pop(field,None)
    if not isinstance(a.get('subject'),str) or not isinstance(a.get('body'),str):
        raise SendNotSubmitted('Email subject and body must be text.')
    return result

def definitely_not_sent(tool,error):
    if tool not in {'send_email','reply_email'}:
        return False
    if isinstance(error,SendNotSubmitted):
        return True
    # Restrict this to the exact Graph validation failure. Timeouts/5xx remain uncertain.
    return bool(re.search(r'Graph API 400:.*?"code"\s*:\s*"ErrorInvalidRecipients"',str(error),re.S))
