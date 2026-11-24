// Run before the stylesheet so a saved appearance applies on the first paint.
(() => {
 'use strict';
 const key='cos-appearance',valid=value=>['system','light','dark'].includes(value);
 const system=window.matchMedia('(prefers-color-scheme: dark)');
 let preference='system',select;
 try{const saved=localStorage.getItem(key);if(valid(saved))preference=saved}catch{}
 function apply(){
  document.documentElement.dataset.theme=preference==='system'?(system.matches?'dark':'light'):preference;
  if(select)select.value=preference;
 }
 apply();
 system.addEventListener('change',apply);
 window.addEventListener('storage',event=>{
  if(event.key===key||event.key===null){preference=valid(event.newValue)?event.newValue:'system';apply()}
 });
 document.addEventListener('DOMContentLoaded',()=>{
  select=document.getElementById('appearance');
  if(!select)return;
  apply();
  select.addEventListener('change',()=>{
   preference=valid(select.value)?select.value:'system';
   try{localStorage.setItem(key,preference)}catch{}
   apply();
  });
 });
})();
