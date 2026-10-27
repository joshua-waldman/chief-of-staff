"""Reuse installed skills and Claude MCP definitions without copying credentials."""
from .processes import background_options
import asyncio
import json
import os
import subprocess
import sys
import time
import re
from .trace import emit,shape,fingerprint,failure
from pathlib import Path
from .config import settings, ROOT

def claude_plugins():
    p=Path.home()/'.claude/plugins/installed_plugins.json'
    if not p.exists():
        return []
    result=[]
    for name,items in json.loads(p.read_text(encoding='utf-8')).get('plugins',{}).items():
        if isinstance(items,dict):
            items=[items]
        for item in items:
            path=Path(item.get('installPath',''))
            if path.is_dir():
                result.append((name,path))
    return result

def skill_roots():
    return [ROOT/'data/skills',*map(Path,settings()['skill_roots']),*[p/'skills' for _,p in claude_plugins()]]

def skill_dir(name):
    for root in skill_roots():
        p = root/name
        if (p/'SKILL.md').exists():
            return p
    raise ValueError('Skill not found: '+name)


class CommunicationRejected(ValueError):
    """The bridge knows the external mutation did not execute."""


def communication(action, arguments):
    """Only the fixed bridge operation set is exposed, never arbitrary endpoints."""
    result=subprocess.run([sys.executable,'-m','cos.communication_bridge'],
        input=json.dumps({'action':action,'arguments':arguments}),cwd=ROOT,
        capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=120,**background_options())
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout)[-4000:])
    data=json.loads(result.stdout)
    if isinstance(data,dict) and data.get('bridge_error'):
        error=CommunicationRejected if data.get('not_executed') else RuntimeError
        raise error(data['bridge_error'])
    return data

def skill_inventory():
    found = {}
    for root in skill_roots():
        if root.exists():
            for p in root.glob('*/SKILL.md'):
                found.setdefault(p.parent.name, {'name':p.parent.name,'path':str(p)})
    return list(found.values())

def run_skill(name,script,args, timeout=90):
    p = skill_dir(name)/'scripts'/script
    emit('skill.started',{'skill':name,'script':p.name,'arguments':shape(args),'flags':[str(x) for x in args if re.fullmatch(r'--[a-z][a-z0-9-]{0,40}',str(x))]})
    started=time.monotonic()
    try:
        result = subprocess.run([sys.executable,str(p),*map(str,args)],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=timeout,env={**os.environ,'PYTHONIOENCODING':'utf-8'},**background_options())
    except Exception as error:
        emit('skill.failed',{'skill':name,'script':p.name,'duration_ms':round((time.monotonic()-started)*1000),**failure(error)})
        raise
    emit('skill.completed',{'skill':name,'script':p.name,'exit_code':result.returncode,'duration_ms':round((time.monotonic()-started)*1000),'stdout':fingerprint(result.stdout),'stderr':fingerprint(result.stderr)})
    if result.returncode:
        raise RuntimeError((result.stdout+'\n'+result.stderr)[-6000:])
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return result.stdout[-20000:]

def auth_status():
    try:
        return run_skill('slai-email','auth.py',['--status','--json'],30)
    except Exception:
        return {'authenticated':False,'nextAction':'sign_in','nextActionSummary':'Run the installed slai-email/scripts/auth.py locally to sign in.'}

def mcp_configs():
    result = {}
    paths=[*settings()['mcp_configs'],*[str(p/'.mcp.json') for _,p in claude_plugins() if (p/'.mcp.json').exists()]]
    for path in paths:
        p = Path(path)
        if p.exists():
            data = json.loads(p.read_text(encoding='utf-8-sig'))
            for name,config in data.get('mcpServers',{}).items():
                result[name] = {**config,'source':str(p)}
    return result

def mcp_inventory():
    return [{'name':n,'transport':'http' if c.get('url') else 'stdio','command':c.get('command',''),'source':c['source'],'enabled':n in settings()['mcp_enabled']} for n,c in mcp_configs().items()]

async def mcp_action(server,tool=None,args=None):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamablehttp_client
    from contextlib import AsyncExitStack
    if server not in settings()['mcp_enabled']:
        raise ValueError('Enable this MCP server in Connections before use.')
    c = mcp_configs().get(server)
    if not c:
        raise ValueError('Unknown MCP server')
    async with AsyncExitStack() as stack:
        if c.get('url'):
            from urllib.parse import urlparse
            host = urlparse(c['url']).hostname or ''
            if not (host.endswith('.amd.com') or host in {'localhost','127.0.0.1'}):
                raise ValueError('MCP HTTP endpoint must be AMD-owned or local.')
            read,write,*_ = await stack.enter_async_context(streamablehttp_client(c['url'],headers=c.get('headers')))
        else:
            params = StdioServerParameters(command=c['command'],args=c.get('args',[]),env={**os.environ,**c.get('env',{})})
            read,write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read,write))
        await session.initialize()
        result = await session.call_tool(tool,args or {}) if tool else await session.list_tools()
        return result.model_dump(mode='json')

def call_mcp(server,tool=None,args=None):
    return asyncio.run(asyncio.wait_for(mcp_action(server,tool,args),timeout=120))
