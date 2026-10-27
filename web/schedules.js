let scheduledTasks=[],editingSchedule=null,scheduleChanging=false;
async function loadSchedules(){scheduledTasks=await api('schedules')}
function schedulesView(){
 const s=editingSchedule||{},zone=s.timezone||Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC';
 return intro('Your work, on a schedule.','Create requests that COS runs once, daily, or weekly.')+
 '<p class="small">COS must be running and connected. Missed occurrences become one catch-up run. Recurring tasks wait until their previous run finishes. Each run uses the usual action approvals.</p>'+
 '<section class="panel"><h2>'+(s.id?'Edit scheduled task':'New scheduled task')+'</h2><form id="schedule-form">'+
 input('schedule-name','Task name',s.name||'')+'<div class="field"><label for="schedule-request">Request for COS</label><textarea id="schedule-request" required>'+esc(s.request||'')+'</textarea></div>'+
 '<div class="form-grid"><div class="field"><label for="schedule-project">Project</label><select id="schedule-project"><option value="">Let COS route</option>'+Object.keys(state.settings.projects).map(p=>'<option '+(p===s.project?'selected ':'')+'value="'+esc(p)+'">'+esc(p)+'</option>').join('')+'</select></div>'+
 '<div class="field"><label for="schedule-cadence">Repeat</label><select id="schedule-cadence">'+[['once','Once'],['daily','Daily'],['weekly','Weekly on the start day']].map(([v,l])=>'<option value="'+v+'" '+(v===(s.cadence||'once')?'selected':'')+'>'+l+'</option>').join('')+'</select></div>'+
 input('schedule-start','Start date and time',s.start_local||'','datetime-local')+input('schedule-zone','Timezone',zone,'text','IANA name, for example America/Chicago. Daily and weekly runs keep this local time.')+'</div>'+btn(s.id?'Save changes':'Create schedule','type="submit"')+(s.id?' '+btn('Cancel edit','id="cancel-schedule-edit" type="button"','secondary'):'')+'</form></section>'+
 (scheduledTasks.length?scheduledTasks.map(r=>'<article class="panel"><div class="eyebrow">'+esc(r.status)+' · '+esc(r.cadence)+'</div><h2>'+esc(r.name)+'</h2><p class="readable">'+esc(r.request)+'</p><p class="small">'+esc(r.project||'COS routing')+' · '+esc(r.timezone)+' · Next: '+(r.next_run?esc(new Date(r.next_run).toLocaleString(undefined,{timeZone:r.timezone,timeZoneName:'short'})):'Finished')+'</p>'+((r.project&&!Object.hasOwn(state.settings.projects,r.project))?'<p class="warning">Project is no longer registered. Edit this schedule to select an available project.</p>':'')+
 '<div class="row">'+btn('Edit','data-schedule-edit="'+esc(r.id)+'"','secondary')+(r.status==='active'?btn('Pause','data-schedule-action="pause" data-schedule="'+esc(r.id)+'"','secondary'):r.status==='paused'?btn('Resume','data-schedule-action="resume" data-schedule="'+esc(r.id)+'"','secondary'):'')+btn('Delete schedule','data-schedule-action="delete" data-schedule="'+esc(r.id)+'"','secondary')+'</div><p class="small">Pausing or deleting a schedule leaves existing jobs in the Job tracker.</p><details id="schedule-history-'+esc(r.id)+'"><summary>Recent runs ('+r.runs.length+')</summary>'+r.runs.map(run=>'<p>'+btn(esc(date(run.created))+' · '+esc(labels[run.status]||run.status),'data-schedule-job="'+esc(run.job_id)+'"','link-btn')+'<small> · Due '+esc(date(run.due))+'</small></p>').join('')+'</details></article>').join(''):'<section class="panel empty"><p>No scheduled tasks yet.</p></section>');
}
function resetScheduleEditor(){editingSchedule=null;$('#main').dataset.view='';render()}
function bindSchedules(){
 document.querySelectorAll('[data-schedule-edit],[data-schedule-action]').forEach(b=>b.disabled=scheduleChanging);
 document.querySelectorAll('[data-schedule-job]').forEach(b=>b.onclick=handle(()=>openJob(b.dataset.scheduleJob)));
 if($('#cancel-schedule-edit'))$('#cancel-schedule-edit').onclick=resetScheduleEditor;
 document.querySelectorAll('[data-schedule-edit]').forEach(b=>b.onclick=()=>{editingSchedule=scheduledTasks.find(s=>s.id===b.dataset.scheduleEdit);$('#main').dataset.view='';render();$('#schedule-name').focus()});
 document.querySelectorAll('[data-schedule-action]').forEach(b=>b.onclick=handle(async()=>{
  if(scheduleChanging)return;
  scheduleChanging=true;document.querySelectorAll('[data-schedule-edit],[data-schedule-action]').forEach(control=>control.disabled=true);
  try{
  const s=scheduledTasks.find(r=>r.id===b.dataset.schedule);
  await api('schedules/'+s.id,{action:b.dataset.scheduleAction,version:s.version});
  await loadSchedules();if(editingSchedule?.id===s.id)resetScheduleEditor();else render();toast('Schedule updated');
  }finally{scheduleChanging=false;document.querySelectorAll('[data-schedule-edit],[data-schedule-action]').forEach(control=>control.disabled=false)}
 }));
 if($('#schedule-form'))$('#schedule-form').onsubmit=handle(async e=>{
  e.preventDefault();if(scheduleChanging)return;
  scheduleChanging=true;document.querySelectorAll('[data-schedule-edit],[data-schedule-action]').forEach(control=>control.disabled=true);
  const s=editingSchedule,button=e.submitter;if(button)button.disabled=true;
  try{
  const body={name:$('#schedule-name').value,request:$('#schedule-request').value,project:$('#schedule-project').value,cadence:$('#schedule-cadence').value,start_local:$('#schedule-start').value,timezone:$('#schedule-zone').value,...(s?{version:s.version}:{})};
  await api('schedules'+(s?'/'+s.id:''),body);await loadSchedules();resetScheduleEditor();toast('Schedule saved');
  }finally{scheduleChanging=false;if(button)button.disabled=false;document.querySelectorAll('[data-schedule-edit],[data-schedule-action]').forEach(control=>control.disabled=false)}
 });
}
