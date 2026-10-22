const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('web/app.js','utf8');

test('org chart places agents in their execution org, links parents and separates history',()=>{
 const context={organizationAgents:[
  {id:'parent',title:'Handle request',org:'Communications',status:'waiting_children'},
  {id:'child',parent_id:'parent',parent_title:'Handle <request>',parent_org:'Communications',title:'Research <issue>',org:'Technology',status:'running',status_summary:'Comparing <sources>',next_step:'Write findings'},
  {id:'queued',title:'Queued work',org:'Technology',status:'queued'},
  {id:'waiting',title:'Needs approval',org:'Technology',status:'awaiting_approval'},
  {id:'finished',title:'Old work',org:'Technology',status:'completed'},
  {id:'cancelled',title:'Cancelled work',org:'Technology',status:'cancelled'},
 ],state:{organizations:[{name:'Communications',leader:'CCO'},{name:'Technology',leader:'CTO'},{name:'Engineering',leader:'Engineering director'}]},labels:{},date:()=>'',esc:x=>String(x??'').replace(/</g,'&lt;'),badge:s=>'<span>'+s+'</span>',btn:(label,attrs)=>'<button '+attrs+'>'+label+'</button>'};
 vm.createContext(context);
 vm.runInContext(source.slice(source.indexOf('function agentNode('),source.indexOf('function table(')),context);
 const html=context.organizationChart();
 const technology=html.slice(html.indexOf('aria-label="Technology agents"'),html.indexOf('aria-label="Engineering agents"'));
 assert.match(technology,/3 deployed · 1 running · 1 sub-agents/);
 assert.match(technology,/Research &lt;issue>/);
 assert.match(technology,/Comparing &lt;sources>/);
 assert.match(technology,/Write findings/);
 assert.match(technology,/data-job="parent"/);
 assert.match(technology,/Communications · Handle &lt;request>/);
 assert.match(technology,/Finished agents \(2\)/);
 assert.ok(technology.indexOf('Old work')>technology.indexOf('<details'));
 assert.ok(technology.indexOf('Needs approval')<technology.indexOf('<details'));
 assert.match(html,/No agents currently deployed/);
 assert.ok(!html.includes('<issue>'));
});
