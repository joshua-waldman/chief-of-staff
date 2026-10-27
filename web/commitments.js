// Register controls are local review decisions. They never approve external work.
let commitmentPage={items:[],total:0,offset:0,limit:25},commitmentQuery='',commitmentFilter='active',commitmentDetail=null,commitmentMergeTarget=null;
const commitmentLabels={proposed:'Proposed — review owner and scope',open:'Open',completed:'Completed',dismissed:'Dismissed',merged:'Merged'};

function commitmentTiming(item,now=new Date()){
 if(!['open','proposed'].includes(item.status)||!['explicit','inferred'].includes(item.deadline_certainty)||!/^\d{4}-\d{2}-\d{2}$/.test(item.deadline_date||''))return null;
 const [y,m,d]=item.deadline_date.split('-').map(Number),due=new Date(y,m-1,d);
 if(due.getFullYear()!==y||due.getMonth()!==m-1||due.getDate()!==d)return null;
 const today=Date.UTC(now.getFullYear(),now.getMonth(),now.getDate()),days=(Date.UTC(y,m-1,d)-today)/86400000;
 if(days>2)return null;
 const inferred=item.deadline_certainty==='inferred';
 return {level:inferred?'soon':days<0?'overdue':'soon',label:(inferred?'Possible timing — confirm: ':'')+(days<0?'Overdue':days===0?'Due today':'Due soon')};
}
function commitmentTimingClass(item){const timing=commitmentTiming(item);return timing?' timing-'+timing.level:''}
function commitmentTimingBadge(item){const timing=commitmentTiming(item);return timing?'<span class="timing-badge timing-'+timing.level+'">◷ '+esc(timing.label)+'</span>':''}

function commitmentDeadline(item){
 const certainty={unknown:'Deadline unknown',explicit:'Stated deadline',inferred:'Inferred date — confirm'}[item.deadline_certainty]||'Deadline unknown';
 return certainty+(item.deadline_text?' · '+item.deadline_text:'')+(item.deadline_date?' · '+item.deadline_date:'');
}

function commitmentAttention(){
 const count=state.commitments_attention||0;
 return count?'<section class="panel"><h2>Meeting commitments ('+count+')</h2><p>Proposed assignments and open commitments remain visible independently of their original jobs.</p>'+btn('Review commitments','data-go="commitments"','secondary')+'</section>':'';
}

async function loadCommitments(){
 commitmentPage=await api('commitments?'+new URLSearchParams({q:commitmentQuery,status:commitmentFilter,offset:commitmentPage.offset,limit:commitmentPage.limit}));
 if(commitmentPage.offset&&commitmentPage.offset>=commitmentPage.total){commitmentPage.offset=0;return loadCommitments()}
 // Keep the detail's review version frozen during background refreshes.
}

function renderCommitments(resetFields=false){
 if(resetFields&&$('#main'))$('#main').dataset.view='';
 render();
}

function commitmentSummary(item){
 return '<article class="panel'+commitmentTimingClass(item)+'">'+commitmentTimingBadge(item)+readable('Owner',item.owner)+readable('Deliverable',item.deliverable)+readable('Scope',item.scope)+readable('Status',commitmentLabels[item.status]||item.status)+readable('Timing',commitmentDeadline(item))+(item.deadline_basis?readable('Date interpretation',item.deadline_basis):'')+'</article>';
}

function commitmentSources(item){
 return '<details id="cm-evidence" open><summary>Source evidence ('+item.evidence.length+')</summary>'+item.evidence.map(e=>'<article class="panel"><b>'+esc(e.kind==='completion'?'Possible completion — verify':e.kind==='assignment'?'Assignment evidence':'Corroborating evidence')+'</b><p>'+esc(e.meeting_date||'Meeting date unknown')+' · '+esc(e.source_timestamp)+'</p><blockquote class="readable">'+esc(e.excerpt)+'</blockquote><p><a href="'+esc(e.source_url)+'" target="_blank" rel="noreferrer">Meeting source ↗</a> · <a href="/api/files/'+encodeURIComponent(e.source_file_id)+'">Transcript</a> · '+btn('Source job','data-job="'+esc(e.source_job)+'"','link-btn')+'</p></article>').join('')+'</details>';
}

function commitmentHistory(item){
 return '<details id="cm-history"><summary>Review history ('+item.history.length+')</summary>'+item.history.map(h=>{const details=JSON.parse(h.details),related=details.source_id||details.target_id;return '<p>'+esc(date(h.created)+' · '+h.actor+' · '+h.action)+(h.note?' · '+esc(h.note):'')+'</p><pre class="code">'+esc(JSON.stringify(details,null,2))+'</pre>'+(related?btn('Related commitment','data-cm-open="'+esc(related)+'"','link-btn'):'')}).join('')+'</details>';
}

function commitmentDetailView(item){
 const active=['proposed','open'].includes(item.status);
 let html='<section class="panel" id="cm-detail">'+btn('Close record','id="cm-close"','secondary')+btn('Refresh record','id="cm-reload"','secondary')+'<h2>Commitment details</h2><p class="small">Record '+esc(item.id)+' · Revision '+item.version+'</p>'+commitmentSummary(item);
 if(item.status==='merged')return html+'<p>Evidence is retained in the canonical record.</p>'+btn('Open canonical record','data-cm-open="'+esc(item.merged_into)+'"','secondary')+commitmentSources(item)+commitmentHistory(item)+'</section>';
 if(item.completion_pending)html+='<p class="warning">New completion evidence is available. Verify the actual deliverable before marking this commitment complete.</p>';
 html+=commitmentSources(item);
 if(active)html+='<details id="cm-edit"><summary>Correct owner, deliverable or timing</summary><form id="cm-edit-form"><div class="form-grid">'+input('cm-owner','Owner',item.owner)+input('cm-scope','Scope',item.scope)+'</div><div class="field"><label for="cm-deliverable">Deliverable</label><textarea id="cm-deliverable" maxlength="1000" required>'+esc(item.deliverable)+'</textarea></div><div class="form-grid">'+input('cm-deadline-text','Deadline wording',item.deadline_text)+input('cm-deadline-date','Calendar date',item.deadline_date,'date')+'<div class="field"><label for="cm-certainty">Deadline certainty</label><select id="cm-certainty">'+['unknown','explicit','inferred'].map(value=>'<option value="'+value+'" '+(value===item.deadline_certainty?'selected':'')+'>'+esc(value)+'</option>').join('')+'</select></div></div><div class="field"><label for="cm-basis">Basis for an inferred date</label><textarea id="cm-basis" maxlength="1000">'+esc(item.deadline_basis)+'</textarea></div>'+btn('Save corrections','type="submit"')+'</form></details>';
 html+='<div class="field"><label for="cm-note">Verification note or reason</label><textarea id="cm-note" maxlength="1500" placeholder="Explain completion, dismissal or reopening. A completion note is your confirmation that the work was done."></textarea></div>';
 const completions=item.evidence.filter(e=>e.kind==='completion');
 if(item.status==='open'&&completions.length)html+='<div class="field"><label for="cm-completion-evidence">Evidence you reviewed for completion</label><select id="cm-completion-evidence"><option value="">Use my verification note instead</option>'+completions.map(e=>'<option value="'+esc(e.id)+'">'+esc((e.meeting_date||'Unknown date')+' · '+e.source_timestamp+' · '+e.excerpt.slice(0,120))+'</option>').join('')+'</select></div>';
 html+='<p class="small">These controls update the register only. Sending, scheduling, sharing and delegated work keep their own approval requirements.</p><div class="row">';
 if(item.status==='proposed')html+=btn('Confirm assignment','data-cm-action="accept"');
 if(item.status==='open')html+=btn('Confirm completed','data-cm-action="complete"');
 if(active)html+=btn('Dismiss commitment','data-cm-action="dismiss"','secondary');
 if(['completed','dismissed'].includes(item.status))html+=btn('Reopen commitment','data-cm-action="reopen"','secondary');
 html+='</div>';
 if(active){
  html+='<details id="cm-merge"><summary>Compare or merge a repeated commitment</summary><p>Similar wording is only a suggestion. Compare the owner, deliverable, timing and evidence before merging.</p><label for="cm-merge-id">Canonical record to keep</label><input id="cm-merge-id" list="cm-match-options" value="'+esc(commitmentMergeTarget?.id||'')+'"><datalist id="cm-match-options">'+item.possible_matches.map(m=>'<option value="'+esc(m.id)+'">'+esc(m.deliverable)+'</option>').join('')+'</datalist>'+btn('Compare records','id="cm-compare"','secondary');
  if(commitmentMergeTarget)html+='<h3>Canonical record to keep</h3>'+commitmentSummary(commitmentMergeTarget)+commitmentSources(commitmentMergeTarget).replace('id="cm-evidence"','id="cm-target-evidence"')+'<p>The canonical record keeps its fields and status. Source evidence is combined; the original record remains in history. Record the reason above.</p>'+btn('Merge into this record','data-cm-action="merge"','secondary');
  html+='</details>';
 }
 html+=commitmentHistory(item)+'</section>';
 return html;
}

function commitmentsView(){
 return intro('Commitments that stay connected.','Review meeting assignments, preserve uncertain dates, and reconcile evidence across meetings.')+'<p>New assignments are proposals until you confirm them. Source evidence never closes work automatically.</p><div class="toolbar"><form id="cm-search-form"><label for="cm-query">Search commitments</label><input id="cm-query" value="'+esc(commitmentQuery)+'"><label for="cm-filter">Status</label><select id="cm-filter">'+Object.entries({active:'Proposed and open',all:'All records',...commitmentLabels}).map(([value,label])=>'<option value="'+value+'" '+(value===commitmentFilter?'selected':'')+'>'+esc(label)+'</option>').join('')+'</select>'+btn('Search','type="submit"')+'</form></div><div id="cm-list">'+(commitmentPage.items.length?commitmentPage.items.map(item=>'<article class="panel'+commitmentTimingClass(item)+'"><div class="section-title"><h2>'+esc(item.deliverable)+'</h2><span class="tag">'+esc(commitmentLabels[item.status])+'</span></div>'+commitmentTimingBadge(item)+'<p>'+esc(item.owner)+' · '+esc(item.scope)+'</p><p>'+esc(commitmentDeadline(item))+'</p>'+(item.completion_pending?'<p class="warning">Completion evidence needs review.</p>':'')+btn('Review record','data-cm-open="'+esc(item.id)+'"','secondary')+'</article>').join(''):'<div class="panel empty">No commitments match this view. Ask COS to review a meeting transcript and propose explicit owner commitments.</div>')+'</div><div class="row"><p>'+commitmentPage.total+' matching records</p>'+(commitmentPage.offset?btn('Previous','data-cm-page="'+Math.max(0,commitmentPage.offset-commitmentPage.limit)+'"','secondary'):'')+(commitmentPage.offset+commitmentPage.limit<commitmentPage.total?btn('Next','data-cm-page="'+(commitmentPage.offset+commitmentPage.limit)+'"','secondary'):'')+'</div>'+(commitmentDetail?commitmentDetailView(commitmentDetail):'');
}

async function reviewCommitment(action){
 const item=commitmentDetail;
 const args={action,expected_version:item.version,note:$('#cm-note').value};
 if(action==='complete')args.evidence_id=$('#cm-completion-evidence')?.value||'';
 if(action==='merge'){
  if(!commitmentMergeTarget||$('#cm-merge-id').value.trim()!==commitmentMergeTarget.id)throw Error('Compare the selected target before merging.');
  args.target_id=commitmentMergeTarget.id;args.target_version=commitmentMergeTarget.version;
 }
 try{commitmentDetail=await api('commitments/'+encodeURIComponent(item.id),args)}
 catch(error){commitmentDetail=await api('commitments/'+encodeURIComponent(item.id));await loadCommitments();commitmentMergeTarget=null;renderCommitments(true);throw error}
 commitmentMergeTarget=null;await loadCommitments();await refresh(true);toast('Commitment updated. External actions require separate approval.');
}

function bindCommitments(){
 if(!$('#cm-list'))return;
 $('#cm-search-form').onsubmit=handle(async e=>{e.preventDefault();commitmentQuery=$('#cm-query').value;commitmentFilter=$('#cm-filter').value;commitmentPage.offset=0;await loadCommitments();render()});
 document.querySelectorAll('[data-cm-open]').forEach(button=>button.onclick=handle(async()=>{commitmentDetail=await api('commitments/'+encodeURIComponent(button.dataset.cmOpen));commitmentMergeTarget=null;renderCommitments(true);$('#cm-detail')?.scrollIntoView({block:'start'})}));
 document.querySelectorAll('[data-cm-page]').forEach(button=>button.onclick=handle(async()=>{commitmentPage.offset=+button.dataset.cmPage;await loadCommitments();render()}));
 document.querySelectorAll('[data-cm-action]').forEach(button=>button.onclick=handle(async()=>{button.disabled=true;try{await reviewCommitment(button.dataset.cmAction)}finally{button.disabled=false}}));
 if($('#cm-reload'))$('#cm-reload').onclick=handle(async()=>{commitmentDetail=await api('commitments/'+encodeURIComponent(commitmentDetail.id));commitmentMergeTarget=null;renderCommitments(true)});
 if($('#cm-close'))$('#cm-close').onclick=()=>{commitmentDetail=null;commitmentMergeTarget=null;renderCommitments(true)};
 if($('#cm-edit-form'))$('#cm-edit-form').onsubmit=handle(async e=>{
  e.preventDefault();const item=commitmentDetail;
  const fields={owner:$('#cm-owner').value,deliverable:$('#cm-deliverable').value,scope:$('#cm-scope').value,deadline_text:$('#cm-deadline-text').value,deadline_date:$('#cm-deadline-date').value,deadline_certainty:$('#cm-certainty').value,deadline_basis:$('#cm-basis').value};
  commitmentDetail=await api('commitments/'+encodeURIComponent(item.id),{action:'edit',expected_version:item.version,fields});commitmentMergeTarget=null;await loadCommitments();render();toast('Corrections saved.');
 });
 if($('#cm-compare'))$('#cm-compare').onclick=handle(async()=>{const target=await api('commitments/'+encodeURIComponent($('#cm-merge-id').value.trim()));if(target.id===commitmentDetail.id||!['proposed','open'].includes(target.status))throw Error('Choose another proposed or open commitment.');commitmentMergeTarget=target;render()});
}
