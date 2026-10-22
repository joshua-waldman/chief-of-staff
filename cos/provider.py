import json
import time
import httpx
from .trace import model_call
from .config import settings, gateway_key, DEFAULTS

class NotConfigured(Exception):
    pass

class Gateway:
    @model_call
    def complete(self,messages,role='worker',tools=None,*,timeout=180,attempts=3,max_tokens=4096):
        s = settings()
        key = gateway_key()
        if not key:
            raise NotConfigured('Add your AMD gateway key in Connections. It is stored in Windows Credential Manager.')
        if s['gateway_url'] != DEFAULTS['gateway_url']:
            raise NotConfigured('Unverified model endpoint blocked.')
        model = s['models'][role]
        payload = {'model':model,'messages':[{k:v for k,v in m.items() if not k.startswith('_')} for m in messages],'max_completion_tokens':max_tokens}
        if tools:
            payload['tools'] = tools
            payload['tool_choice'] = 'auto'
            payload['parallel_tool_calls'] = False
        use_responses = bool(tools) and model.lower().startswith('gpt-')
        endpoint = '/chat/completions'
        if use_responses:
            items=[]
            for message in messages:
                if message.get('_responses_output') and message.get('_responses_model',model)==model:
                    items.extend(message['_responses_output'])
                    continue
                if message['role']=='tool':
                    items.append({'type':'function_call_output','call_id':message['tool_call_id'],'output':message['content']})
                    continue
                content=message.get('content')
                if content:
                    if isinstance(content,list):
                        content=[{'type':'input_image','image_url':x['image_url']['url']} if x['type']=='image_url' else {'type':'input_text','text':x['text']} for x in content]
                    items.append({'role':message['role'],'content':content})
                for call in message.get('tool_calls',[]):
                    items.append({'type':'function_call','call_id':call['id'],'name':call['function']['name'],'arguments':call['function']['arguments']})
            endpoint='/responses'
            payload={'model':model,'input':items,'max_output_tokens':8192,'store':False,'include':['reasoning.encrypted_content'],'reasoning':{'effort':'medium'},'tools':[{'type':'function',**tool['function']} for tool in tools],'parallel_tool_calls':False}
        headers = {'Authorization':'Bearer '+key,'Ocp-Apim-Subscription-Key':key}
        for attempt in range(attempts):
            try:
                # No redirect following: credentials may only reach the verified AMD gateway.
                response = httpx.post(s['gateway_url']+endpoint,json=payload,headers=headers,timeout=timeout,follow_redirects=False)
                if response.status_code in {429,502,503,504} and attempt < attempts-1:
                    time.sleep(min(8,2**attempt))
                    continue
                if response.status_code in {401,403}:
                    raise NotConfigured('AMD gateway rejected authentication or model access. Verify the key and allowed model names in Connections.')
                if response.status_code >= 300:
                    try:
                        body=response.json()
                        error=body.get('error',body)
                        detail=str(error.get('message',''))[:1200].replace(key,'[REDACTED]')
                    except Exception:
                        detail=''
                    raise RuntimeError('AMD gateway returned HTTP '+str(response.status_code)+(': '+detail if detail else ''))
                data = response.json()
                if use_responses:
                    output=data.get('output',[])
                    answer={'role':'assistant','content':'','tool_calls':[],'_responses_output':output,'_responses_model':model}
                    for item in output:
                        if item['type']=='function_call':
                            answer['tool_calls'].append({'id':item['call_id'],'type':'function','function':{'name':item['name'],'arguments':item['arguments']}})
                        elif item['type']=='message':
                            answer['content']+=''.join(x.get('text','') for x in item.get('content',[]) if x.get('type')=='output_text')
                    if data.get('status')=='incomplete' and not answer['tool_calls']:
                        raise RuntimeError('Model exhausted its response budget before producing an action. Try a smaller task.')
                    return answer,{'model':model,'usage':data.get('usage',{}),'api':'responses'}
                return data['choices'][0]['message'], {'model':model,'usage':data.get('usage',{}),'api':'chat_completions'}
            except (httpx.TimeoutException,httpx.ConnectError):
                if attempt == attempts-1:
                    raise RuntimeError('Cannot reach AMD gateway. Check AMD network/VPN access.')
                time.sleep(2**attempt)
        raise RuntimeError('Gateway retry limit reached')

def tool_schema(name,description,properties,required=None):
    return {'type':'function','function':{'name':name,'description':description,'parameters':{'type':'object','properties':properties,'required':required or list(properties),'additionalProperties':False}}}

def string(description=''):
    return {'type':'string','description':description}
