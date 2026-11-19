/* Stable release notices live outside the main view so edits do not dismiss them. */
(function () {
 'use strict';
 const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const working = state => ['checking','preparing','waiting','installing'].includes(state);
 function content(status, expanded) {
  const release = status.release || {}, state = status.state;
  if (!expanded && !status.attention && (status.reminded || !['available','preparing','waiting','installing'].includes(state))) return '';
  const messages = {idle:'Check for a new COS release.',checking:'Checking for a stable release…',current:'COS is up to date.',available:'COS '+release.tag+' is available.',preparing:'Downloading the release…',waiting:'Waiting for active work to finish. New work is paused.',installing:'Installing the update. COS will restart; this page will reconnect.',error:status.error};
  return '<p role="status">'+escape(messages[state] || '')+'</p>'+
   (status.current?'<p class="small">Installed: '+escape(status.current.tag || 'Unversioned installation')+'</p>':'')+
   (release.tag?'<details><summary>What’s new in '+escape(release.tag)+'</summary><pre class="update-notes">'+escape(release.notes)+'</pre></details>':'')+
   (state==='available'?'<p>Installation waits for active work, backs up the database, and restarts COS.</p><button type="button" class="primary" data-update="install">Update and restart</button> <button type="button" class="secondary" data-update="remind">Remind me tomorrow</button> ':'')+
   (!working(state)?'<button type="button" class="secondary" data-update="check">Check for updates</button>':'');
 }
 if (typeof module !== 'undefined' && module.exports) { module.exports = {content, working}; return; }
 const panel = document.querySelector('#release-notice');
 const check = document.querySelector('#check-updates');
 const token = document.querySelector('meta[name=cos-token]').content;
 let status = {state:'idle'}, expanded = false, polling = false, reconnecting = false;
 function render() {
  panel.innerHTML = content(status, expanded);
  panel.hidden = !panel.innerHTML;
  check.disabled = working(status.state);
 }
 async function request(action, body) {
  const response = await fetch('/api/updates'+(action?'/'+action:''), {method:action?'POST':'GET',headers:{'Content-Type':'application/json','X-COS-Token':token},body:action?JSON.stringify(body || {}):undefined});
  if (response.status===401 && reconnecting) {
   const health = await fetch('/healthz').then(r=>r.json());
   if (!health.trial) location.reload();
   return;
  }
  const next = await response.json();
  if (!response.ok) throw Error(next.error || next.detail || 'Update request failed.');
  status = next;
  if (['preparing','waiting','installing'].includes(status.state)) reconnecting = true;
  render();
 }
 async function perform(action) {
  if (working(status.state)) return;
  expanded = true;
  const previous = status;
  status = {...status,state:action==='check'?'checking':action==='install'?'preparing':status.state}; render();
  try {
   await request(action,action==='install'?{acknowledgment:'update_and_restart',tag:previous.release.tag,sha:previous.release.sha}:{});
   if(action==='remind'){expanded=false;render();}
  } catch(error) { status={...previous,state:'error',error:error.message};render(); }
 }
 check.addEventListener('click',()=>perform('check'));
 panel.addEventListener('click',event=>{const button=event.target.closest('[data-update]');if(button)perform(button.dataset.update);});
 async function poll() {
  if(polling || document.hidden || panel.querySelector('details[open]'))return;
  polling=true;
  try { await request(); } catch(error) { /* Keep the last notice while offline or restarting. */ }
  finally { polling=false; }
 }
 poll(); setInterval(poll,5000);
}());
