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

function jobRefreshContext(){
 let resolveRequest,writes=0;
 const dialog={open:true,scrollTop:100};
 const detail={set innerHTML(value){writes++;this.html=value;dialog.scrollTop=0}};
 const refreshButton={};
 const ctx={handle:fn=>fn,selected:'job',document:{activeElement:null,querySelector:()=>null,querySelectorAll:()=>[]},
  $:s=>({'#job-dialog':dialog,'#detail':detail,'#refresh-job':refreshButton}[s]||null),
  api:()=>new Promise(resolve=>resolveRequest=resolve),
  captureView:root=>({y:root.scrollTop}),restoreView:(root,saved)=>root.scrollTop=saved.y,
  esc:x=>String(x??''),badge:()=>'',date:()=>'',readable:()=>'',reviewView:()=>'',returnedResultView:()=>'',btn:()=>'',bindReview:()=>{}};
 vm.createContext(ctx);
 vm.runInContext(source.slice(source.indexOf('function itemTiming('),source.indexOf('function table(')),ctx);
 vm.runInContext(source.slice(source.indexOf('function capacityView('),source.indexOf('async function navigate(')),ctx);
 return {ctx,dialog,detail,writes:()=>writes,respond:(title='Title',extra={})=>resolveRequest({id:'job',title,display_name:title,org:'Engineering',status:'completed',approvals:[],files:[],children:[],events:[],...extra})};
}

test('unchanged main content keeps its DOM and scroll while header status updates',()=>{
 let writes=0;
 const main={dataset:{view:'acceptance'},scrollTop:0,set innerHTML(value){writes++;this.scrollTop=0}};
 const pill={},ctx={returnedResults:[],view:'acceptance',tests:[],names:{acceptance:'Acceptance tests'},
  state:{settings:{},monitor:{state:'paused'}},document:{querySelectorAll:()=>[]},
  $:s=>s==='#main'?main:s==='#search'?null:pill,
  renderQueueControl:()=>{},intro:()=>'',bind:()=>{},
  captureView:root=>({y:root.scrollTop}),restoreView:(root,saved)=>root.scrollTop=saved.y};
 vm.createContext(ctx);
 vm.runInContext(source.slice(source.indexOf('function render(){'),source.indexOf('function input(')),ctx);
 ctx.render();main.scrollTop=600;
 ctx.state.monitor.state='watching';ctx.render();
 assert.equal(writes,1);
 assert.equal(main.scrollTop,600);
 assert.equal(pill.textContent,'● Monitoring active');
});

test('job refresh preserves scrolling performed while its request is in flight',async()=>{
 const s=jobRefreshContext(),pending=s.ctx.openJob('job',{background:true});
 s.dialog.scrollTop=640;s.respond();await pending;
 assert.equal(s.dialog.scrollTop,640);
 assert.equal(s.writes(),1);
 const unchanged=s.ctx.openJob('job',{background:true});
 s.dialog.scrollTop=810;s.respond();await unchanged;
 assert.equal(s.writes(),1,'unchanged content must keep the existing DOM');
 assert.equal(s.dialog.scrollTop,810);
 const changed=s.ctx.openJob('job',{background:true});
 s.dialog.scrollTop=920;s.respond('Updated title');await changed;
 assert.equal(s.writes(),2);
 assert.equal(s.dialog.scrollTop,920);
});

test('pending background job refresh respects closing, switching jobs and starting a draft',async()=>{
 for(const change of [s=>{s.dialog.open=false;s.ctx.selected=null},s=>s.ctx.selected='another-job',s=>s.ctx.document.activeElement={tagName:'TEXTAREA'}]){
  const s=jobRefreshContext(),pending=s.ctx.openJob('job',{background:true});
  change(s);s.respond();await pending;
  assert.equal(s.writes(),0);
 }
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
 assert.match(source,/view==='attention'\?resultsNotice\(\)\+monitoringAlert\(\)/);
});


test('resolved job questions disappear from current status while error history remains',async()=>{
 const nodes={'#detail':{innerHTML:''},'#job-dialog':{open:false,showModal(){this.open=true}},'#refresh-job':{}};
 const job={id:'research',org:'Technology',title:'Research',status:'running',project:'',created:'now',request:'Research',brief:{status_summary:'Continuing'},question:'Old access error',approvals:[],files:[],children:[],events:[{id:1,kind:'tool_error',body:'Old access error',created:'then'}]};
 const ctx={handle:fn=>fn,selected:null,api:async()=>job,$:s=>nodes[s]||null,esc:x=>String(x??''),badge:x=>x,date:x=>x,readable:()=>'',btn:()=>'',reviewView:()=>'',returnedResultView:()=>'',bindReview:()=>{},document:{querySelectorAll:()=>[]}};
 vm.createContext(ctx);
 vm.runInContext(source.slice(source.indexOf('function itemTiming('),source.indexOf('function table(')),ctx);
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


test('deliverables stay visible and recovery downloads are collapsed separately',()=>{
 const ctx={esc:x=>String(x??'').replace(/</g,'&lt;')};
 vm.createContext(ctx);
 vm.runInContext(source.slice(source.indexOf('function jobFilesView('),source.indexOf('async function openJob(')),ctx);
 assert.equal(ctx.jobFilesView([]),'');
 const files=[{id:'report',name:'history-report.txt',purpose:'deliverable'},
  {id:'archive',name:'history-123.txt',purpose:'recovery'},
  {id:'checkpoint',name:'checkpoint-456.txt',purpose:'recovery'}];
 const html=ctx.jobFilesView(files);
 assert.match(html,/<h3>Deliverables<\/h3>/);
 assert.ok(html.indexOf('history-report.txt')<html.indexOf('<details'));
 assert.match(html,/<details id="recovery-files"><summary>Internal recovery files \(2\)<\/summary>/);
 assert.ok(html.indexOf('history-123.txt')>html.indexOf('<details'));
 assert.match(html,/href="\/api\/files\/archive"/);
 const recoveryOnly=ctx.jobFilesView(files.slice(1));
 assert.ok(!recoveryOnly.includes('<h3>Deliverables'));
 assert.ok(!recoveryOnly.includes(' open'));
 assert.match(ctx.jobFilesView([{id:'old',name:'Report <draft>.txt'}]),/Report &lt;draft>.txt/);
});

test('job detail heading uses the escaped display name instead of the source ID or email subject',async()=>{
 const s=jobRefreshContext();
 s.ctx.esc=x=>String(x??'').replace(/</g,'&lt;').replace(/>/g,'&gt;');
 const pending=s.ctx.openJob('job',{background:true});
 s.respond('Teams conversation: 19:meeting_opaque@thread.v2',{display_name:'Launch <readiness>',brief:{email_subject:'Long original source subject'}});
 await pending;
 assert.match(s.detail.html,/<h2 class="detail-title">Launch &lt;readiness&gt;<\/h2>/);
 assert.ok(!s.detail.html.includes('19:meeting_opaque'));
});

test('job details display inferred time sensitivity without a deadline',async()=>{
 const s=jobRefreshContext();
 s.ctx.readable=(label,value)=>label+': '+s.ctx.esc(value);
 const pending=s.ctx.openJob('job',{background:true});
 s.respond('Launch decision',{status:'waiting_input',brief:{time_sensitive:'yes',timing_reason:'Assessment: dependent team is blocked.'}});
 await pending;
 assert.match(s.detail.html,/Time sensitive/);
 assert.match(s.detail.html,/Timing rationale: Assessment: dependent team is blocked/);
 assert.ok(!s.detail.html.includes('Overdue'));
});


test('opening another job shows loading immediately and discards the older response',async()=>{
 const s=jobRefreshContext(),responses={};
 s.ctx.api=path=>new Promise(resolve=>responses[path]=resolve);
 const slow=s.ctx.openJob('slow');
 assert.match(s.detail.html,/Loading job/);
 assert.equal(s.ctx.selected,'slow');
 const fast=s.ctx.openJob('fast');
 const job=id=>({id,display_name:id,org:'Engineering',status:'completed',approvals:[],files:[],children:[],events:[]});
 responses['jobs/fast?compact=true'](job('fast'));await fast;
 assert.match(s.detail.html,/fast/);
 const writes=s.writes();
 responses['jobs/slow?compact=true'](job('slow'));await slow;
 assert.equal(s.writes(),writes);
 assert.equal(s.ctx.selected,'fast');
});


test('accepted approvals show execution progress without another approve button',async()=>{
 const s=jobRefreshContext(),pending=s.ctx.openJob('job',{background:true});
 s.respond('Approved action',{status:'awaiting_approval',approvals:[{id:'approved-action',status:'executing',tool:'run_command'}]});
 await pending;
 assert.match(s.detail.html,/Approved action in progress/);
 assert.match(s.detail.html,/run_command is running/);
 assert.ok(!s.detail.html.includes('data-approve="approved-action"'));
 assert.ok(!s.detail.html.includes('data-resolve="approved-action"'));
});

test('uncertain actions are checked by COS without manual outcome controls or questions',async()=>{
 const s=jobRefreshContext(),pending=s.ctx.openJob('job');
 s.respond('Checking',{status:'blocked',question:'Verify the external outcome',approvals:[{id:'action',tool:'send_teams',status:'uncertain',args:'{}'}]});
 await pending;
 assert.match(s.detail.html,/COS is checking the action outcome/);
 assert.doesNotMatch(s.detail.html,/data-resolve|Verified:|Decision or information needed|id="answer-form"/);
});

test('inconclusive outcomes retain evidence and do not require owner verification',async()=>{
 const s=jobRefreshContext(),pending=s.ctx.openJob('job');
 s.respond('Continuing',{status:'queued',approvals:[{id:'action',tool:'send_teams',status:'verification_inconclusive',verification_evidence:'No authoritative receipt',args:'{}'}]});
 await pending;
 assert.match(s.detail.html,/Action outcome could not be confirmed/);
 assert.match(s.detail.html,/No authoritative receipt/);
 assert.doesNotMatch(s.detail.html,/data-resolve|Verified:|Approve exact action/);
});
