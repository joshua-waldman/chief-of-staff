import json
import time
import threading
import re
import hashlib
from contextlib import contextmanager
import httpx
from .trace import model_call, context, CURRENT
from . import usage
from .config import settings, gateway_key, DEFAULTS
from .capacity import Capacity, CapacityDelayed

class NotConfigured(Exception):
    pass

class ModelUnavailable(NotConfigured):
    """A definite rejection of this model, before any usable model response."""
    pass

class ContextWindowExceeded(RuntimeError):
    """The request must shrink before it can be retried."""
    pass


def balance_legacy_finish(messages):
    """Close old terminal turns for replay without claiming task completion.

    Older engines saved finish without a tool receipt. Later recovery or owner
    feedback can reopen those histories, even with system messages after finish.
    Only finish is safe to close here; never invent an external action outcome.
    Work on a new list so inference does not alter the durable history.
    """
    balanced=[]
    for index,message in enumerate(messages):
        balanced.append(message)
        if message.get('role')!='assistant':
            continue
        receipts=set()
        for following in messages[index+1:]:
            if following.get('role')!='tool':
                break
            receipts.add(following.get('tool_call_id'))
        for call in message.get('tool_calls',[]):
            if call.get('id') in receipts:
                continue
            function=call.get('function',{})
            try:
                request=json.loads(function.get('arguments',''))
            except (ValueError,TypeError):
                continue
            if function.get('name')=='perform' and isinstance(request,dict) and request.get('action')=='finish':
                balanced.append({'role':'tool','tool_call_id':call['id'],'content':json.dumps({
                    'prior_turn_ended':True,'legacy_finish_receipt':True,
                    'instruction':'This historical finish ended the prior turn. Work has resumed; reassess current directions and evidence. This receipt does not verify task completion or any external action.'})})
    return balanced


class Gateway:
    _lock = threading.Lock()
    _gates = {}
    def __init__(self, store=None, purpose='agent'):
        if store is None:
            from .db import Store
            store = Store()
        self.capacity = Capacity(store)
        self.purpose = purpose

    @model_call
    def complete(self,messages,role='worker',tools=None,*,timeout=180,attempts=3,max_tokens=4096):
        config = settings()
        self._credentials(config)
        from .model_routing import select
        with context(model_role=role, model_purpose=self.purpose):
            last_rejection = None
            for selection_attempt in range(3):
                if selection_attempt < 2:
                    model, routing = select(self,config,messages,role,tools,max_tokens)
                else:
                    model = config['models'][role]
                    if model in self.unavailable_models(config):
                        raise last_rejection
                    routing = {'mode':'automatic', 'fallback':True, 'role':role,
                               'reason':'Assigned role fallback after rejected catalog models'}
                try:
                    with self._capacity_gate(model,config):
                        answer, metadata = self._complete(messages,role,tools,timeout=timeout,attempts=attempts,
                                                           max_tokens=max_tokens,config=config,model=model)
                    break
                except ModelUnavailable as error:
                    if routing['mode'] != 'automatic' or routing.get('fallback'):
                        raise
                    last_rejection = error
        answer['_cos_model'] = model
        metadata['routing'] = routing
        if routing['mode'] == 'automatic':
            answer['_cos_route'] = {**{k:v for k,v in routing.items() if k!='router_usage'}, 'model': model}
        return answer, metadata

    def _availability_key(self, config):
        scope = hashlib.sha256((config['gateway_url'] + '\0' + self._credentials(config)).encode()).hexdigest()
        return 'model_unavailable:' + scope

    def unavailable_models(self, config):
        value = self.capacity.store.state(self._availability_key(config), {})
        if not isinstance(value, dict):
            return set()
        return {model for model, until in value.items()
                if type(until) in (int, float) and until > time.time()}

    def _mark_unavailable(self, model, config):
        try:
            with self.capacity.store.lock:
                key = self._availability_key(config)
                value = self.capacity.store.state(key, {})
                value = {m: t for m, t in value.items() if type(t) in (int, float) and t > time.time()} if isinstance(value, dict) else {}
                value[model] = time.time() + 3600
                self.capacity.store.set_state(key, value)
                return True
        except Exception:
            # Cache failure cannot turn a definite gateway rejection into a retry.
            return False

    @staticmethod
    def _credentials(config):
        if config['gateway_url'] != DEFAULTS['gateway_url']:
            raise NotConfigured('Unverified model endpoint blocked.')
        key = gateway_key()
        if not key:
            raise NotConfigured('Add your AMD gateway key in Connections. It is stored in Windows Credential Manager.')
        return key

    @contextmanager
    def _capacity_gate(self,model,config):
        self.capacity.check(model)
        gate_key = (self.capacity.store.path, model, config.get('model_parallel_requests',1))
        with self._lock:
            gate = self._gates.setdefault(gate_key,threading.BoundedSemaphore(gate_key[2]))
        if not gate.acquire(blocking=False):
            raise CapacityDelayed(model,{'retry_at':time.time()+1,'busy':True})
        try:
            yield
        finally:
            gate.release()

    def decide(self,model,state,questions,*,config=None):
        config = config or settings()
        with context(model_role='router', model_purpose='routing'), self._capacity_gate(model,config):
            return self._post('/systemone',{'model':model,'state':state,'questions':questions},
                              config,timeout=10,attempts=1)

    def _complete(self,messages,role='worker',tools=None,*,timeout=180,attempts=3,max_tokens=4096,config=None,model=None):
        messages=balance_legacy_finish(messages)
        s = config or settings()
        model = model or s['models'][role]
        from .model_catalog import snapshot, catalog, supports
        from .model_routing import has_images, window
        metadata = next((m for m in snapshot(s) if m['id']==model),{})
        if not metadata and model not in DEFAULTS['models'].values():
            # A saved custom assignment must also work after a server restart,
            # before the owner visits Connections to load the catalog again.
            metadata = next((m for m in catalog()['models'] if m['id']==model),{})
        if metadata and not supports(metadata,'chat'):
            raise NotConfigured('This model does not support chat. Select a chat model in Connections: '+model)
        if metadata and has_images(messages) and not supports(metadata,'vision'):
            raise NotConfigured('This model does not support images. Select a vision model in Connections: '+model)
        if metadata and tools and (metadata.get('capabilities',{}).get('tools') is False or
                ('supported_parameters' in metadata and 'tools' not in metadata['supported_parameters'])):
            raise NotConfigured('This model does not support tools. Select a tool-capable model in Connections: '+model)
        if metadata.get('max_output_tokens'):
            max_tokens=min(max_tokens,metadata['max_output_tokens'])
        if metadata:
            from .context import estimate
            if estimate(messages,tools)+max_tokens+2048>window(s,model,metadata):
                raise ContextWindowExceeded('The request exceeds the selected model context window.')
        wire_messages=[]
        for message in messages:
            clean={k:v for k,v in message.items() if not k.startswith('_')}
            # Reasoning state belongs to its originating model, never another backend.
            if message.get('_cos_model') != model:
                clean.pop('reasoning_content',None)
            wire_messages.append(clean)
        payload = {'model':model,'messages':wire_messages,'max_completion_tokens':max_tokens}
        if tools:
            payload['tools'] = tools
            payload['tool_choice'] = 'auto'
            payload['parallel_tool_calls'] = False
        # Retain legacy behavior without discovery; use explicit gateway metadata
        # when available. GPT-oss and non-GPT providers use chat completions.
        use_responses = bool(tools) and model.lower().startswith(('gpt-', 'o1', 'o3', 'o4')) and not model.lower().startswith('gpt-oss')
        if '-codex' in model.lower() or model.lower()=='o3-pro':
            use_responses=True
        if metadata.get('capabilities',{}).get('responses') is False:
            use_responses=False
        if metadata.get('capabilities',{}).get('chat') is False and metadata.get('capabilities',{}).get('responses') is True:
            use_responses=True
        endpoint = '/chat/completions'
        if use_responses:
            items=[]
            for message in messages:
                if message.get('_responses_output') and message.get('_responses_model')==model:
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
            payload={'model':model,'input':items,'max_output_tokens':max_tokens,'store':False}
            if re.match(r'^(gpt-(?:[5-9]|[1-9]\d)|o[1-9])',model,re.I):
                payload.update(include=['reasoning.encrypted_content'],reasoning={'effort':'medium'})
            if tools:
                payload.update(tools=[{'type':'function',**tool['function']} for tool in tools],parallel_tool_calls=False)
        data = self._post(endpoint,payload,s,timeout=timeout,attempts=attempts)
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

    def _post(self,endpoint,payload,s,*,timeout,attempts):
        key=self._credentials(s)
        model=payload['model']
        headers = {'Authorization':'Bearer '+key,'Ocp-Apim-Subscription-Key':key}
        for attempt in range(attempts):
            identity = None
            try:
                self.capacity.check(model)
                observed_capacity = self.capacity.get(model)
                identity = usage.uid()
                usage.record(self.capacity.store, identity, model=model, endpoint=endpoint, key=key,
                             purpose=CURRENT.get().get('model_purpose',self.purpose),
                             role=CURRENT.get().get('model_role',''))
                # No redirect following: credentials may only reach the verified AMD gateway.
                response = httpx.post(s['gateway_url']+endpoint,json=payload,headers=headers,timeout=timeout,follow_redirects=False)
                if response.status_code >= 300:
                    usage.record(self.capacity.store, identity,
                                 status='unknown' if response.status_code >= 500 else 'rejected')
                if response.status_code == 429:
                    detail = response.text[:3000].replace(key,'[REDACTED]')
                    raise self.capacity.limited(model, response.headers, detail, s.get('capacity_recovery_seconds',900))
                if response.status_code in {502,503,504} and attempt < attempts-1:
                    time.sleep(min(8,2**attempt))
                    continue
                if response.status_code in {403,404,410,426}:
                    marked = self._mark_unavailable(model, s)
                    # An earlier timeout/5xx in this same physical retry series
                    # is ambiguous; do not initiate an additional model replay.
                    error = ModelUnavailable if marked and attempt == 0 else NotConfigured
                    raise error('AMD gateway rejected this model (HTTP '+str(response.status_code)+'). Verify model availability and access in Connections.')
                if response.status_code == 401:
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
                try:
                    data = response.json()
                except ValueError:
                    usage.record(self.capacity.store, identity, status='unknown')
                    raise
                usage.record(self.capacity.store, identity, status='reported', data=data)
                self.capacity.succeeded(model, observed=observed_capacity)
                return data
            except (httpx.TimeoutException,httpx.NetworkError):
                if identity:
                    usage.record(self.capacity.store, identity, status='unknown')
                if attempt == attempts-1:
                    raise RuntimeError('Cannot reach AMD gateway. Check AMD network/VPN access.')
                time.sleep(2**attempt)
        raise RuntimeError('Gateway retry limit reached')

def tool_schema(name,description,properties,required=None):
    return {'type':'function','function':{'name':name,'description':description,'parameters':{'type':'object','properties':properties,'required':required or list(properties),'additionalProperties':False}}}

def string(description=''):
    return {'type':'string','description':description}
