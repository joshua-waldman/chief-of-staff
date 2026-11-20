const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('web/app.js','utf8');

function context(warm){
 const ctx={con:{warm_start:warm},state:{settings:{queue_paused:false}},
  esc:x=>String(x??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),
  date:x=>x,btn:(label,attrs)=>'<button '+attrs+'>'+label+'</button>'};
 vm.createContext(ctx);
 vm.runInContext(source.slice(source.indexOf('function warmStartView('),source.indexOf('function connectionView(')),ctx);
 return ctx;
}

test('warm start explains context boundary and offers explicit opt in',()=>{
 const ctx=context({state:'not_started'}),html=ctx.warmStartView();
 assert.match(html,/Historical messages never create tasks/);
 assert.match(html,/email previews and Teams text/);
 assert.match(html,/Start 30-day warm start/);
 assert.match(html,/data-warm-action="start"/);
});

test('running and partial imports show progress, pause and coverage gaps',()=>{
 const ctx=context({state:'running',records:12,summarized:2,batches:3,day:1});
 assert.match(ctx.warmStartView(),/12 records/);
 assert.match(ctx.warmStartView(),/Pause after current batch/);
 ctx.state.settings.queue_paused=true;
 assert.match(ctx.warmStartView(),/Waiting for Connect/);
 ctx.con.warm_start={state:'partial',limited:true,limitations:['<script>']};
 const html=ctx.warmStartView();
 assert.match(html,/Coverage is partial/);
 assert.match(html,/&lt;script>/);
 assert.doesNotMatch(html,/<script>/);
 assert.doesNotMatch(html,/data-warm-action/);
});
