const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('web/app.js','utf8');
function setup(){
 const messages=[];
 const context={returnedResults:[],knownResults:null,toast:s=>messages.push(s),messages,
  esc:x=>String(x??'').replace(/</g,'&lt;'),date:x=>x,
  btn:(text,attrs)=>'<button '+attrs+'>'+text+'</button>',intro:(a,b)=>a+b,
  jobFilesView:files=>files.map(f=>f.name).join(',')};
 vm.createContext(context);
 vm.runInContext(source.slice(source.indexOf('function resultsNotice('),source.indexOf('function render(){')),context);
 return context;
}
test('review queue preserves escaped summaries and provides a direct result action',()=>{
 const ctx=setup();
 ctx.returnedResults=[{id:'job',org:'Technology',title:'Draft <brief>',summary:'Result <text>',completed:'today'}];
 const html=ctx.resultsView();
 assert.match(html,/Draft &lt;brief>/);
 assert.match(html,/Result &lt;text>/);
 assert.match(html,/data-job="job"/);
 assert.match(ctx.resultsNotice(),/1 completed result is ready/);
 ctx.returnedResults=[];
 assert.match(ctx.resultsView(),/All caught up/);
});
test('result review places declared deliverables and acknowledgement with the full response',()=>{
 const ctx=setup();
 const job={files:[{id:'a',name:'brief.docx',purpose:'deliverable'},{id:'b',name:'source.txt'},
  {id:'c',name:'history.txt',purpose:'recovery'}],result_review:{summary:'Full <answer>',file_ids:['a','c'],completed:'today'}};
 let html=ctx.returnedResultView(job);
 assert.match(html,/Full &lt;answer>/);
 assert.match(html,/brief.docx/);
 assert.ok(!html.includes('source.txt'));
 assert.ok(!html.includes('history.txt'));
 assert.match(html,/id="mark-result-reviewed"/);
 job.result_review.file_ids=[];
 job.result_review.summary+=' /api/files/a';
 assert.match(ctx.returnedResultView(job),/brief.docx/);
 job.result_review.reviewed='later';
 html=ctx.returnedResultView(job);
 assert.match(html,/Reviewed result/);
 assert.ok(!html.includes('id="mark-result-reviewed"'));
});
test('completion notice announces each new revision once and does not repeat after a temporary hide',()=>{
 const ctx=setup(),first={revision:'a',title:'First response'};
 ctx.notifyResults([first]);
 assert.equal(ctx.messages.length,0);
 ctx.notifyResults([first,{revision:'b',title:'New draft'}]);
 assert.equal(ctx.messages.length,1);
 assert.match(ctx.messages[0],/New draft/);
 ctx.notifyResults([]);
 ctx.notifyResults([first]);
 assert.equal(ctx.messages.length,1);
 ctx.notifyResults([{revision:'c',title:'Revised response'}]);
 assert.equal(ctx.messages.length,2);
});

test('review summaries turn bare and Markdown file references into safe named links',()=>{
 const ctx=setup(),files=[{id:'abc-123',name:'Roadmap <draft>.docx'}];
 const html=ctx.resultSummary('Review /api/files/abc-123. Also [Task list](/api/files/child). <script>bad</script>',files);
 assert.match(html,/href="\/api\/files\/abc-123"[^>]*>Roadmap &lt;draft>.docx<\/a>\./);
 assert.match(html,/href="\/api\/files\/child"[^>]*>Task list<\/a>/);
 assert.match(html,/target="_blank" rel="noopener"/);
 assert.ok(!html.includes('<script>'));
 assert.match(ctx.resultSummary('/api/files/unknown'),/>Open review file<\/a>/);
 assert.equal(ctx.resultSummary('[Bad](javascript:alert(1))'),'[Bad](javascript:alert(1))');
});

test('parent reviews use referenced child file names and queue cards offer file links',()=>{
 const ctx=setup();
 const review={summary:'Read /api/files/child.',file_ids:[],completed:'today',files:[{id:'child',name:'Task list.docx'}]};
 const html=ctx.returnedResultView({files:[],result_review:review});
 assert.match(html,/>Task list.docx<\/a>/);
 ctx.returnedResults=[{...review,id:'parent',title:'Roadmap',org:'Strategy'}];
 assert.match(ctx.resultsView(),/href="\/api\/files\/child"/);
});
