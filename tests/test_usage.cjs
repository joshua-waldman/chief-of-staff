const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('web/app.js','utf8');
function context(){
 const ctx={Number,Date,usageState:null,gatewayUsage:null,personalBalance:null,usageError:'',usageDays:'1',view:'overview',
  btn:(text,attrs)=>'<button '+attrs+'>'+text+'</button>',date:s=>'date '+s};
 vm.createContext(ctx);
 vm.runInContext(source.slice(source.indexOf('const esc='),source.indexOf('const names=')),ctx);
 vm.runInContext(source.slice(source.indexOf('function usageNumber(')),ctx);
 return ctx;
}
test('usage distinguishes measured consumption from unknown gateway balance',()=>{
 const ctx=context();
 ctx.usageState={days:'1',period:{total_tokens:30,requests:2,incomplete_requests:1},models:[],purposes:[]};
 ctx.gatewayUsage={fetched_at:'2026-11-25',total_tokens:500,requests:10,approx_charge_usd:0.2,period_end:'2026-11-25',stale:true};
 const html=ctx.usageDetails();
 assert.match(html,/Connect TokenOps/);
 assert.match(html,/approximately \$0.20 USD/);
 assert.match(html,/1 incomplete or pending/);
 assert.match(html,/Last successful report/);
 assert.match(ctx.usageOverview(),/30/);
 assert.match(ctx.jobUsageView(ctx.usageState.period),/Includes delegated work/);
});
test('usage escapes remote and model values',()=>{
 const ctx=context();
 ctx.gatewayUsage={error:'<img src=x onerror=bad>'};
 assert.doesNotMatch(ctx.usageDetails(),/<img/);
 assert.match(ctx.usageRows([{model:'<script>bad</script>'}],'model'),/&lt;script&gt;/);
});
test('COS costs distinguish small estimates, partial coverage and unavailable pricing',()=>{
 const ctx=context();
 ctx.usageState={period:{requests:3,total_tokens:100,estimated_cost_usd:0.000125,unpriced_requests:1},
  models:[{model:'example',estimated_cost_usd:0.000125,unpriced_requests:1}],cost_pricing_stale:true};
 const html=ctx.usageDetails();
 assert.match(html,/Estimated COS cost: <b>\$0.000125/);
 assert.match(html,/1 request excluded/);
 assert.match(html,/EST. COST \(USD\)/);
 assert.match(html,/1 unpriced/);
 assert.match(html,/last saved gateway rates/);
 assert.match(html,/Estimates for earlier requests change when rates change/);
 assert.match(ctx.usageOverview(),/Estimated COS cost/);
 assert.match(ctx.jobUsageView(ctx.usageState.period),/\$0.000125/);
 assert.match(ctx.usageRows([{model:'unknown',estimated_cost_usd:null}],'model'),/Unavailable/);
 assert.equal(ctx.usageCostMoney(0),'$0.00');
});
test('period begins at local midnight and includes the requested number of days',()=>{
 const ctx=context(),start=new Date(ctx.usageStart('7')),now=new Date();
 assert.equal(start.getHours(),0);
 assert.equal(start.getMinutes(),0);
 const expected=new Date(now);expected.setHours(0,0,0,0);expected.setDate(expected.getDate()-6);
 assert.equal(start.getTime(),expected.getTime());
});

test('an older period request cannot overwrite a newer selection',async()=>{
 const ctx=context(),requests=[];
 ctx.view='connections';
 ctx.api=()=>new Promise(resolve=>requests.push(resolve));
 ctx.usageDays='30';const older=ctx.loadUsage();
 ctx.usageDays='7';const newer=ctx.loadUsage();
 requests[3]({period:{total_tokens:7}});requests[4]({state:'ready'});requests[5]({state:'not_connected'});await newer;
 assert.equal(ctx.usageState.days,'7');
 requests[0]({period:{total_tokens:30}});requests[1]({state:'unavailable'});requests[2]({state:'not_connected'});await older;
 assert.equal(ctx.usageState.days,'7');
 assert.equal(ctx.usageState.period.total_tokens,7);
 assert.equal(ctx.gatewayUsage.state,'ready');
});

test('personal balance is shown in USD and stale or incomplete source data remains visible',()=>{
 const ctx=context();
 ctx.personalBalance={stale:true,state:'sign_in_required',report:{month:'2026-11',remaining_usd:20,budget_usd:100,spend_usd:80,source_stale:true}};
 const html=ctx.tokenOpsView();
 assert.match(html,/\$20.00 USD remaining/);
 assert.match(html,/last successful balance/);
 assert.match(html,/stale or incomplete source data/);
 assert.match(html,/Sign in again/);
 ctx.personalBalance.report.remaining_usd=-5;
 assert.match(ctx.personalBalanceSummary(),/\$5.00 USD over budget/);
});

test('a stale poll cannot restore a balance after disconnecting TokenOps',async()=>{
 const ctx=context(),requests=[];
 ctx.api=()=>new Promise(resolve=>requests.push(resolve));
 const older=ctx.loadUsage();
 ctx.loadUsage.personalGeneration=2;
 ctx.personalBalance={state:'not_connected',report:null};
 requests[0]({period:{total_tokens:7}});requests[1]({state:'ready'});
 requests[2]({state:'ready',report:{remaining_usd:20}});await older;
 assert.equal(ctx.personalBalance.state,'not_connected');
 assert.equal(ctx.personalBalance.report,null);
 assert.equal(ctx.usageState.period.total_tokens,7);
});

test('job usage explains budget-driven model choices and escapes routing text',()=>{
 const ctx=context();
 const html=ctx.jobUsageView({total_tokens:5,requests:1},{model:'<model>',reason:'<reason>',budget_tier:'critical',estimated_saving_percent:95});
 assert.match(html,/&lt;model&gt;/);
 assert.match(html,/&lt;reason&gt;/);
 assert.match(html,/Estimated input\/output saving: 95%/);
});
