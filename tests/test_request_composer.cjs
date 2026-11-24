const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('web/app.js','utf8');

function setup(saved=null,{storageFails=false}={}){
 const storage=new Map(saved===null?[]:[['cos-open-delegated-job',saved]]),requests=[],opened=[],notices=[],refreshes=[];
 const nodes={'#request-form':{},'#attachments':{files:[],value:''},'#file-chips':{textContent:''},
  '#request':{value:'Prepare a comparison'},'#project':{value:'Example'},'#submit':{disabled:false},'#open-delegated-job':{}};
 const ctx={localStorage:{getItem:key=>{if(storageFails)throw Error('Unavailable');return storage.get(key)??null},
  setItem:(key,value)=>{if(storageFails)throw Error('Unavailable');storage.set(key,value)}},
  $:id=>nodes[id],state:{settings:{projects:{Example:'.'}}},esc:x=>x,btn:(text,attrs)=>'<button '+attrs+'>'+text+'</button>',handle:fn=>fn,
  api:async(path,body)=>{requests.push({path,body});return {id:'new-job'}},
  openJob:async id=>opened.push(id),refresh:async force=>refreshes.push(force),toast:text=>notices.push(text)};
 vm.createContext(ctx);
 vm.runInContext(source.slice(source.indexOf('let openDelegatedJob='),source.indexOf('let returnedResults=')),ctx);
 vm.runInContext(source.slice(source.indexOf('function composer('),source.indexOf('function renderQueueControl(')),ctx);
 vm.runInContext(source.slice(source.indexOf('function bindRequestComposer('),source.indexOf('const sourceLoads=')),ctx);
 nodes['#open-delegated-job'].checked=ctx.composer().includes('id="open-delegated-job" checked');
 ctx.bindRequestComposer();
 return {ctx,nodes,storage,requests,opened,notices,refreshes};
}
const event={preventDefault(){}};

test('initial delegation opens details by default and refreshes the tracker',async()=>{
 const s=setup();
 assert.equal(s.nodes['#open-delegated-job'].checked,true);
 await s.nodes['#request-form'].onsubmit(event);
 assert.deepEqual(s.opened,['new-job']);
 assert.deepEqual(s.refreshes,[true]);
 assert.equal(s.requests.length,1);
 assert.equal(s.requests[0].path,'jobs');
 assert.equal(s.requests[0].body.request,'Prepare a comparison');
 assert.equal(s.requests[0].body.project,'Example');
 assert.equal(s.nodes['#submit'].disabled,false);
});

test('opting out remembers the choice and submits without opening details',async()=>{
 const s=setup();
 s.nodes['#open-delegated-job'].checked=false;
 s.nodes['#open-delegated-job'].onchange({target:s.nodes['#open-delegated-job']});
 assert.equal(s.storage.get('cos-open-delegated-job'),'false');
 assert.doesNotMatch(s.ctx.composer(),/id="open-delegated-job" checked/);
 assert.equal(setup(s.storage.get('cos-open-delegated-job')).nodes['#open-delegated-job'].checked,false);
 s.nodes['#file-chips'].textContent='old attachment';
 await s.nodes['#request-form'].onsubmit(event);
 assert.equal(s.requests.length,1);
 assert.deepEqual(s.opened,[]);
 assert.deepEqual(s.refreshes,[true]);
 assert.equal(s.notices.length,1);
 assert.equal(s.nodes['#request'].value,'');
 assert.equal(s.nodes['#file-chips'].textContent,'');
 s.nodes['#open-delegated-job'].checked=true;
 s.nodes['#open-delegated-job'].onchange({target:s.nodes['#open-delegated-job']});
 assert.equal(s.storage.get('cos-open-delegated-job'),'true');
 await s.nodes['#request-form'].onsubmit(event);
 assert.deepEqual(s.opened,['new-job']);
});

test('blocked browser storage still lets the owner choose to keep details closed',async()=>{
 const s=setup(null,{storageFails:true});
 assert.equal(s.nodes['#open-delegated-job'].checked,true);
 s.nodes['#open-delegated-job'].checked=false;
 s.nodes['#open-delegated-job'].onchange({target:s.nodes['#open-delegated-job']});
 await s.nodes['#request-form'].onsubmit(event);
 assert.deepEqual(s.opened,[]);
 assert.equal(s.requests.length,1);
});

test('failed delegation preserves the draft and choice for retry',async()=>{
 const s=setup('false');
 s.ctx.api=async()=>{throw Error('Server unavailable')};
 s.nodes['#file-chips'].textContent='notes.txt';
 await assert.rejects(s.nodes['#request-form'].onsubmit(event),/Server unavailable/);
 assert.equal(s.nodes['#request'].value,'Prepare a comparison');
 assert.equal(s.nodes['#file-chips'].textContent,'notes.txt');
 assert.equal(s.nodes['#open-delegated-job'].checked,false);
 assert.equal(s.nodes['#submit'].disabled,false);
 assert.deepEqual(s.opened,[]);
 assert.deepEqual(s.refreshes,[]);
 assert.deepEqual(s.notices,[]);
});
