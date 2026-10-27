const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('web/app.js','utf8');
const ctx={esc:x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),date:x=>x,badge:x=>x};
vm.createContext(ctx);
vm.runInContext(source.slice(source.indexOf('const triageLabels='),source.indexOf('function monitoringAlert(')),ctx);
vm.runInContext(fs.readFileSync('web/commitments.js','utf8'),ctx);
const now=new Date('2026-10-27T12:00:00Z');
test('LLM inferred urgency works without any deadline and preserves reasoning safely',()=>{
 const item={status:'waiting_input',time_sensitive:'yes',timing_reason:'Assessment: release work is blocked <until approval>'};
 const timing=ctx.itemTiming(item,now);
 assert.equal(timing.label,'Time sensitive');assert.equal(timing.reason,item.timing_reason);
 const html=ctx.timingBadge(item);
 assert.match(html,/Time sensitive/);assert.match(html,/&lt;until approval&gt;/);
 assert.ok(!html.includes('<until approval>'));
 assert.match(ctx.table([{...item,id:'one',title:'Launch',org:'Technology',updated:'today'}]),/class="timing-soon"/);
});
test('supported deadlines distinguish overdue, near, future, absent and invalid dates',()=>{
 const item={status:'awaiting_approval'};
 for(const [deadline_at,label] of [['2026-10-27T11:59:59Z','Overdue'],['2026-10-29T12:00:00Z','Due soon'],['2026-10-29T12:00:01Z',null],['bad',null],['',null]]){
  assert.equal(ctx.itemTiming({...item,deadline_at},now)?.label||null,label);
 }
 assert.equal(ctx.itemTiming({...item,time_sensitive:'unknown'},now),null);
 assert.equal(ctx.itemTiming({...item,brief:{time_sensitive:'yes',timing_reason:'Assessment: incident response is blocked'}},now).label,'Time sensitive');
});
test('terminal requests and commitments do not retain urgency',()=>{
 for(const status of ['completed','cancelled','dismissed','merged'])assert.equal(ctx.itemTiming({status,time_sensitive:'yes',deadline_at:'2026-01-01T00:00:00Z'},now),null);
 for(const status of ['completed','dismissed','merged'])assert.equal(ctx.commitmentTiming({status,deadline_certainty:'explicit',deadline_date:'2026-01-01'},now),null);
});
test('commitment calendar dates use local days and preserve inferred uncertainty',()=>{
 const today=new Date(2026,9,27,23,59);
 const item={status:'open',deadline_certainty:'explicit'};
 assert.equal(ctx.commitmentTiming({...item,deadline_date:'2026-10-27'},today).label,'Due today');
 assert.equal(ctx.commitmentTiming({...item,deadline_date:'2026-10-29'},today).label,'Due soon');
 assert.equal(ctx.commitmentTiming({...item,deadline_date:'2026-10-30'},today),null);
 assert.equal(ctx.commitmentTiming({...item,deadline_date:'2026-02-30'},today),null);
 const inferred=ctx.commitmentTiming({...item,deadline_certainty:'inferred',deadline_date:'2026-10-26'},today);
 assert.match(inferred.label,/Possible timing — confirm/);assert.equal(inferred.level,'soon');
});

test('timing column makes routine, unknown and unassessed items visibly distinct',()=>{
 for(const [time_sensitive,label] of [['no','Routine'],['unknown','Timing unclear'],['','Not yet assessed']]){
  const html=ctx.table([{id:'job',title:'Request',org:'Technology',status:'waiting_input',time_sensitive,timing_reason:'Context <evidence>',updated:'now'}]);
  assert.match(html,/<th>TIMING<\/th>/);
  assert.ok(html.includes(label));
  assert.ok(html.includes('Context &lt;evidence&gt;'));
  assert.ok(!html.includes('<evidence>'));
 }
});
