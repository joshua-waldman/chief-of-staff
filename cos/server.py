import base64
import json
import mimetypes
import secrets
import shutil
import subprocess
import sys
import threading
import time
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI,Request,HTTPException
from fastapi.responses import FileResponse,HTMLResponse,JSONResponse
from fastapi.staticfiles import StaticFiles
from .config import ROOT,DATA,settings,save_settings,gateway_key,store_key
from .db import Store,uid,now
from .engine import Engine,session
from .monitor import Monitor
from .integrations import skill_inventory,mcp_inventory,auth_status,skill_dir
from .tools import file_record,workspace,safe_name
from .provider import Gateway
from .trace import emit,context,failure,bundle

TOKEN=secrets.token_urlsafe(32)
store=Store()
engine=Engine(store)
monitor=Monitor(store)
auth={'running':False,'output':''}

@asynccontextmanager
async def lifespan(app):
    emit('server.started',{'source_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'cos').glob('*.py')},'models':settings()['models'],'parallel_agents':settings()['parallel_agents']})
    engine.start()
    monitor.start()
    yield
    monitor.stop_event.set()
    engine.stop()

app=FastAPI(title='AMD Chief of Staff',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)

@app.middleware('http')
async def local_security(request,call_next):
    host=request.headers.get('host','')
    hostname=host.split(':')[0]
    if hostname not in {'127.0.0.1','localhost','testserver'}:
        return JSONResponse({'error':'Local access only'},status_code=403)
    origin=request.headers.get('origin')
    if origin and origin not in {'http://'+host,'https://'+host}:
        return JSONResponse({'error':'Cross-origin requests are blocked'},status_code=403)
    if request.url.path.startswith('/api/'):
        if not secrets.compare_digest(request.cookies.get('cos_session',''),TOKEN):
            return JSONResponse({'error':'Open the local application first'},status_code=401)
        if request.method not in {'GET','HEAD'} and not secrets.compare_digest(request.headers.get('x-cos-token',''),TOKEN):
            return JSONResponse({'error':'Missing request token'},status_code=403)
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='no-referrer'
    response.headers['X-Frame-Options']='DENY'
    response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    response.headers['Cache-Control']='no-store'
    return response

@app.middleware('http')
async def request_trace(request,call_next):
    request_id=uid()
    started=time.monotonic()
    with context(request_id=request_id,trace_id=request_id):
        try:
            response=await call_next(request)
        except Exception as error:
            emit('http.failed',{'method':request.method,**failure(error)})
            raise
        route=request.scope.get('route')
        emit('http.completed',{'method':request.method,'route':getattr(route,'path','<unmatched>'),'status':response.status_code,'duration_ms':round((time.monotonic()-started)*1000)})
        response.headers['X-Request-ID']=request_id
        return response

@app.exception_handler(ValueError)
def bad_input(request,error):
    return JSONResponse({'error':str(error)},status_code=400)

@app.get('/')
def index():
    response=HTMLResponse((ROOT/'web/index.html').read_text(encoding='utf-8').replace('__REQUEST_TOKEN__',TOKEN))
    response.set_cookie('cos_session',TOKEN,httponly=True,samesite='strict')
    return response

@app.get('/api/status')
def status():
    jobs=store.rows('SELECT id,parent_id,status,org FROM jobs')
    config=settings()
    with engine.lock:
        active=len(engine.active)
    monitoring=store.state('monitor_status',{'state':'paused'})
    monitor_attention=bool(config['monitor_enabled'] and (monitoring.get('attention_required') or monitoring.get('state')=='needs_attention'))
    return {'gateway_configured':bool(gateway_key()),'jobs':len([j for j in jobs if not j['parent_id']]),'running':sum(j['status']=='running' for j in jobs),'needs_attention':sum(j['status'] in {'waiting_input','awaiting_approval','blocked','failed'} for j in jobs)+int(monitor_attention)+int(any(j['status']=='capacity_paused' for j in jobs)),'monitor_attention':monitor_attention,'completed':sum(j['status']=='completed' for j in jobs),'monitor':monitoring,'settings':config,'queue_active':active,'organizations':store.rows('SELECT * FROM organizations ORDER BY created')}

@app.get('/api/jobs')
def jobs():
    rows=store.rows('''SELECT j.id,j.parent_id,j.title,j.org,j.project,j.status,j.created,j.updated,j.result,j.question,b.body AS brief_body
        FROM jobs j LEFT JOIN job_briefs b ON b.job_id=j.id ORDER BY j.created DESC LIMIT 1000''')
    from .message_triage import category
    for job in rows:
        brief=json.loads(job.pop('brief_body') or '{}')
        job['triage_category']=category(brief) if job['org']=='Communications' else ''
        job['triage_summary']=brief.get('email_excerpt','') if job['org']=='Communications' else ''
    return rows

@app.get('/api/organization-agents')
def organization_agents():
    agents=store.rows('''SELECT j.id,j.parent_id,j.title,j.org,j.project,j.status,j.updated,
        p.title AS parent_title,p.org AS parent_org,b.body AS brief_body
        FROM jobs j LEFT JOIN jobs p ON p.id=j.parent_id
        LEFT JOIN job_briefs b ON b.job_id=j.id ORDER BY j.created DESC,j.id''')
    for agent in agents:
        brief=json.loads(agent.pop('brief_body') or '{}')
        agent['status_summary']=brief.get('status_summary','')
        agent['next_step']=brief.get('next_step','')
    return agents

@app.get('/api/jobs/{id}')
def job(id):
    result=store.detail(id)
    if not result:
        raise HTTPException(404,'Job not found')
    return result

@app.get('/api/jobs/{id}/diagnostics')
def diagnostics(id):
    if not store.job(id):
        raise HTTPException(404,'Job not found')
    return JSONResponse(bundle(store,id),headers={'Content-Disposition':'attachment; filename="cos-diagnostics-'+id+'.json"'})

@app.post('/api/jobs/{id}/source')
def communication_source(id):
    job=store.job(id)
    if not job:
        raise HTTPException(404,'Job not found')
    if job['org']!='Communications':
        raise ValueError('Source text is available for Communications jobs.')
    from .conversations import load_sources
    return load_sources(store,job)

@app.post('/api/jobs')
async def create_job(request:Request):
    body=await request.json()
    text=body.get('request','').strip()
    if not text or len(text)>100000:
        raise ValueError('Enter a request of up to 100,000 characters.')
    project=body.get('project','')
    if project and project not in settings()['projects']:
        raise ValueError('Unknown project')
    files=body.get('files',[])
    if len(files)>10:
        raise ValueError('Attach at most ten files.')
    decoded=[]
    for f in files:
        data=base64.b64decode(f['data'],validate=True)
        if len(data)>25*1024*1024:
            raise ValueError('Each attachment must be under 25 MB.')
        decoded.append((safe_name(f['name']),data))
    if sum(len(x[1]) for x in decoded)>50*1024*1024:
        raise ValueError('Attachments must total under 50 MB.')
    # Keep the new job invisible to workers until all inputs are attached.
    with engine.lock:
        j=store.create_job(text,project=project)
        store.update(j['id'],status='preparing')
        for name,data in decoded:
            p=workspace(j['id'])/'inputs'/(uid()[:8]+'-'+name)
            p.parent.mkdir(parents=True,exist_ok=True)
            p.write_bytes(data)
            file_record(store,j['id'],p)
        store.update(j['id'],status='queued')
    return store.detail(j['id'])

@app.post('/api/jobs/{id}/answer')
async def answer(id,request:Request):
    body=await request.json()
    if not body.get('answer','').strip():
        raise ValueError('Enter your response')
    engine.answer(id,body['answer'])
    return {'ok':True}

@app.post('/api/jobs/{id}/review')
async def review_job(id,request:Request):
    from .reviews import submit
    body=await request.json()
    return submit(engine,id,body.get('message',''),body.get('remember',False),body.get('guidance',''),body.get('applies_to',''),body.get('draft'),body.get('remember_target','general'))

@app.post('/api/jobs/{id}/retry-capacity')
def retry_capacity(id):
    engine.retry_capacity(id)
    return {'ok':True}

@app.post('/api/jobs/{id}/cancel')
def cancel(id):
    if not store.job(id):
        raise HTTPException(404,'Job not found')
    engine.cancel(id)
    return {'ok':True}

@app.post('/api/approvals/{id}')
async def approve(id,request:Request):
    body=await request.json()
    if type(body.get('approved')) is not bool:
        raise ValueError('An explicit approval decision is required')
    # Run blocking external actions off the event loop.
    import asyncio
    await asyncio.to_thread(engine.decide,id,body['approved'],body.get('edited'))
    return {'ok':True}

@app.post('/api/approvals/{id}/resolve')
async def resolve(id,request:Request):
    body=await request.json()
    a=store.one('SELECT * FROM approvals WHERE id=?',(id,))
    if not a or a['status']!='uncertain' or body.get('outcome') not in {'executed','not_executed'}:
        raise ValueError('Choose a verified outcome for an uncertain action.')
    j=store.job(a['job_id'])
    s=session(j)
    engine.add_result(s['messages'],s.pop('waiting_call'),{'verified_by_user':body['outcome'],'instruction':'Do not repeat the action.' if body['outcome']=='executed' else 'The user verified it did not execute; request a fresh approval before retrying.'})
    store.write('UPDATE approvals SET status=? WHERE id=?',('verified_'+body['outcome'],id))
    engine.persist(j['id'],s,status='queued',question='')
    store.event(j['id'],'verified','User verified action outcome: '+body['outcome'])
    return {'ok':True}

@app.get('/api/files/{id}')
def download(id):
    f=store.one('SELECT * FROM files WHERE id=?',(id,))
    if not f or not Path(f['path']).is_file():
        raise HTTPException(404,'File not found')
    return FileResponse(f['path'],filename=f['name'],media_type='application/octet-stream')

@app.get('/api/connections')
def connections():
    return {'gateway':{'configured':bool(gateway_key()),'url':settings()['gateway_url']},'microsoft':auth_status(),'skills':skill_inventory(),'mcp':mcp_inventory(),'sandbox':{'available':bool(shutil.which('docker'))},'auth_flow':auth}

@app.post('/api/settings')
async def update_settings(request:Request):
    changes=await request.json()
    # Serialize the pause with worker submission so nothing new starts after
    # the disconnect request has been acknowledged.
    with engine.lock:
        s=save_settings(changes)
    return s

@app.post('/api/gateway/key')
async def set_key(request:Request):
    body=await request.json()
    store_key(body['key'])
    return {'ok':True}

@app.post('/api/gateway/test')
def test_gateway():
    answer,meta=Gateway().complete([{'role':'user','content':'Reply only OK.'}],role='triage')
    return {'ok':True,'response':answer.get('content',''),'model':meta['model']}

@app.post('/api/auth/start')
def start_auth():
    if auth['running']:
        return {'ok':True}
    auth.update(running=True,output='')
    def run():
        try:
            p=subprocess.Popen([sys.executable,'-u',str(skill_dir('slai-email')/'scripts/auth.py')],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace')
            for line in p.stdout:
                auth['output']=(auth['output']+line)[-12000:]
            p.wait()
        except Exception as error:
            auth['output']=str(error)
        finally:
            auth['running']=False
    threading.Thread(target=run,daemon=True).start()
    return {'ok':True}

@app.get('/api/acceptance')
def acceptance():
    return json.loads((ROOT/'acceptance.json').read_text(encoding='utf-8'))


# Local knowledge is subject to the same session and CSRF controls as jobs.
@app.get('/api/knowledge')
def knowledge_list(q:str='',scope:str='',status:str='all'):
    from .knowledge import Knowledge
    if status not in {'all','active','suggested'}:
        raise ValueError('Invalid knowledge filter.')
    if q:
        rows=Knowledge(store).search(q,scope,limit=50,include_targeted=True)
        if status=='suggested':
            rows=[]
    else:
        rows=store.rows("SELECT * FROM knowledge WHERE status IN ('active','suggested') ORDER BY updated DESC LIMIT 500")
        if scope:
            rows=[r for r in rows if r['scope'] in {'global',scope}]
    targets={r['knowledge_id']:{'source':r['source'],'thread_id':r['thread_id']} for r in store.rows('SELECT * FROM knowledge_targets')}
    return [{**r,'target':targets.get(r['id'],{})} for r in rows if status=='all' or r['status']==status]

@app.post('/api/knowledge')
async def knowledge_create(request:Request):
    from .knowledge import Knowledge
    return Knowledge(store).save(await request.json(),user=True)

@app.post('/api/knowledge/{id}')
async def knowledge_change(id,request:Request):
    from .knowledge import Knowledge
    body=await request.json()
    k=Knowledge(store)
    if not store.one("SELECT id FROM knowledge WHERE id=? AND status<>'forgotten'",(id,)):
        raise HTTPException(404,'Knowledge entry not found')
    action=body.get('action')
    if action=='accept':
        k.accept(id)
    elif action=='forget':
        k.forget(id)
    elif action=='feedback':
        k.feedback(id,body.get('helpful'))
    elif action=='edit':
        return k.save(body['entry'],user=True,id=id)
    else:
        raise ValueError('Invalid knowledge action.')
    return {'ok':True}


@app.get('/api/atlassian')
def atlassian_status():
    return {'check':store.state('atlassian_check',{'ok':False,'output':'Run Test Atlassian connection to verify credentials and access.'}),
            'setup_help':(ROOT/'docs/ATLASSIAN_SETUP.txt').read_text(encoding='utf-8'),
            'config_path':str(Path.home()/'.atlassian/config.json')}

@app.post('/api/atlassian/test')
async def atlassian_test():
    import asyncio
    from .integrations import run_skill
    def check():
        try:
            result=run_skill('slai-atlassian','atlassian.py',['check'])
            value={'ok':isinstance(result,dict) and result.get('jira')=='ok' and result.get('confluence')=='ok','output':json.dumps(result,indent=2) if isinstance(result,dict) else result,'checked':now()}
        except RuntimeError as error:
            value={'ok':False,'output':str(error),'checked':now()}
        store.set_state('atlassian_check',value)
        return value
    return await asyncio.to_thread(check)

@app.post('/api/atlassian/setup')
def atlassian_setup():
    # Only the user types the secret in their own shell; no credential API.
    cli=skill_dir('slai-atlassian')/'scripts/atlassian.py'
    subprocess.Popen(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(ROOT/'Setup-Atlassian.ps1'),'-Cli',str(cli),'-Python',sys.executable],
        creationflags=subprocess.CREATE_NEW_CONSOLE)
    return {'ok':True}

app.mount('/static',StaticFiles(directory=ROOT/'web'),name='static')
