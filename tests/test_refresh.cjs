const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('web/app.js','utf8');
const context={window:{scrollX:12,scrollY:40,scrollTo(x,y){this.scrollX=x;this.scrollY=y}},document:{getElementById:id=>context.nodes.find(n=>n.id===id)},nodes:[]};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function captureView('),source.indexOf('function readable(')),context);
function element(id,extra={}){return {id,tagName:'DIV',scrollLeft:0,scrollTop:0,style:{},...extra}}
function surface(count){
 const field=element('answer',{tagName:'TEXTAREA',value:'My unsent draft',style:{height:'210px'}});
 const secret=element('secret',{tagName:'INPUT',type:'password',value:'never-copy'});
 const nested=element('event-body-5',{scrollTop:145});
 const detail=element('activity-details',{open:true,querySelector:()=>({textContent:'Full activity timeline ('+count+' events)'})});
 const root=element('job-dialog',{scrollTop:720,querySelectorAll:s=>s==='details'?[detail]:[field,secret,nested,detail],contains:n=>[field,secret,nested,detail].includes(n)});
 context.nodes=[root,field,secret,nested,detail];
 return {root,field,secret,nested,detail};
}
test('refresh preserves nested scroll, dialog scroll, expansion, draft and size',()=>{
 const old=surface(20),saved=context.captureView(old.root);
 const next=surface(21);
 next.root.scrollTop=0;next.nested.scrollTop=0;next.detail.open=false;next.field.value='';next.field.style.height='';next.secret.value='';
 context.restoreView(next.root,saved);
 assert.equal(next.root.scrollTop,720);
 assert.equal(next.nested.scrollTop,145);
 assert.equal(next.detail.open,true);
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
