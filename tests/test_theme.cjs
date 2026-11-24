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

test('theme palettes keep body, secondary, button and status text readable',()=>{
 function luminance(hex){
  let value=hex.slice(1);if(value.length===3)value=[...value].map(c=>c+c).join('');
  const channels=[0,2,4].map(i=>parseInt(value.slice(i,i+2),16)/255)
   .map(c=>c<=0.04045?c/12.92:((c+0.055)/1.055)**2.4);
  return channels.reduce((sum,c,i)=>sum+c*[0.2126,0.7152,0.0722][i],0);
 }
 const css=fs.readFileSync('web/style.css','utf8');
 const palettes=[...css.matchAll(/:root(?:\[data-theme="dark"\])?\{([^}]+)\}/g)];
 assert.equal(palettes.length,2);
 const pairs=[['ink','bg'],['muted','surface'],['muted','surface-soft'],
  ['green','success-bg'],['secondary-ink','surface-soft'],
  ['on-accent','primary'],['on-accent','primary-hover'],
  ...['danger','info','purple','warning'].map(status=>[status+'-ink',status+'-bg'])];
 for(const [index,palette] of palettes.entries()){
  const colors=Object.fromEntries([...palette[1].matchAll(/--([\w-]+):([^;]+);/g)]
   .map(m=>[m[1],m[2]]));
  for(const [fg,bg] of pairs){
   const [low,high]=[luminance(colors[fg]),luminance(colors[bg])].sort((a,b)=>a-b);
   assert.ok((high+0.05)/(low+0.05)>=4.5,`palette ${index}: ${fg} on ${bg}`);
  }
 }
});
