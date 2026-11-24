const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('web/app.js','utf8');

function context(){
 const nodes={};
 const ctx={nodes,$:s=>nodes[s]||null,document:{querySelectorAll:()=>[]},state:{settings:{
  models:{chief:'chief',director:'director',worker:'worker',triage:'fast'}}},
  gatewayCatalog:{models:[{id:'DeepSeek',mode:'chat',capabilities:{chat:true}},
   {id:'Decision',mode:'decision',capabilities:{chat:false,decision:true}},
   {id:'Audio <unsafe>',mode:'realtime',capabilities:{chat:false}}],error:'',stale:false},
  routingProfiles:{fast:['Fast','triage'],coding:['Coding','director']}};
 vm.createContext(ctx);
 vm.runInContext(source.slice(source.indexOf('const esc='),source.indexOf('const names=')),ctx);
 vm.runInContext(source.slice(source.indexOf('function input('),source.indexOf('function bindWarmStart(')),ctx);
 return ctx;
}

test('all catalog models appear and incompatible choices are labeled and disabled',()=>{
 const ctx=context();
 const html=ctx.modelSelect('model-worker','Worker','DeepSeek');
 assert.match(html,/<select/);
 assert.match(html,/value="DeepSeek" selected/);
 assert.match(html,/value="Decision" disabled/);
 assert.match(html,/value="Audio &lt;unsafe&gt;" disabled/);
 assert.ok(!html.includes('Audio <unsafe>'));
 const router=ctx.modelSelect('router','Router','Decision','decision');
 assert.match(router,/value="Decision" selected/);
 assert.match(router,/value="DeepSeek" disabled/);
 ctx.gatewayCatalog.models.push({id:'Codex',mode:'chat',capabilities:{chat:false,responses:true}});
 assert.match(ctx.modelOptions('Codex','chat'),/value="Codex" selected >Codex/);
});

test('saved assignments remain selectable when missing or catalog is unavailable',()=>{
 const ctx=context();
 ctx.gatewayCatalog={models:[],error:'Offline',stale:false};
 const html=ctx.modelOptions('saved-model','chat');
 assert.match(html,/selected value="saved-model"/);
 assert.match(html,/saved; not in current catalog/);
 assert.match(ctx.modelRoutingView(),/id="routing-enabled" > Select/);
 assert.match(ctx.modelOptions('','chat','Use worker assignment'),/value="" selected/);
});

test('refresh updates options without losing unsaved selections or rebuilding settings',async()=>{
 const ctx=context();
 const select={value:'unsaved-model',dataset:{capability:'chat',placeholder:''},innerHTML:''};
 ctx.document.querySelectorAll=()=>[select];
 ctx.nodes['#model-catalog-status']={textContent:''};
 ctx.api=async path=>{assert.equal(path,'gateway/models?refresh=true');return ctx.gatewayCatalog};
 await ctx.loadModelCatalog(true);
 assert.match(select.innerHTML,/selected value="unsaved-model"/);
 assert.match(ctx.nodes['#model-catalog-status'].textContent,/3 gateway models/);
});

test('routing form persists selected roles, decision model and task overrides',()=>{
 const ctx=context();
 for(const [id,value] of Object.entries({'routing-router':'Decision','routing-confidence':'.85',
  'routing-fast':'DeepSeek','routing-coding':''}))ctx.nodes['#'+id]={value};
 ctx.nodes['#routing-enabled']={checked:true};
 ctx.document.querySelectorAll=selector=>selector==='.routing-role:checked'?[{value:'worker'}]:[];
 const result=JSON.parse(JSON.stringify(ctx.modelRoutingForm()));
 assert.deepEqual(result,{enabled:true,roles:['worker'],router_model:'Decision',min_confidence:.85,profiles:{fast:'DeepSeek'}});
});
