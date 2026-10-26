const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('web/commitments.js','utf8');

function context(){
 const ctx={URLSearchParams,state:{commitments_attention:2},esc:x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),date:x=>x,intro:(a,b)=>a+b,
  btn:(text,attrs)=>'<button '+attrs+'>'+text+'</button>',input:(id,label,value)=>'<label>'+label+'</label><input id="'+id+'" value="'+String(value).replace(/</g,'&lt;')+'">',render:()=>{},$:()=>null};
 ctx.readable=(label,value)=>label+': '+ctx.esc(value);
 vm.createContext(ctx);vm.runInContext(source,ctx);return ctx;
}

function record(status='proposed'){
 return {id:'one',owner:'Alex',deliverable:'Prepare <script>release plan',scope:'global',status,version:1,deadline_certainty:'inferred',deadline_text:'Friday',deadline_date:'2026-10-30',deadline_basis:'Inferred from meeting context',completion_pending:true,
  evidence:[{id:'evidence',kind:'completion',source_job:'job',source_file_id:'file',source_url:'https://example.com/recording',meeting_date:'2026-10-23',source_timestamp:'00:02:00',excerpt:'<script>untrusted source'}],history:[],possible_matches:[]};
}

test('uncertain deadlines and proposals remain distinct from confirmed work',()=>{
 const ctx=context();const html=ctx.commitmentDetailView(record());
 assert.match(html,/Inferred date — confirm/);
 assert.match(html,/Confirm assignment/);
 assert.ok(!html.includes('Confirm completed'));
 assert.ok(!html.includes('<script>'));
 assert.match(html,/&lt;script&gt;untrusted source/);
 assert.match(html,/external|Sending, scheduling/i);
 assert.match(ctx.commitmentAttention(),/Meeting commitments \(2\)/);
});

test('open records expose evidence review and closed records require reopening',()=>{
 const ctx=context();const open=ctx.commitmentDetailView(record('open'));
 assert.match(open,/Confirm completed/);
 assert.match(open,/Use my verification note instead/);
 const completed=ctx.commitmentDetailView(record('completed'));
 assert.match(completed,/Reopen commitment/);
 assert.ok(!completed.includes('Save corrections'));
 assert.ok(!completed.includes('Confirm completed'));
});

test('merging requires an explicit comparison and preserves canonical fields',()=>{
 const ctx=context();ctx.sample=record('open');
 vm.runInContext('commitmentMergeTarget={...sample,id:"canonical",evidence:[]}',ctx);
 const html=ctx.commitmentDetailView(ctx.sample);
 assert.match(html,/Merge into this record/);
 assert.match(html,/keeps its fields and status/);
 assert.match(html,/Canonical record to keep/);
});

test('background refresh does not silently advance the version being reviewed',async()=>{
 const ctx=context();ctx.sample=record('open');
 vm.runInContext('commitmentDetail=sample',ctx);
 const calls=[];ctx.api=async path=>{calls.push(path);return {items:[],total:0,offset:0,limit:25}};
 await ctx.loadCommitments();
 assert.equal(calls.length,1);
 assert.equal(vm.runInContext('commitmentDetail.version',ctx),1);
});

test('completion submits exactly the reviewed version and selected evidence',async()=>{
 const ctx=context();ctx.sample=record('open');
 vm.runInContext('commitmentDetail=sample;loadCommitments=async()=>{}',ctx);
 ctx.$=selector=>selector==='#cm-note'?{value:'Verified the deliverable'}:selector==='#cm-completion-evidence'?{value:'evidence'}:null;
 const calls=[];ctx.api=async(path,args)=>{calls.push({path,args});return {...ctx.sample,status:'completed',version:2}};
 ctx.refresh=async()=>{};ctx.toast=()=>{};
 await ctx.reviewCommitment('complete');
 assert.equal(calls[0].path,'commitments/one');
 assert.equal(calls[0].args.expected_version,1);
 assert.equal(calls[0].args.evidence_id,'evidence');
 assert.equal(calls[0].args.action,'complete');
});
