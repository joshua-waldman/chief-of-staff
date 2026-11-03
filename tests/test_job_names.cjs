const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('web/app.js','utf8');

function setup(){
 const requests=[],notices=[],refreshes=[],nodes={
  '#rename-job-form':{reset(){this.wasReset=true}},
  '#rename-job':{open:true},'#job-name':{value:'  My release name  '},
  '#cancel-rename':{},'#reset-job-name':{}
 };
 const ctx={$:id=>nodes[id],esc:x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
  btn:(text,attrs)=>'<button '+attrs+'>'+text+'</button>',handle:fn=>fn,
  api:async(path,body)=>requests.push({path,body}),toast:text=>notices.push(text),
  openJob:async id=>refreshes.push(id),refresh:async force=>refreshes.push(force)};
 vm.createContext(ctx);
 vm.runInContext(source.slice(source.indexOf('function renameJobView('),source.indexOf('function intro(')),ctx);
 ctx.bindJobRename({id:'job'});
 return {ctx,nodes,requests,notices,refreshes};
}

test('rename saves trimmed text, refreshes views, resets to automatic and cancels locally',async()=>{
 const s=setup();
 await s.nodes['#rename-job-form'].onsubmit({preventDefault(){}});
 assert.equal(s.requests[0].path,'jobs/job/rename');
 assert.equal(s.requests[0].body.name,'My release name');
 assert.deepEqual(s.refreshes,['job',true]);
 await s.nodes['#reset-job-name'].onclick();
 assert.equal(s.requests[1].body.name,'');
 s.nodes['#cancel-rename'].onclick();
 assert.equal(s.nodes['#rename-job'].open,false);
 assert.equal(s.nodes['#rename-job-form'].wasReset,true);
 assert.equal(s.requests.length,2);
 s.nodes['#job-name'].value='   ';
 await assert.rejects(s.nodes['#rename-job-form'].onsubmit({preventDefault(){}}),/Enter a job name/);
 assert.equal(s.requests.length,2);
});

test('rename escapes the name in the input and offers reset only for a user name',()=>{
 const {ctx}=setup();
 const html=ctx.renameJobView({display_name:'"<script>&',user_name:'custom'});
 assert.match(html,/value="&quot;&lt;script&gt;&amp;"/);
 assert.match(html,/Use automatic name/);
 assert.doesNotMatch(ctx.renameJobView({display_name:'Generated'}),/Use automatic name/);
});

test('failed rename preserves input for retry and does not announce success',async()=>{
 const s=setup();
 s.ctx.api=async()=>{throw Error('Save failed')};
 await assert.rejects(s.nodes['#rename-job-form'].onsubmit({preventDefault(){}}),/Save failed/);
 assert.equal(s.nodes['#job-name'].value,'  My release name  ');
 assert.deepEqual(s.notices,[]);
 assert.deepEqual(s.refreshes,[]);
});
