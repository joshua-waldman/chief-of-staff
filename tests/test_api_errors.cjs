const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('web/app.js','utf8');

function api(response){
 const ctx={token:'session',fetch:async()=>response};
 vm.createContext(ctx);
 vm.runInContext(source.slice(source.indexOf('async function api('),source.indexOf('\nfunction toast')),ctx);
 return ctx.api;
}

test('plain text server failures identify the endpoint and HTTP status',async()=>{
 const request=api({ok:false,status:500,json:async()=>{throw new SyntaxError('Invalid JSON')}});
 await assert.rejects(request('atlassian'),/\/api\/atlassian failed \(HTTP 500\).*server logs/);
});

test('expired sessions retain refresh guidance even with a non-JSON response',async()=>{
 const request=api({ok:false,status:401,json:async()=>{throw new SyntaxError('Invalid JSON')}});
 await assert.rejects(request('connections'),/server restarted.*Refresh/);
});

test('JSON errors and successful responses remain supported',async()=>{
 await assert.rejects(api({ok:false,status:400,json:async()=>({error:'Install the skill'})})('atlassian'),/Install the skill/);
 const result=await api({ok:true,status:200,json:async()=>({ok:true})})('atlassian');
 assert.equal(result.ok,true);
});

test('malformed successful responses report an invalid server response',async()=>{
 await assert.rejects(api({ok:true,status:200,json:async()=>{throw new SyntaxError('Invalid JSON')}})('connections'),/HTTP 200.*invalid response/);
});
