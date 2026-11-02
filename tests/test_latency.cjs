const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('web/app.js','utf8');
const deferred=()=>{let resolve,reject;const promise=new Promise((r,j)=>{resolve=r;reject=j});return {promise,resolve,reject}};

function refreshContext(){
 const requests=[];
 const ctx={view:'overview',document:{activeElement:null},$:()=>({}),renderQueueControl(){},notifyResults(){},render(){},
  api(path){const request=deferred();requests.push({path,...request});return request.promise}};
 vm.createContext(ctx);
 vm.runInContext(source.slice(source.indexOf('async function refresh('),source.indexOf('\ndocument.querySelectorAll',source.indexOf('async function refresh('))),ctx);
 return {ctx,requests,respond(start=0){requests[start].resolve({});requests[start+1].resolve([]);requests[start+2].resolve([])}};
}

test('simultaneous refreshes share requests and a mutation can require a fresh read',async()=>{
 const s=refreshContext();
 const first=s.ctx.refresh(),second=s.ctx.refresh(),forced=s.ctx.refresh(true);
 assert.equal(s.requests.length,3);
 assert.equal(s.requests[1].path,'jobs?compact=true');
 s.respond();await Promise.all([first,second]);
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(s.requests.length,6,'a forced read must follow the earlier polling read');
 s.respond(3);await forced;
 assert.equal(s.ctx.refresh.pending,null);
});

test('buttons show progress immediately, suppress repeated clicks, and recover after errors',async()=>{
 const action=deferred(),errors=[];
 const ctx={toast:message=>errors.push(message)};
 vm.createContext(ctx);
 vm.runInContext(source.slice(source.indexOf('function handle('),source.indexOf('function bind(){')),ctx);
 const button={tagName:'BUTTON',childElementCount:0,textContent:'Approve',disabled:false,setAttribute(key,value){this[key]=value},removeAttribute(key){delete this[key]}};
 let calls=0;
 const callback=ctx.handle(async()=>{calls++;await action.promise;throw Error('Action failed')});
 const first=callback({currentTarget:button});
 assert.equal(button.disabled,true);
 assert.equal(button.textContent,'Working…');
 assert.equal(button['aria-busy'],'true');
 await callback({currentTarget:button});assert.equal(calls,1);
 action.resolve();await first;
 assert.equal(button.disabled,false);
 assert.equal(button.textContent,'Approve');
 assert.equal(button['aria-busy'],undefined);
 assert.equal(ctx.handle.active,0);
 assert.deepEqual(errors,['Action failed']);
 await ctx.handle(async()=>{button.textContent='Connect'})({currentTarget:button});
 assert.equal(button.textContent,'Connect','the action may update its label to reflect its new state');
});


test('a failed earlier poll cannot suppress the refresh after a successful action',async()=>{
 const s=refreshContext();
 const poll=s.ctx.refresh();
 const failed=assert.rejects(poll,/Connection lost/);
 const action=s.ctx.refresh(true);
 s.requests[0].reject(Error('Connection lost'));
 await failed;await new Promise(resolve=>setImmediate(resolve));
 assert.equal(s.requests.length,6);
 s.respond(3);await action;
});

test('navigation keeps nested counters alive during and after asynchronous actions',async()=>{
 for(const id of ['results-count','attention-count']){
  const action=deferred(),errors=[],counter={id,textContent:'21'};
  let children=[{textContent:'Ready for review'},counter];
  const button={tagName:'BUTTON',disabled:false,
   get childElementCount(){return children.length},
   get textContent(){return children.map(child=>child.textContent).join('')},
   set textContent(value){children=[{textContent:value}]},
   setAttribute(key,value){this[key]=value},removeAttribute(key){delete this[key]}};
  const ctx={toast:message=>errors.push(message)};
  vm.createContext(ctx);
  vm.runInContext(source.slice(source.indexOf('function handle('),source.indexOf('function bind(){')),ctx);
  const click=ctx.handle(async()=>{
   children.find(child=>child.id===id).textContent='22';
   await action.promise;
   children.find(child=>child.id===id).textContent='23';
  })({currentTarget:button});
  assert.equal(button.disabled,true);
  assert.equal(children.find(child=>child.id===id),counter);
  assert.equal(counter.textContent,'22');
  action.resolve();await click;
  assert.deepEqual(errors,[]);
  assert.equal(children.find(child=>child.id===id),counter);
  assert.equal(counter.textContent,'23');
  assert.equal(button.disabled,false);
  assert.equal(button['aria-busy'],undefined);
 }
});
