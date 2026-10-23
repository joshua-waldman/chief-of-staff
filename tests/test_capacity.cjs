const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('web/app.js','utf8');
const ctx={jobs:[],esc:x=>String(x??'').replaceAll('<','&lt;'),btn:(label,attrs)=>'<button '+attrs+'>'+label+'</button>',Date:{now:()=>1000000}};
vm.createContext(ctx);
vm.runInContext(source.slice(source.indexOf('function capacityAlert('),source.indexOf('async function openJob(')),ctx);
test('capacity waiting shows scheduled delay and escapes gateway details',()=>{
 const html=ctx.capacityView({status:'waiting_capacity',capacity:{model:'model',retry_at:1030,detail:'<script>untrusted</script>'}});
 assert.match(html,/in 30 seconds/);
 assert.match(html,/Retry now/);
 assert.match(html,/Gateway diagnostics/);
 assert.ok(!html.includes('<script>'));
 assert.ok(!html.includes('Decision or information needed'));
 assert.equal(ctx.capacityView({status:'completed'}),'');
});
test('persistent throttling has one shared alert and explains paused recovery',()=>{
 ctx.jobs=[{status:'capacity_paused'},{status:'capacity_paused'}];
 const html=ctx.capacityAlert();
 assert.equal((html.match(/role="alert"/g)||[]).length,1);
 assert.match(html,/2 job/);
 assert.match(ctx.capacityView({status:'capacity_paused'}),/Automatic retries reached/);
 ctx.jobs=[];
 assert.equal(ctx.capacityAlert(),'');
});
