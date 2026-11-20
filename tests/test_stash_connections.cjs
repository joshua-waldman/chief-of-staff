const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('web/app.js','utf8');

function context(){
 const nodes={};
 const ctx={nodes,$:selector=>nodes[selector]||null,document:{querySelectorAll:()=>[]},
  bindWarmStart:()=>{},bindSchedules:()=>{},bindCommitments:()=>{},bindKnowledge:()=>{},bindRows:()=>{},
  handle:fn=>fn,toast:()=>{},calls:[],con:null,refresh:async()=>{}};
 ctx.api=async(path,body)=>{ctx.calls.push({path,body})};
 vm.createContext(ctx);
 vm.runInContext(source.slice(source.indexOf('function bind(){'),source.indexOf('async function submit(')),ctx);
 return ctx;
}

test('token form sends the selected route separately and clears a successfully saved secret',async()=>{
 const ctx=context();
 for(const [id,value] of Object.entries({'stash-token-form':'','stash-server':' amd ',
  'stash-project':' ATE ','stash-token':'private-test-token'}))ctx.nodes['#'+id]={value};
 ctx.bind();
 await ctx.nodes['#stash-token-form'].onsubmit({preventDefault(){}});
 assert.equal(ctx.calls[0].path,'stash/token');
 assert.deepEqual(JSON.parse(JSON.stringify(ctx.calls[0].body)),{
  server:'amd',project:'ATE',token:'private-test-token'});
 assert.equal(ctx.nodes['#stash-token'].value,'');
});

test('settings preserve both repository providers and never include the token',async()=>{
 const ctx=context();
 ctx.state={settings:{models:{},projects:{}}};
 const values={'settings-form':'','approval-mode':'manual','engineering-workflows':'{}',
  'github-accounts':'{"org":"work"}','stash-servers':'{"amd":{"url":"https://stash.amd.com","projects":{"ATE":"work"}}}',
  parallel:'4','model-parallel':'1',steps:'30',interval:'60',prefix:'!cos','research-access':'public',hosts:'amd.com',
  monitor:'','timing-assessment':''};
 for(const [id,value] of Object.entries(values))ctx.nodes['#'+id]={value,checked:false};
 ctx.bind();
 await ctx.nodes['#settings-form'].onsubmit({preventDefault(){}});
 const saved=ctx.calls.find(c=>c.path==='settings').body;
 assert.equal(saved.github_accounts.org,'work');
 assert.equal(saved.stash_servers.amd.projects.ATE,'work');
 assert.ok(!('token' in saved));
});
