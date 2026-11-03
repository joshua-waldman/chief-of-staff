const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('web/app.js','utf8');
function context(){
 const ctx={};vm.createContext(ctx);
 vm.runInContext(source.slice(source.indexOf('const esc='),source.indexOf('const esc=')+source.slice(source.indexOf('const esc=')).indexOf('\n')),ctx);
 vm.runInContext(source.slice(source.indexOf('function memoryBinding('),source.indexOf('function bindMemoryCards(')),ctx);
 vm.runInContext(source.slice(source.indexOf('function reviewView(')),ctx);
 ctx.btn=(label,attrs)=>'<button '+attrs+'>'+label+'</button>';
 return ctx;
}
function job(candidates=[]){return {org:'Communications',status:'completed',approvals:[],sender_candidates:candidates};}

test('sender selection previews exact identity and escapes display labels',()=>{
 const ctx=context(),j=job([{source:'teams',sender_id:'28:bot-id',label:'<img onerror=alert(1)>'}]);
 const html=ctx.reviewView(j);
 assert.match(html,/One sender across conversations/);
 assert.match(html,/28:bot-id/);
 assert.match(html,/&lt;img/);
 assert.ok(!html.includes('<img'));
 assert.match(html,/&quot;sender_id&quot;:&quot;28:bot-id&quot;/);
 assert.match(html,/Conditions and exceptions/);
 assert.ok(!html.includes('conversationsS'));
 assert.match(ctx.reviewView(job()),/<option value="sender" disabled>/);
 assert.match(ctx.reviewView(job()),/Load original messages above/);
});

test('editing bindings keeps general, conversation and sender scopes distinct',()=>{
 const ctx=context();
 const plain=x=>JSON.parse(JSON.stringify(x));
 assert.deepEqual(plain(ctx.memoryBinding('email-sender',' ALERTS@example.test ')),{source:'email',sender_id:'ALERTS@example.test'});
 assert.deepEqual(plain(ctx.memoryBinding('teams','topic:one')),{source:'teams',thread_id:'topic:one'});
 assert.deepEqual(plain(ctx.memoryBinding('','')),{});
});

test('review submits the selected identity and only requires it in sender scope',async()=>{
 const ctx=context(),nodes={};
 for(const id of ['review-form','review-remember','review-rule-fields','review-applies','review-target','review-sender-field','review-sender','send-review','review-message','review-guidance'])nodes['#'+id]={value:'',checked:false,reset(){}};
 ctx.$=s=>nodes[s]||null;ctx.document={querySelector:()=>null};ctx.handle=fn=>fn;
 const calls=[];ctx.api=async(path,payload)=>{calls.push(payload);return {memory_id:'saved'};};
 ctx.openJob=async()=>{};ctx.refresh=async()=>{};ctx.toast=()=>{};
 ctx.bindReview('job');
 nodes['#review-remember'].checked=true;nodes['#review-target'].value='sender';
 nodes['#review-target'].onchange();
 assert.equal(nodes['#review-sender-field'].hidden,false);
 assert.equal(nodes['#review-sender'].required,true);
 nodes['#review-sender'].value=JSON.stringify({source:'email',sender_id:'alerts@example.test'});
 nodes['#review-message'].value='Ignore routine updates';
 nodes['#review-applies'].value='Unless assigned work';
 await nodes['#review-form'].onsubmit({preventDefault(){}});
 assert.equal(calls[0].remember_sender.sender_id,'alerts@example.test');
 assert.equal(calls[0].applies_to,'Unless assigned work');
 nodes['#review-target'].value='conversation';nodes['#review-target'].onchange();
 assert.equal(nodes['#review-sender-field'].hidden,true);
 assert.equal(nodes['#review-sender'].required,false);
});
