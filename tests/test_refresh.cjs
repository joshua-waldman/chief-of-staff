const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('web/app.js','utf8');
test('original messages display full escaped text, metadata and honest preview labels',()=>{
 const ctx={sourceLoads:new Set(),sourceErrors:new Map(),esc:x=>String(x??'').replace(/</g,'&lt;'),readable:(label,value)=>label+': '+value,btn:()=>'<button>Load</button>'};
 vm.createContext(ctx);
 vm.runInContext(source.slice(source.indexOf('function sourceView('),source.indexOf('async function openJob(')),ctx);
 const body='First line\n'+'Long message '.repeat(3000)+'<script>THE END';
 const html=ctx.sourceView({id:'job',source_messages:[{sender:'Participant',received:'2026-01-01',body,complete:1},{body:'Short excerpt',complete:0}]});
 assert.ok(html.includes(body.replace('<','&lt;')));
 assert.match(html,/Full message/);
 assert.match(html,/Preview — full text not loaded/);
 assert.match(html,/Participant/);
 assert.match(html,/2026-01-01/);
 assert.ok(!html.includes('<script>'));
});
const context={window:{scrollX:12,scrollY:40,scrollTo(x,y){this.scrollX=x;this.scrollY=y}},document:{getElementById:id=>context.nodes.find(n=>n.id===id)},nodes:[]};
test('conversation shows newest messages first without changing stored order and collapses from either control',()=>{
 const summary={focus(options){this.focused=options},scrollIntoView(options){this.scrolled=options}};
 const conversation={open:true,querySelector:()=>summary};
 const ctx={sourceLoads:new Set(),sourceErrors:new Map(),esc:x=>String(x??''),readable:(label,value)=>label+': '+value,btn:(text,attrs)=>'<button '+attrs+'>'+text+'</button>',$:()=>conversation};
 vm.createContext(ctx);
 vm.runInContext(source.slice(source.indexOf('function sourceView('),source.indexOf('async function openJob(')),ctx);
 const messages=[{received:'2026-01-01T10:00:00Z',body:'Older message'},{received:'2026-01-02T10:00:00Z',body:'Newest message'},{body:'Undated message'}];
 const html=ctx.sourceView({id:'job',source_messages:messages});
 assert.ok(html.indexOf('Newest message')<html.indexOf('Older message'));
 assert.ok(html.indexOf('Older message')<html.indexOf('Undated message'));
 assert.equal(messages[0].body,'Older message');
 assert.match(html,/<details id="source-conversation"><summary/);
 assert.match(html,/Collapse conversation/);
 assert.ok(html.indexOf('Collapse conversation')<html.indexOf('Newest message'));
 assert.equal((html.match(/data-collapse-source/g)||[]).length,2);
 ctx.collapseSourceConversation();
 assert.equal(conversation.open,false);
 assert.equal(summary.focused.preventScroll,true);
 assert.equal(summary.scrolled.block,'nearest');
 const many=Array.from({length:10},(_,i)=>({received:'2026-01-'+String(i+1).padStart(2,'0'),body:'Message number '+i}));
 const limited=ctx.sourceView({id:'job',source_messages:many});
 assert.equal((limited.match(/class="source-message"/g)||[]).length,5);
 assert.ok(limited.includes('Message number 9'));
 assert.ok(!limited.includes('Message number 4'));
});
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function captureView('),source.indexOf('function readable(')),context);
function element(id,extra={}){return {id,tagName:'DIV',scrollLeft:0,scrollTop:0,style:{},...extra}}
function surface(count){
 const field=element('answer',{tagName:'TEXTAREA',value:'My unsent draft',style:{height:'210px'}});
 const secret=element('secret',{tagName:'INPUT',type:'password',value:'never-copy'});
 const nested=element('event-body-5',{scrollTop:145});
 const detail=element('activity-details',{open:true,querySelector:()=>({textContent:'Full activity timeline ('+count+' events)'})});
 const conversation=element('source-conversation',{open:false});
 const root=element('job-dialog',{scrollTop:720,querySelectorAll:s=>s==='details'?[detail,conversation]:[field,secret,nested,detail,conversation],contains:n=>[field,secret,nested,detail,conversation].includes(n)});
 context.nodes=[root,field,secret,nested,detail,conversation];
 return {root,field,secret,nested,detail,conversation};
}
test('refresh preserves nested scroll, dialog scroll, expansion, draft and size',()=>{
 const old=surface(20),saved=context.captureView(old.root);
 const next=surface(21);
 next.root.scrollTop=0;next.nested.scrollTop=0;next.detail.open=false;next.field.value='';next.field.style.height='';next.secret.value='';
 next.conversation.open=true;
 context.restoreView(next.root,saved);
 assert.equal(next.root.scrollTop,720);
 assert.equal(next.nested.scrollTop,145);
 assert.equal(next.detail.open,true);
 assert.equal(next.conversation.open,false);
 assert.equal(next.field.value,'My unsent draft');
 assert.equal(next.field.style.height,'210px');
 assert.equal(next.secret.value,'');
 assert.equal(context.window.scrollY,40);
});

test('monitor failure is visible, escaped, and clears on recovery',()=>{
 const alertContext={state:{monitor_attention:true,monitor:{error:'HTTP 400 <script>',sources:{email:{state:'watching'}}}},labels:{watching:'Watching'},esc:x=>String(x??'').replace(/</g,'&lt;'),date:x=>x,btn:()=>'<button>Settings</button>'};
 vm.createContext(alertContext);
 vm.runInContext(source.slice(source.indexOf('function monitoringAlert('),source.indexOf('function composer(')),alertContext);
 const html=alertContext.monitoringAlert();
 assert.match(html,/role="alert"/);
 assert.match(html,/HTTP 400 &lt;script>/);
 assert.match(html,/Email/);
 alertContext.state.monitor_attention=false;
 assert.equal(alertContext.monitoringAlert(),'');
 assert.match(source,/view==='attention'\?monitoringAlert\(\)/);
});


test('resolved job questions disappear from current status while error history remains',async()=>{
 const nodes={'#detail':{innerHTML:''},'#job-dialog':{open:false,showModal(){this.open=true}},'#refresh-job':{}};
 const job={id:'research',org:'Technology',title:'Research',status:'running',project:'',created:'now',request:'Research',brief:{status_summary:'Continuing'},question:'Old access error',approvals:[],files:[],children:[],events:[{id:1,kind:'tool_error',body:'Old access error',created:'then'}]};
 const ctx={selected:null,api:async()=>job,$:s=>nodes[s]||null,esc:x=>String(x??''),badge:x=>x,date:x=>x,readable:()=>'',btn:()=>'',reviewView:()=>'',bindReview:()=>{},document:{querySelectorAll:()=>[]}};
 vm.createContext(ctx);
 const start=source.indexOf('async function openJob(');
 const end=source.indexOf('\n}',start)+2;
 vm.runInContext(source.slice(source.indexOf('function capacityView('),end),ctx);
 for(const status of ['queued','running','completed','waiting_children','awaiting_approval','waiting_input','blocked','failed','waiting_capacity','capacity_paused']){
  job.status=status;ctx.selected=null;
  await ctx.openJob(job.id);
  const html=nodes['#detail'].innerHTML;
  assert.ok(html.includes('Old access error'),'historical errors remain available');
  assert.equal(html.includes('Decision or information needed'),['waiting_input','blocked','failed'].includes(status),status);
 }
});

test('queue control drains, reconnects, prevents duplicate clicks and recovers from failures',async()=>{
 const status={},button={},calls=[],notices=[];
 let release;
 const ctx={state:{settings:{queue_paused:false},queue_active:2},queueChanging:false,$:s=>s==='#queue-status'?status:button,handle:f=>f,toast:t=>notices.push(t),refresh:async()=>{},api:async(path,body)=>{calls.push([path,body]);await new Promise(r=>release=r);return body}};
 vm.createContext(ctx);
 vm.runInContext(source.slice(source.indexOf('function renderQueueControl('),source.indexOf('function render(){')),ctx);
 ctx.renderQueueControl();
 assert.equal(button.textContent,'Disconnect');
 assert.match(status.textContent,/Connected/);
 const disconnect=ctx.toggleQueue();
 assert.equal(button.disabled,true);
 await ctx.toggleQueue();
 assert.equal(calls.length,1);
 assert.equal(calls[0][1].queue_paused,true);
 release();await disconnect;
 assert.equal(button.disabled,false);
 assert.equal(button.textContent,'Connect');
 assert.match(status.textContent,/Finishing current agent steps \(2\)/);
 ctx.state.queue_active=0;ctx.renderQueueControl();
 assert.match(status.textContent,/Disconnected/);
 const connect=ctx.toggleQueue();release();await connect;
 assert.equal(calls[1][1].queue_paused,false);
 assert.equal(button.textContent,'Disconnect');
 ctx.api=async()=>{throw Error('Save failed')};
 await assert.rejects(ctx.toggleQueue(),/Save failed/);
 assert.equal(button.disabled,false);
 assert.equal(button.textContent,'Disconnect');
 assert.equal(ctx.state.settings.queue_paused,false);
});
