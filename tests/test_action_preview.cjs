const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('web/app.js','utf8');
const ctx={esc:value=>String(value??'').replace(/</g,'&lt;'),readable:(label,value)=>label+': '+String(value??'').replace(/</g,'&lt;')};
vm.createContext(ctx);
vm.runInContext(source.slice(source.indexOf('function actionPreview('),source.indexOf('function orgCards(')),ctx);

test('calendar preview exposes response and organizer notification, escaping source text',()=>{
 const preview=ctx.actionPreview('calendar_rsvp',{id:'event',event_subject:'<script>event',response:'decline',send_response:false,event_type:'seriesMaster'});
 assert.match(preview,/Response: decline/);
 assert.match(preview,/Notify organizer: No/);
 assert.match(preview,/seriesMaster/);
 assert.ok(!preview.includes('<script>'));
});

test('cleanup and branch previews identify exact targets',()=>{
 assert.match(ctx.actionPreview('delete_email',{id:'message-123',subject:'An update',from:'sender@example.com'}),/Message ID: message-123/);
 assert.match(ctx.actionPreview('delete_email',{id:'message-123'}),/Deleted Items/);
 assert.match(ctx.actionPreview('mark_teams_read',{chat_id:'chat-123'}),/entire chat/);
 assert.match(ctx.actionPreview('create_feature_branch',{project:'Synthetic',branch:'feature/TEST-1-filter'}),/New branch: feature\/TEST-1-filter/);
});
