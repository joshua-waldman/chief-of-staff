const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('web/app.js','utf8');
const ctx={esc:x=>String(x??'').replace(/</g,'&lt;'),badge:x=>x,date:x=>x};
vm.createContext(ctx);
vm.runInContext(source.slice(source.indexOf('const triageLabels='),source.indexOf('function monitoringAlert(')),ctx);
test('queue decisions and summaries are escaped and searchable with independent filters',()=>{
 const job={id:'job',title:'Update',org:'Communications',project:'',status:'queued',triage_category:'action',triage_summary:'Investigate <release>',updated:'today'};
 const html=ctx.table([job]);
 assert.match(html,/Action Needed/);
 assert.match(html,/Investigate &lt;release>/);
 assert.ok(!html.includes('<release>'));
 assert.equal(ctx.queueMatches(job,'release','queued','action'),true);
 assert.equal(ctx.queueMatches(job,'release','completed','action'),false);
 assert.equal(ctx.queueMatches(job,'release','queued','reading'),false);
});

test('queue uses a short name, keeps original text searchable and collapses summaries',()=>{
 const job={id:'job',title:'Original detailed request',display_name:'Launch readiness',org:'Technology',project:'',status:'queued',triage_summary:'Full summary',updated:'today'};
 const html=ctx.table([job]);
 assert.match(html,/class="job-title">Launch readiness/);
 assert.ok(!html.includes('Original detailed request'));
 assert.match(html,/<details class="small"><summary>View summary<\/summary>Full summary<\/details>/);
 assert.equal(ctx.queueMatches(job,'launch','',''),true);
 assert.equal(ctx.queueMatches(job,'detailed','',''),true);
});
