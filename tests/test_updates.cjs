const {test}=require('node:test');
const assert=require('node:assert/strict');
const {content,working}=require('../web/updates.js');

test('release notice escapes notes and version and offers explicit installation',()=>{
 const html=content({state:'available',release:{tag:'v1.2.0',notes:'<script>unsafe()</script>'}},false);
 assert.match(html,/Update and restart/);
 assert.match(html,/Remind me tomorrow/);
 assert.ok(!html.includes('<script>'));
 assert.match(html,/&lt;script&gt;/);
});
test('reminders hide automatic notices while manual checks show status',()=>{
 assert.equal(content({state:'available',reminded:true},false),'');
 assert.match(content({state:'current'},true),/up to date/);
 assert.equal(content({state:'error',error:'Offline'},false),'');
 assert.match(content({state:'error',error:'Offline'},true),/Offline/);
});
test('installation state prevents duplicate installation and explains draining',()=>{
 const html=content({state:'waiting'},false);
 assert.match(html,/active work/);
 assert.ok(!html.includes('data-update="install"'));
 assert.ok(working('preparing') && working('waiting') && working('installing'));
 assert.ok(!working('available'));
});
