const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('web/theme.js','utf8');

function browser({saved=null,dark=false,blocked=false}={}){
 const events={},systemEvents={},controlEvents={},documentEvents={};
 const select={value:'',addEventListener:(name,fn)=>controlEvents[name]=fn};
 const system={matches:dark,addEventListener:(name,fn)=>systemEvents[name]=fn};
 const root={dataset:{}};
 const storage={value:saved,getItem(){if(blocked)throw Error('blocked');return this.value},
  setItem(key,value){if(blocked)throw Error('blocked');this.value=value}};
 vm.runInNewContext(source,{
  window:{matchMedia:()=>system,addEventListener:(name,fn)=>events[name]=fn},
  document:{documentElement:root,getElementById:()=>select,
   addEventListener:(name,fn)=>documentEvents[name]=fn},localStorage:storage});
 return {root,system,select,storage,events,systemEvents,
  ready:()=>documentEvents.DOMContentLoaded(),
  choose:value=>{select.value=value;controlEvents.change()}};
}

test('applies saved dark appearance before DOM ready and restores the selector',()=>{
 const b=browser({saved:'dark'});
 assert.equal(b.root.dataset.theme,'dark');
 b.ready();assert.equal(b.select.value,'dark');
});
test('system default and invalid stored values track OS changes',()=>{
 for(const saved of [null,'invalid','system']){
  const b=browser({saved,dark:true});b.ready();
  assert.equal(b.select.value,'system');assert.equal(b.root.dataset.theme,'dark');
  b.system.matches=false;b.systemEvents.change();
  assert.equal(b.root.dataset.theme,'light');
 }
});
test('explicit selection persists, overrides OS and can return to system',()=>{
 const b=browser({dark:true});b.ready();b.choose('light');
 assert.equal(b.storage.value,'light');b.systemEvents.change();
 assert.equal(b.root.dataset.theme,'light');
 b.choose('dark');assert.equal(b.root.dataset.theme,'dark');
 assert.equal(browser({saved:b.storage.value}).root.dataset.theme,'dark');
 b.system.matches=false;b.choose('system');
 assert.equal(b.storage.value,'system');assert.equal(b.root.dataset.theme,'light');
});
test('storage changes and clearing preferences synchronize tabs',()=>{
 const b=browser();b.ready();
 b.events.storage({key:'cos-appearance',newValue:'dark'});
 assert.equal(b.root.dataset.theme,'dark');assert.equal(b.select.value,'dark');
 b.events.storage({key:'unrelated',newValue:'light'});
 assert.equal(b.root.dataset.theme,'dark');
 b.events.storage({key:null,newValue:null});
 assert.equal(b.root.dataset.theme,'light');assert.equal(b.select.value,'system');
});
test('appearance remains usable when browser storage is unavailable',()=>{
 const b=browser({blocked:true,dark:true});b.ready();
 assert.equal(b.root.dataset.theme,'dark');b.choose('light');
 assert.equal(b.root.dataset.theme,'light');
});
