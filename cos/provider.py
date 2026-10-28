import json
import time
import threading
import httpx
from .trace import model_call
from .config import settings, gateway_key, DEFAULTS
from .capacity import Capacity, CapacityDelayed

class NotConfigured(Exception):
    pass

class ContextWindowExceeded(RuntimeError):
    """The request must shrink before it can be retried."""
    pass

class Gateway:
    _lock = threading.Lock()
    _gates = {}
    def __init__(self, store=None):
        if store is None:
            from .db import Store
            store = Store()
        self.capacity = Capacity(store)

    @model_call
    def complete(self,messages,role='worker',tools=None,*,timeout=180,attempts=3,max_tokens=4096):
        config = settings()
        model = config['models'][role]
        self.capacity.check(model)
        gate_key = (self.capacity.store.path, model, config.get('model_parallel_requests',1))
        with self._lock:
            gate = self._gates.setdefault(gate_key,threading.BoundedSemaphore(gate_key[2]))
        if not gate.acquire(blocking=False):
            raise CapacityDelayed(model,{'retry_at':time.time()+1,'busy':True})
        try:
            return self._complete(messages,role,tools,timeout=timeout,attempts=attempts,max_tokens=max_tokens)
        finally:
            gate.release()

    def _complete(self,messages,role='worker',tools=None,*,timeout=180,attempts=3,max_tokens=4096):
        s = settings()
        key = gateway_key()
        if not key:
            raise NotConfigured('Add your AMD gateway key in Connections. It is stored in Windows Credential Manager.')
        if s['gateway_url'] != DEFAULTS['gateway_url']:
            raise NotConfigured('Unverified model endpoint blocked.')
        model = s['models'][role]
        self.capacity.check(model)
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
            payload={'model':model,'input':items,'max_output_tokens':max_tokens,'store':False,'include':['reasoning.encrypted_content'],'reasoning':{'effort':'medium'},'tools':[{'type':'function',**tool['function']} for tool in tools],'parallel_tool_calls':False}
        headers = {'Authorization':'Bearer '+key,'Ocp-Apim-Subscription-Key':key}
        for attempt in range(attempts):
            try:
                self.capacity.check(model)
                observed_capacity = self.capacity.get(model)
                # No redirect following: credentials may only reach the verified AMD gateway.
                response = httpx.post(s['gateway_url']+endpoint,json=payload,headers=headers,timeout=timeout,follow_redirects=False)
                if response.status_code == 429:
                    detail = response.text[:3000].replace(key,'[REDACTED]')
                    raise self.capacity.limited(model, response.headers, detail, s.get('capacity_recovery_seconds',900))
                if response.status_code in {502,503,504} and attempt < attempts-1:
                    time.sleep(min(8,2**attempt))
                    continue
                if response.status_code in {401,403}:
                    raise NotConfigured('AMD gateway rejected authentication or model access. Verify the key and allowed model names in Connections.')
                if response.status_code >= 300:
                    try:
                        body=response.json()
                        error=body.get('error',body)
                        detail=str(error.get('message',''))[:1200].replace(key,'[REDACTED]')
                        context_error=any(marker in json.dumps(body).lower() for marker in ('context_length_exceeded','context_window_exceeded','exceeds the context window','maximum context length'))
                    except Exception:
                        detail=''
                        context_error=False
                    if response.status_code in {400,413} and context_error:
                        raise ContextWindowExceeded('The model input exceeds the gateway context window.')
                    raise RuntimeError('AMD gateway returned HTTP '+str(response.status_code)+(': '+detail if detail else ''))
                data = response.json()
                self.capacity.succeeded(model, observed=observed_capacity)
                if use_responses:
                    output=data.get('output',[])
                    answer={'role':'assistant','content':'','tool_calls':[],'_responses_output':output,'_responses_model':model}
                    for item in output:
                        if item['type']=='function_call':
                            answer['tool_calls'].append({'id':item['call_id'],'type':'function','function':{'name':item['name'],'arguments':item['arguments']}})
                        elif item['type']=='message':
                            answer['content']+=''.join(x.get('text','') for x in item.get('content',[]) if x.get('type')=='output_text')
                    return answer,{'model':model,'usage':data.get('usage',{}),'api':'responses',
                        'output_incomplete':data.get('status')=='incomplete'}
                choice=data['choices'][0]
                return choice['message'], {'model':model,'usage':data.get('usage',{}),'api':'chat_completions',
                    'output_incomplete':choice.get('finish_reason')=='length'}
            except (httpx.TimeoutException,httpx.ConnectError):
                if attempt == attempts-1:
                    raise RuntimeError('Cannot reach AMD gateway. Check AMD network/VPN access.')
                time.sleep(2**attempt)
        raise RuntimeError('Gateway retry limit reached')

def tool_schema(name,description,properties,required=None):
    return {'type':'function','function':{'name':name,'description':description,'parameters':{'type':'object','properties':properties,'required':required or list(properties),'additionalProperties':False}}}

def string(description=''):
    return {'type':'string','description':description}
