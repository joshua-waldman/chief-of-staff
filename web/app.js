
const $=s=>document.querySelector(s),token=$('meta[name=cos-token]').content;
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const names={schedules:'Scheduled tasks',overview:'Overview',jobs:'Job tracker',attention:'Needs attention',results:'Ready for review',organizations:'Organizations',knowledge:'Knowledge base',commitments:'Commitments',connections:'Connections',acceptance:'Acceptance tests'};
const labels={waiting_capacity:'Waiting for model capacity',capacity_paused:'Model capacity needs attention',queued:'Queued',running:'Running',waiting_children:'Delegated',waiting_input:'Your input needed',awaiting_approval:'Approval needed',blocked:'Setup needed',completed:'Completed',failed:'Failed',cancelled:'Cancelled',watching:'Watching',needs_attention:'Needs attention',paused:'Paused'};
let queueChanging=false;
let returnedResults=[],knownResults=null;
let atlassian=null;
let organizationAgents=[];
let memories=[],memoryQuery='',memoryFilter='',editingMemory=null;
let view='overview',state={},jobs=[],con=null,tests=[],selected=null,busy=false;
const badge=s=>'<span class="badge '+esc(s)+'">'+esc(labels[s]||s)+'</span>';
const date=s=>new Date(s).toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
const attention=j=>['waiting_input','awaiting_approval','blocked','failed','capacity_paused'].includes(j.status);
const btn=(text,attrs='',cls='primary')=>'<button class="'+cls+'" '+attrs+'>'+text+'</button>';
async function api(path,body){const r=await fetch('/api/'+path,{method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json','X-COS-Token':token},body:body===undefined?undefined:JSON.stringify(body)});const d=await r.json();if(!r.ok)throw Error(r.status===401?'The server restarted. Refresh this page to reconnect.':d.error||d.detail||'Request failed');return d}
function toast(s,error=false){$('#toast').textContent=s;$('#toast').style.background=error?'#9d5145':'#244d35';$('#toast').style.display='block';setTimeout(()=>$('#toast').style.display='none',6500)}
function intro(title,subtitle){return '<div class="eyebrow">YOUR COMMAND CENTER</div><div class="heading"><h1>'+title+'</h1><span class="date">'+new Date().toLocaleDateString(undefined,{weekday:'long',month:'long',day:'numeric'})+'</span></div><p class="intro">'+subtitle+'</p>'}
function actionPreview(action,args){
 if(action==='calendar_rsvp')return readable('Event',args.event_subject)+readable('Starts',typeof args.event_start==='object'?JSON.stringify(args.event_start):args.event_start)+readable('Response',args.response)+readable('Notify organizer',args.send_response?'Yes':'No')+(args.comment?readable('Comment',args.comment):'')+(args.event_type?readable('Event type',args.event_type):'')+'<details><summary>Exact event and action</summary><pre class="code">'+esc(JSON.stringify(args,null,2))+'</pre></details>';
 if(['delete_email','mark_email_read'].includes(action))return readable('Subject',args.subject)+readable('From',typeof args.from==='object'?JSON.stringify(args.from):args.from)+readable('Message ID',args.id)+(action==='delete_email'?'<p>Move this message to Deleted Items.</p>':'<p>Mark this message as read.</p>');
 if(action==='mark_teams_read')return readable('Teams chat',args.chat_id)+'<p>Mark the entire chat as read for your signed-in account.</p>';
 if(action==='create_feature_branch')return readable('Project',args.project)+readable('New branch',args.branch);
 return '<details><summary>Exact action details</summary><pre class="code">'+esc(JSON.stringify(args,null,2))+'</pre></details>';
}
function orgCards(){const icons=['↗','⌘','✧','⌨','◇'];return '<div class="org-grid">'+state.organizations.map((o,i)=>'<article class="org-card"><div class="org-icon">'+(icons[i]||'◈')+'</div><h3>'+esc(o.name)+'</h3><p>'+esc(o.purpose)+'</p><div class="org-foot"><span>'+esc(o.leader)+'</span><b>'+jobs.filter(j=>j.org===o.name&&j.status==='running').length+' active</b></div></article>').join('')+'</div>'}
function agentNode(agent){
 const summary=agent.status_summary||labels[agent.status]||agent.status;
 return '<article class="agent-node"><div class="agent-meta"><span>'+esc(agent.parent_id?'Sub-agent':'Director task')+' · '+esc(agent.id.slice(0,8))+'</span>'+badge(agent.status)+'</div>'+btn(esc(agent.title),'data-job="'+esc(agent.id)+'"','agent-title')+'<p class="agent-summary">'+esc(summary)+'</p>'+(agent.next_step?'<p class="agent-next"><b>Next:</b> '+esc(agent.next_step)+'</p>':'')+'<div class="small">'+esc(agent.project||'Workspace')+' · Updated '+esc(date(agent.updated))+'</div>'+(agent.parent_id?'<div class="agent-parent">Delegated by '+btn(esc((agent.parent_org||'Parent agent')+' · '+(agent.parent_title||agent.parent_id.slice(0,8))),'data-job="'+esc(agent.parent_id)+'"','link-btn')+'</div>':'')+'</article>';
}
function agentRoster(org,index){
 const agents=organizationAgents.filter(a=>a.org===org),current=agents.filter(a=>!['completed','cancelled'].includes(a.status)),history=agents.filter(a=>['completed','cancelled'].includes(a.status));
 const running=current.filter(a=>a.status==='running').length;
 return '<div class="agent-roster"><p class="roster-count">'+current.length+' deployed · '+running+' running · '+current.filter(a=>a.parent_id).length+' sub-agents</p>'+(current.length?'<ul class="agent-branches">'+current.map(a=>'<li>'+agentNode(a)+'</li>').join('')+'</ul>':'<p class="agent-empty">No agents currently deployed.</p>')+(history.length?'<details class="agent-history" id="agent-history-'+index+'"><summary>Finished agents ('+history.length+')</summary><ul class="agent-branches">'+history.map(a=>'<li>'+agentNode(a)+'</li>').join('')+'</ul></details>':'')+'</div>';
}
function organizationChart(){
 const icons=['↗','⌘','✧','⌨','◇'];
 return '<section class="organization-chart" aria-label="Agent organization chart"><div class="panel chief-node"><div class="eyebrow">TOP LEVEL</div><h2>Chief of Staff</h2><p class="small">Routes requests · Coordinates directors · Returns questions and approvals · Synthesizes results</p>'+(organizationAgents.some(a=>a.org==='Chief of Staff')?agentRoster('Chief of Staff','chief'):'')+'</div><div class="org-grid org-chart-grid">'+state.organizations.map((o,i)=>'<section class="org-card org-branch" aria-label="'+esc(o.name)+' agents"><div class="org-icon" aria-hidden="true">'+(icons[i]||'◈')+'</div><h2>'+esc(o.name)+'</h2><p>'+esc(o.purpose)+'</p><div class="director-node"><div class="eyebrow">DIRECTOR</div><h3>'+esc(o.leader)+'</h3></div>'+agentRoster(o.name,i)+'</section>').join('')+'</div></section>';
}
const triageLabels={reading:'Summarized for My Reading',response:'Response Needed',action:'Action Needed',response_action:'Response and Action Needed',ignore:'Can Be Ignored and Marked Read',review:'Needs Review'};
function triageLabel(category){return triageLabels[category]||''}
function queueMatches(j,search,status,category){return (!status||j.status===status)&&(!category||j.triage_category===category)&&((j.display_name||'')+j.title+j.org+j.project+(j.triage_summary||'')).toLowerCase().includes(search.toLowerCase())}
function itemTiming(item,now=new Date()){
 if(['completed','cancelled','dismissed','merged'].includes(item.status))return null;
 const b=item.brief||item,deadline=Date.parse(b.deadline_at||''),remaining=deadline-now.getTime();
 if(Number.isFinite(deadline)&&remaining<=48*60*60*1000)return {level:remaining<0?'overdue':'soon',label:remaining<0?'Overdue':'Due soon',reason:(b.timing_reason?b.timing_reason+' · ':'')+'Due '+date(b.deadline_at)};
 return b.time_sensitive==='yes'?{level:'soon',label:'Time sensitive',reason:b.timing_reason||'Prompt attention needed'}:null;
}
function timingClass(item){const timing=itemTiming(item);return timing?'timing-'+timing.level:''}
function timingBadge(item){const timing=itemTiming(item);return timing?'<span class="timing-badge timing-'+timing.level+'" title="'+esc(timing.reason)+'">◷ '+esc(timing.label)+'</span>':''}
function timingCell(item){
 const b=item.brief||item,timing=itemTiming(item);
 if(['completed','cancelled'].includes(item.status))return '<span class="small">—</span>';
 const label=timing?timing.label:b.time_sensitive==='no'?'Routine':b.time_sensitive==='unknown'?'Timing unclear':'Not yet assessed';
 const reason=timing?timing.reason:b.timing_reason||'Timing assessment is pending. This does not mean the item is routine.';
 return '<span class="timing-badge '+(timing?'timing-'+timing.level:'timing-neutral')+'">'+esc(label)+'</span><div class="small timing-reason">'+esc(reason)+'</div>';
}
function table(list){return list.length?'<div class="table-wrap"><table><thead><tr><th>REQUEST</th><th>ORGANIZATION</th><th>STATUS</th><th>TIMING</th><th>UPDATED</th></tr></thead><tbody>'+list.map(j=>'<tr class="'+timingClass(j)+'" data-job="'+j.id+'" tabindex="0"><td><div class="job-title">'+esc(j.display_name||j.title)+'</div>'+(j.triage_category?'<div class="tag">'+esc(triageLabel(j.triage_category))+'</div>':'')+(j.triage_summary?'<details class="small"><summary>View summary</summary>'+esc(j.triage_summary)+'</details>':'')+'<div class="small">'+esc(j.project||'Workspace')+(j.parent_id?' · Sub-agent':'')+'</div></td><td>'+esc(j.org)+'</td><td>'+badge(j.status)+'</td><td class="timing-cell">'+timingCell(j)+'</td><td>'+date(j.updated)+'</td></tr>').join('')+'</tbody></table></div>':'<div class="panel empty"><div class="empty-symbol">◇</div><h3>No requests here yet</h3><p>Give your Chief of Staff a request to start moving work forward.</p></div>'}
function monitoringAlert(){
 if(!state.monitor_attention)return '';
 const m=state.monitor||{}, sources=m.sources||{};
 return '<section class="warning" role="alert"><h2>Message monitoring needs attention</h2><p>Some incoming messages may be waiting for collection. Successful sources continue; failed sources retry from their saved checkpoints.</p><p>Last complete scan: '+esc(m.last_poll?date(m.last_poll):'Not recorded')+'</p>'+Object.entries(sources).map(([name,s])=>'<p><b>'+esc(name==='email'?'Email':'Teams')+':</b> '+esc(labels[s.state]||s.state)+' · Last successful scan: '+esc(s.last_poll?date(s.last_poll):'Not recorded')+'</p>').join('')+'<details><summary>Failure details</summary><pre class="code">'+esc(m.error||'A previous scan failed; recovery is being checked.')+'</pre></details>'+btn('Open monitoring settings','data-go="connections"','secondary')+'</section>';
}
function composer(){return '<form class="composer" id="request-form"><div class="composer-top"><span class="spark">✧</span> CHIEF OF STAFF <span class="pill">Ready to help</span></div><textarea id="request" aria-label="Your request" placeholder="What would you like to move forward?" required></textarea><div class="file-chips" id="file-chips"></div><div class="composer-bottom"><div class="composer-left"><label class="attach">＋ Attach files or screenshots<input type="file" id="attachments" multiple></label><select id="project" aria-label="Project"><option value="">Let the Chief of Staff route</option>'+Object.keys(state.settings.projects).map(p=>'<option>'+esc(p)+'</option>').join('')+'</select></div>'+btn('Delegate request ↗','type="submit" id="submit"')+'</div></form><p class="hint">Your Chief of Staff coordinates the right organization, tracks progress, and brings decisions back to you.</p>'}
function renderQueueControl(){
 const paused=!!state.settings?.queue_paused,active=state.queue_active??state.running??0;
 const status=$('#queue-status'),button=$('#queue-toggle');
 status.textContent=paused?(active?'Finishing current agent steps ('+active+')':'Disconnected · Queue paused'):'Connected · Queue active';
 button.textContent=paused?'Connect':'Disconnect';
 button.disabled=queueChanging;
 button.title=paused?'Resume queued jobs':'Pause between agent steps after in-flight calls finish. Monitoring continues collecting requests.';
 button.onclick=handle(toggleQueue);
}
async function toggleQueue(){
 if(queueChanging)return;
 const paused=!state.settings.queue_paused;
 queueChanging=true;renderQueueControl();
 try{
  const saved=await api('settings',{queue_paused:paused});
  state.settings={...state.settings,queue_paused:saved.queue_paused};
  toast(paused?'Queue disconnecting. Agents will pause after their current step; progress is saved.':'Queue connected. Queued work will resume.');
  await refresh();
 }finally{queueChanging=false;renderQueueControl()}
}
function resultsNotice(){
 const count=returnedResults.length;
 return count?'<section class="panel result-notice" role="status"><h2>'+count+' completed '+(count===1?'result is':'results are')+' ready for review</h2><p>Responses and deliverables stay here until you mark them reviewed.</p>'+btn('Review completed work →','data-go="results"')+'</section>':'';
}
function resultSummary(summary,files=[]){
 const text=String(summary||''),pattern=/\[([^\]\n]+)\]\(\/api\/files\/([A-Za-z0-9_-]+)\)|\/api\/files\/([A-Za-z0-9_-]+)/g;
 let html='',end=0;
 for(const match of text.matchAll(pattern)){
  const id=match[2]||match[3],file=files.find(f=>f.id===id);
  html+=esc(text.slice(end,match.index))+'<a class="review-file-link" href="/api/files/'+encodeURIComponent(id)+'" target="_blank" rel="noopener">'+esc(file?.name||match[1]||'Open review file')+'</a>';
  end=match.index+match[0].length;
 }
 return html+esc(text.slice(end));
}
function resultsView(){
 return intro('Your completed work, ready to review.','Read the response, open its deliverables, and mark it reviewed when you are done.')+(returnedResults.length?returnedResults.map(r=>'<article class="panel"><div class="eyebrow">'+esc(r.org)+' · '+esc(date(r.completed))+'</div><h2>'+esc(r.display_name||r.title)+'</h2><p class="result">'+resultSummary(r.summary)+'</p>'+btn('Review response & deliverables','data-job="'+esc(r.id)+'"')+'</article>').join(''):'<section class="panel empty"><h2>All caught up</h2><p>New completed responses and deliverables will appear here.</p></section>');
}
function returnedResultView(j){
 const r=j.result_review;
 if(!r)return '';
 const files=(r.files||j.files).filter(f=>(r.file_ids.includes(f.id)||r.summary.includes('/api/files/'+f.id))&&f.purpose!=='recovery');
 return '<section class="panel returned-result"><h3>'+(r.reviewed?'Reviewed result':'Completed · Ready for your review')+'</h3><p class="small">Returned '+esc(date(r.completed))+'</p>'+(j.status&&j.status!=='completed'?'<p class="small">This saved result remains available while COS handles new activity.</p>':'')+'<div class="readable">'+resultSummary(r.summary,files)+'</div>'+jobFilesView(files)+(r.reviewed?'<p class="small">Marked reviewed '+esc(date(r.reviewed))+'</p>':'<p class="small">This stays in Ready for review until you mark it reviewed.</p>'+btn('Mark reviewed','id="mark-result-reviewed"'))+'</section>';
}
function notifyResults(results){
 const revisions=new Set(results.map(r=>r.revision));
 if(knownResults!==null){const fresh=results.filter(r=>!knownResults.has(r.revision));if(fresh.length)toast(fresh.length===1?'Completed: '+(fresh[0].display_name||fresh[0].title)+'. Open Ready for review.':fresh.length+' jobs completed. Open Ready for review.');}
 knownResults=new Set([...(knownResults||[]),...revisions]);
}
function render(){
 renderQueueControl();
 $('#results-count').textContent=returnedResults.length;
 const savedView=$('#main').dataset.view===view?captureView($('#main')):null;$('#main').dataset.view=view;
 $('#breadcrumb').textContent='Workspace / '+names[view];$('#attention-count').textContent=state.needs_attention||0;$('#monitor-pill').textContent=state.monitor?.state==='watching'?(state.monitor?.limitations?.length?'● Monitoring active · access limits':'● Monitoring active'):state.monitor_attention?'Monitoring needs attention':state.settings?.monitor_enabled?'Monitoring: collecting…':'Monitoring paused';
 document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===view));
 let html='';
 if(view==='overview'){
 html=intro('A little more focus.<br>A lot more forward.','One place to turn your priorities into coordinated work.');
 if(!state.gateway_configured)html+='<div class="warning">Connect your AMD gateway key to activate agents. '+btn('Set up connections','data-go="connections"','link-btn')+'</div>';
 html+=resultsNotice()+monitoringAlert()+capacityAlert()+commitmentAttention()+composer()+'<div class="metrics">'+[['Active agents',state.running,'Working across your organizations'],['Open requests',jobs.filter(j=>!j.parent_id&&!['completed','cancelled'].includes(j.status)).length,'Every request, one persistent track'],['Needs attention',state.needs_attention,'Approvals, questions, or setup'],['Completed',state.completed,'Work finished and recorded']].map(([l,n,t])=>'<div class="metric"><div class="metric-label">'+l+'</div><div class="metric-value">'+n+'</div><small>'+t+'</small></div>').join('')+'</div><div class="section-title"><div><h2>Your organizations</h2><p>Specialized teams. One coordinated effort.</p></div>'+btn('View organization structure →','data-go="organizations"','link-btn')+'</div>'+orgCards()+'<div class="section-title"><h2>Recent requests</h2>'+btn('View all requests →','data-go="jobs"','link-btn')+'</div>'+table(jobs.filter(j=>!j.parent_id).slice(0,6));
 }else if(view==='results'){html=resultsView();
 }else if(view==='jobs'||view==='attention'){
 html=intro(view==='jobs'?'Every request. Every next step.':'The decisions that need you.','Follow progress, answer questions, and review proposed actions.')+(view==='attention'?resultsNotice()+monitoringAlert()+capacityAlert()+commitmentAttention():'')+'<div class="toolbar"><input id="search" aria-label="Search requests" placeholder="Search requests, organizations, or projects…"><select id="filter" aria-label="Filter status"><option value="">All statuses</option>'+Object.entries(labels).map(([k,v])=>'<option value="'+k+'">'+v+'</option>').join('')+'</select><select id="triage-filter" aria-label="Filter message decision"><option value="">All message decisions</option>'+Object.entries(triageLabels).map(([k,v])=>'<option value="'+k+'">'+v+'</option>').join('')+'</select></div><div id="table">'+table(jobs.filter(j=>view!=='attention'||attention(j)))+'</div>';
 }else if(view==='organizations'){
 html=intro('A team built around your work.','Your agent organization chart. Open any task to inspect its work, decisions, and delegated agents. Agents are task-specific; queued and waiting agents remain visible until finished.')+organizationChart()+'<div class="section-title"><h2>Engineering projects</h2></div>'+Object.entries(state.settings.projects).map(([n,p])=>'<div class="panel"><h3>'+esc(n)+'</h3><p class="code">'+esc(p)+'</p></div>').join('');
 }else if(view==='commitments')html=commitmentsView();
 else if(view==='schedules')html=schedulesView();
 else if(view==='knowledge')html=knowledgeView();
 else if(view==='connections')html=connectionView();
 else html=intro('Evidence before confidence.','Create live acceptance requests after connecting services. Automated smoke tests run separately.')+'<div class="warning">Live requests consume AMD gateway usage. Communications sends still require approval.</div><div class="two-col">'+tests.map((t,i)=>'<article class="panel test-card"><div class="eyebrow">LIVE ACCEPTANCE</div><h2>'+esc(t.category)+'</h2><p>'+esc(t.request)+'</p><ol>'+t.checks.map(c=>'<li>'+esc(c)+'</li>').join('')+'</ol>'+btn('Create acceptance request ↗','data-test="'+i+'"')+'</article>').join('')+'</div>';
 if($('#main')._renderedHTML===html)return;
 $('#main').innerHTML=html;$('#main')._renderedHTML=html;bind();restoreView($('#main'),savedView);if($('#search'))$('#search').oninput();
}
function input(id,label,value,type='text',hint=''){return '<div class="field"><label for="'+id+'">'+esc(label)+'</label><input id="'+id+'" type="'+type+'" value="'+esc(value)+'">'+(hint?'<small>'+esc(hint)+'</small>':'')+'</div>'}
function connectionView(){if(!con)return intro('Your tools. Connected.','Loading connection inventory…');return intro('Your tools. Connected.','AMD inference, your existing skills, and a workspace that moves with you.')+monitoringAlert()+
 '<div class="two-col"><section class="panel"><div class="section-title"><h2>AMD model gateway</h2>'+badge(con.gateway.configured?'completed':'blocked')+'</div><p class="small">'+esc(con.gateway.url)+'</p><p class="small">Gateway usage follows AMD access and charge-back rules, separately from ChatGPT subscription billing.</p><form id="key-form">'+input('gateway-key','Gateway API key','','password','Saved in Windows Credential Manager, outside OneDrive. Never returned by the API.')+btn('Save securely','type="submit"')+' '+btn('Test connection','id="test-gateway" type="button"','secondary')+'</form><p id="gateway-result" class="small"></p><p><a href="https://llm.amd.com/key-management" target="_blank" rel="noreferrer">Open AMD API key management ↗</a></p></section>'+
 '<section class="panel"><div class="section-title"><h2>Microsoft 365</h2>'+badge(con.microsoft.authenticated?'completed':'blocked')+'</div><p class="small">Reuses the installed AMD Outlook and Teams skills. One sign-in covers both.</p>'+btn(con.auth_flow.running?'Sign-in in progress':'Start Microsoft sign-in','id="start-auth"')+'<pre id="auth-output" class="code">'+esc(con.auth_flow.output||con.microsoft.nextActionSummary||'Ready')+'</pre><p class="small">Complete authentication on Microsoft’s page. Never enter your password here.</p></section></div>'+
 '<section class="panel"><h2>Jira & Confluence</h2><p>Use your Atlassian Cloud credentials through the installed local skill. Jira routine updates are automatic; Confluence writes require approval.</p><p class="small">Configuration: '+esc(atlassian?.config_path||'User profile / .atlassian/config.json')+'</p><div class="row">'+btn('Open local credential setup','id="setup-atlassian" type="button"')+btn('Test Atlassian connection','id="test-atlassian" type="button"','secondary')+'</div><pre id="atlassian-result" class="code">'+esc(atlassian?.check?.output||'Not checked yet')+'</pre><details><summary>Official setup instructions</summary><pre class="code">'+esc(atlassian?.setup_help||'')+'</pre></details></section>'+
 '<form id="settings-form"><section class="panel"><h2>Models & orchestration</h2><div class="form-grid">'+Object.entries(state.settings.models).map(([r,n])=>input('model-'+r,r+' model',n)).join('')+input('parallel','Parallel agents',state.settings.parallel_agents,'number')+input('model-parallel','Concurrent requests per model',state.settings.model_parallel_requests,'number','1–12 simultaneous requests to each model, shared across agents. Set to 4 to let four agents call the same model concurrently; gateway limits still apply.')+input('steps','Steps before automatic handoff',state.settings.max_steps,'number')+'</div></section>'+
 '<section class="panel"><h2>Timing assessments</h2><div class="field"><label><input type="checkbox" id="timing-assessment" '+(state.settings.timing_assessment_enabled?'checked':'')+'> Assess timing for attention items automatically</label><small>Uses the configured AMD LLM gateway to assess saved requests, briefs, message excerpts and pending-action details. Does not resume work or approve actions.</small></div></section>'+
 '<section class="panel"><h2>Continuous monitoring & remote requests</h2><div class="field"><label><input type="checkbox" id="monitor" '+(state.settings.monitor_enabled?'checked':'')+'> Monitor all accessible email and Teams conversations</label><small>Processes new messages from the time monitoring is enabled, using persistent checkpoints.</small></div><div class="form-grid">'+input('prefix','Self-chat command prefix',state.settings.command_prefix,'text','Example: !cos Prepare a strategy brief for Example Project')+input('interval','Poll interval (seconds)',state.settings.poll_seconds,'number')+'</div><p class="small">Remote commands are accepted only from your verified identity in saved messages. Approvals remain in this app.</p><p>Last successful scan: '+esc(state.monitor?.last_poll?date(state.monitor.last_poll):'Waiting for first scan')+' · '+esc(state.monitor?.conversations??'—')+' conversations checked</p>'+(state.monitor?.limitations?.length?'<div class="warning"><b>'+state.monitor.limitations.length+' Teams conversation(s) could not be read.</b><p>Teams returned HTTP 403 (access denied). Other accessible conversations continue to be monitored. The denial does not reveal whether membership, tenant policy, or integration permissions caused it.</p><p>Check the conversation in Teams. If it opens there, AMD IT can verify this integration’s permissions.</p></div>':'')+'<details><summary>Monitoring diagnostics</summary><pre class="code">'+esc(JSON.stringify(state.monitor,null,2))+'</pre></details></section>'+
 '<section class="panel"><h2>Engineering workflow</h2><p class="small">Optional rules keyed by a registered project name, or * for shared defaults. Fields: branch_prefix, require_ticket, require_feature_branch, jira_parent, jira_issue_type. Empty settings impose no project-specific rules.</p><div class="field"><label for="engineering-workflows">Workflow settings (JSON)</label><textarea id="engineering-workflows">'+esc(JSON.stringify(state.settings.engineering_workflows||{},null,2))+'</textarea></div></section>'+
 '<section class="panel"><h2>Project roots</h2>'+Object.entries(state.settings.projects).map(([n,p],i)=>input('project-'+i,n,p)).join('')+'</section>'+
 '<section class="panel"><h2>GitHub repository accounts</h2><p class="small">'+esc(con.github?.setup||'Sign in each repository account with gh auth login.')+'</p><p class="small">GitHub CLI: '+(con.github?.available?'detected':'not installed')+'. Repository search and source reads use these routes, separately from public research hosts.</p><div class="field"><label for="github-accounts">Repository owner → GitHub login (JSON)</label><textarea id="github-accounts">'+esc(JSON.stringify(state.settings.github_accounts||{},null,2))+'</textarea><small>Example: {"work-organization":"work-login"}. Tokens stay in GitHub CLI’s credential store. Saving does not resume blocked jobs.</small></div></section>'+
 '<section class="panel"><h2>Imported Claude MCP servers</h2><p class="small">Enable reviewed servers to launch them locally. Imported tool calls require approval.</p><ul class="list">'+con.mcp.map(m=>'<li><label><input class="mcp-toggle" type="checkbox" value="'+esc(m.name)+'" '+(m.enabled?'checked':'')+'> '+esc(m.name)+'</label><small>'+esc(m.transport)+'</small></li>').join('')+'</ul></section>'+
 '<section class="panel"><h2>Research hosts</h2><div class="field"><label for="hosts">Exact allowed HTTPS hostnames, one per line</label><textarea id="hosts">'+esc(state.settings.research_hosts.join('\n'))+'</textarea><small>Retrieval sends no source documents or credentials. Redirect destinations must be allowed public HTTPS hosts. Microsoft Learn Graph view and tabs selectors are supported; other queries are blocked.</small></div></section>'+btn('Save configuration','type="submit"')+'</form>'+
 '<section class="panel"><h2>Installed skills</h2><div class="tags">'+con.skills.map(s=>'<span class="tag">'+esc(s.name)+'</span>').join('')+'</div></section><section class="panel"><h2>Execution environment</h2><p class="small">'+(con.sandbox.available?'Docker detected. Sandbox execution has no network or host credentials.':'Docker is not installed. Unrestricted host execution requires approval; isolated execution needs Docker.')+'</p></section>'}
function bindRows(){document.querySelectorAll('[data-job]').forEach(b=>{b.onclick=e=>{if(!e.target.closest('details'))openJob(b.dataset.job)};b.onkeydown=e=>{if(e.key==='Enter'&&!e.target.closest('details'))openJob(b.dataset.job)}})}
function handle(fn){return async e=>{try{await fn(e)}catch(error){toast(error.message,true)}}}
function bind(){
 bindSchedules();
 bindCommitments();
 bindKnowledge();
 if($('#setup-atlassian'))$('#setup-atlassian').onclick=handle(async()=>{await api('atlassian/setup',{});toast('Complete setup in the local PowerShell window, then test the connection here.')});
 if($('#test-atlassian'))$('#test-atlassian').onclick=handle(async()=>{$('#atlassian-result').textContent='Checking Jira and Confluence…';const r=await api('atlassian/test',{});atlassian=await api('atlassian');$('#atlassian-result').textContent=r.output;toast(r.ok?'Atlassian connection passed':'Atlassian needs setup',!r.ok)});

 bindRows();document.querySelectorAll('[data-go]').forEach(b=>b.onclick=()=>navigate(b.dataset.go));
 if($('#search')){const f=()=>{$('#table').innerHTML=table(jobs.filter(j=>(view!=='attention'||attention(j))&&queueMatches(j,$('#search').value,$('#filter').value,$('#triage-filter').value)));bindRows()};$('#search').oninput=f;$('#filter').onchange=f;$('#triage-filter').onchange=f}
 if($('#request-form')){$('#attachments').onchange=()=>$('#file-chips').textContent=[...$('#attachments').files].map(f=>f.name).join(' · ');$('#request-form').onsubmit=handle(submit)}
 document.querySelectorAll('[data-test]').forEach(b=>b.onclick=handle(async()=>{const j=await api('jobs',{request:tests[+b.dataset.test].request});await refresh();await openJob(j.id)}));
 if($('#key-form'))$('#key-form').onsubmit=handle(async e=>{e.preventDefault();await api('gateway/key',{key:$('#gateway-key').value});$('#gateway-key').value='';toast('Key saved securely');con=await api('connections');await refresh(true)});
 if($('#test-gateway'))$('#test-gateway').onclick=handle(async()=>{$('#gateway-result').textContent='Testing AMD connection…';try{const r=await api('gateway/test',{});$('#gateway-result').textContent='Connected · '+r.model+' · '+r.response}catch(e){$('#gateway-result').textContent=e.message}});
 if($('#start-auth'))$('#start-auth').onclick=handle(async()=>{await api('auth/start',{});toast('Sign-in started. Follow the instructions in this panel.')});
 if($('#settings-form'))$('#settings-form').onsubmit=handle(async e=>{e.preventDefault();const models=Object.fromEntries(Object.keys(state.settings.models).map(r=>[r,$('#model-'+r).value.trim()]));const projects=Object.fromEntries(Object.keys(state.settings.projects).map((n,i)=>[n,$('#project-'+i).value]));await api('settings',{models,projects,engineering_workflows:JSON.parse($('#engineering-workflows').value),github_accounts:JSON.parse($('#github-accounts').value),parallel_agents:+$('#parallel').value,model_parallel_requests:+$('#model-parallel').value,max_steps:+$('#steps').value,monitor_enabled:$('#monitor').checked,timing_assessment_enabled:$('#timing-assessment').checked,poll_seconds:+$('#interval').value,command_prefix:$('#prefix').value.trim(),mcp_enabled:[...document.querySelectorAll('.mcp-toggle:checked')].map(x=>x.value),research_hosts:$('#hosts').value.split('\n').map(x=>x.trim()).filter(Boolean)});toast('Configuration saved');con=await api('connections');await refresh(true)});
}
async function submit(e){e.preventDefault();$('#submit').disabled=true;try{const files=await Promise.all([...$('#attachments').files].map(f=>new Promise((resolve,reject)=>{if(f.size>25*1024*1024)return reject(Error('Each attachment must be under 25 MB'));const r=new FileReader();r.onload=()=>resolve({name:f.name,data:r.result.split(',')[1]});r.onerror=reject;r.readAsDataURL(f)})));const j=await api('jobs',{request:$('#request').value,project:$('#project').value,files});$('#request').value='';$('#attachments').value='';toast('Request received by your Chief of Staff');await refresh(true);await openJob(j.id)}finally{if($('#submit'))$('#submit').disabled=false}}
const sourceLoads=new Set(),sourceErrors=new Map();
async function loadSource(id){
 if(sourceLoads.has(id))return;
 sourceLoads.add(id);sourceErrors.delete(id);
 if(selected===id&&$('#load-source')){$('#load-source').disabled=true;$('#load-source').textContent='Loading full text…'}
 try{const result=await api('jobs/'+id+'/source',{});sourceErrors.set(id,result.errors||[])}
 catch(e){sourceErrors.set(id,['Full text could not be loaded. Check the connection and retry.'])}
 finally{sourceLoads.delete(id);if(selected===id&&$('#job-dialog').open)await openJob(id)}
}
function sourceView(j){
 const messages=[...(j.source_messages||[])].sort((a,b)=>(Date.parse(b.received)||0)-(Date.parse(a.received)||0)||String(b.message_id||'').localeCompare(String(a.message_id||''),undefined,{numeric:true})).slice(0,5),errors=sourceErrors.get(j.id)||[];
 const collapse=btn('Collapse conversation','data-collapse-source aria-controls="source-conversation"','secondary');
 return '<section class="panel"><details id="source-conversation"><summary class="source-summary">Original messages <span class="small">('+messages.length+' · newest first)</span></summary>'+collapse+'<p class="small">Showing the latest five messages or emails.</p>'+messages.map((m,i)=>'<article class="source-message">'+readable('From',m.sender)+ (m.subject?readable('Subject',m.subject):'')+readable('Received',m.received)+'<div class="brief-label">'+(m.complete?'Full message':'Preview — full text not loaded')+'</div><div id="source-message-'+i+'" class="readable source-text">'+esc(m.body||'(Empty message)')+'</div></article>').join('')+(!messages.length?'<p class="small">Original message text has not been loaded yet.</p>':'')+errors.map(e=>'<p class="warning" role="alert">'+esc(e)+'</p>').join('')+'<div class="row">'+btn(sourceLoads.has(j.id)?'Loading full text…':'Load original messages','id="load-source" '+(sourceLoads.has(j.id)?'disabled':''),'secondary')+collapse+'</div></details></section>';
}
function collapseSourceConversation(){
 const conversation=$('#source-conversation');
 conversation.open=false;
 const summary=conversation.querySelector('summary');
 summary.focus({preventScroll:true});
 summary.scrollIntoView({block:'nearest'});
}
function capacityAlert(){
 const count=jobs.filter(j=>j.status==='capacity_paused').length;
 if(!count)return '';
 return '<section class="warning" role="alert"><h2>Model capacity needs attention</h2><p>Automatic recovery paused for '+count+' job(s). Progress is saved. Review AMD gateway capacity, then open an affected job to retry.</p></section>';
}
function capacityView(j){
 const c=j.capacity||{},paused=j.status==='capacity_paused'||c.paused;
 if(!['waiting_capacity','capacity_paused'].includes(j.status))return '';
 const seconds=Math.max(0,Math.ceil((c.retry_at*1000-Date.now())/1000));
 return '<section class="warning" role="status"><h3>'+ (paused?'Model capacity recovery paused':'Waiting for model capacity')+'</h3><p>Progress is saved. '+(paused?'Automatic retries reached the recovery window. Review AMD gateway capacity or retry when ready.':'We’ll retry automatically '+(seconds?'in '+seconds+' seconds.':'when a worker is available.'))+'</p>'+btn('Retry now','id="retry-capacity"','secondary')+'<details><summary>Gateway diagnostics</summary><div class="readable">'+esc(c.model)+' · '+esc(c.detail||'Model request concurrency limit reached.')+'</div></details></section>';
}
function jobFilesView(files){
 const deliverables=files.filter(f=>f.purpose!=='recovery'),recovery=files.filter(f=>f.purpose==='recovery');
 const links=items=>items.map(f=>'<a class="file-link" href="/api/files/'+encodeURIComponent(f.id)+'">↧ '+esc(f.name)+'</a>').join('');
 return (deliverables.length?'<h3>Deliverables</h3>'+links(deliverables):'')+
  (recovery.length?'<details id="recovery-files"><summary>Internal recovery files ('+recovery.length+')</summary><p class="small">Saved history, checkpoints, and archived content support job recovery. Older files may still be referenced by newer checkpoints. Older snapshots are kept losslessly in recovery-archive.zip; existing paths and download links continue to work.</p>'+links(recovery)+'</details>':'');
}
async function openJob(id,{background=false}={}){
 const j=await api('jobs/'+id);
 // Read the live view after the request: the user may have scrolled or closed it while waiting.
 if(background&&(selected!==id||!$('#job-dialog').open||['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName)||document.querySelector('#detail button[disabled]')))return;
 const same=selected===id,opening=!same||!$('#job-dialog').open;
 const snapshot=opening?null:captureView($('#job-dialog'));
 const b=j.brief||{},html='<div class="eyebrow">'+esc(j.org)+'</div><h2 class="detail-title">'+esc(j.display_name||b.job_name||'Job details')+'</h2><div class="detail-meta">'+badge(j.status)+timingBadge(j)+'<span class="small">'+esc(j.project||'Workspace')+' · '+date(j.created)+'</span></div><details><summary class="small">Original request</summary><div class="code">'+esc(j.request)+'</div></details>'+
 returnedResultView(j)+'<section class="panel job-brief"><h3>At a glance</h3>'+(j.org==='Communications'?readable('Message decision',triageLabel(b.triage_category))+readable('Why this decision',b.triage_reason||b.action_reason||b.response_reason)+readable('Quick summary',b.email_excerpt):'')+readable('Time sensitivity',itemTiming(j)?.label||({no:'Routine',unknown:'Timing unclear'})[b.time_sensitive]||'Not yet assessed')+readable('Timing rationale',itemTiming(j)?.reason||b.timing_reason||'Timing assessment is pending; urgency is not yet known.')+readable('Context',b.context)+readable('Current status',b.status_summary)+readable('Next step',b.next_step)+'</section>'+(j.org==='Communications'?sourceView(j):'')+
 (j.org==='Communications'?'<section class="panel"><h3>Source email / conversation</h3><div class="form-grid">'+readable('From',b.email_from)+readable('Subject',b.email_subject)+readable('Received',b.email_received)+'</div>'+readable('Email summary',b.email_excerpt)+'<div class="form-grid">'+readable('Response needed',({yes:'Yes',no:'No',unknown:'Not yet determined'})[b.response_needed]||'Not yet determined')+readable('Why',b.response_reason)+'</div><div class="form-grid">'+readable('Action needed',({yes:'Yes',no:'No',unknown:'Not yet determined'})[b.action_needed]||'Not yet determined')+readable('Action rationale',b.action_reason)+'</div>'+readable('Requested work',b.requested_action)+readable('Action plan',b.action_plan)+(b.uncertainties?readable('Still unverified',b.uncertainties):'')+'</section>':'')+
 (b.proposed_body?'<section class="panel"><h3>'+(j.status==='completed'?'Response text':'Proposed response')+'</h3>'+readable('To',b.proposed_to)+readable('Subject',b.proposed_subject)+readable(j.status==='completed'?'Message text':'Draft reply',b.proposed_body,'proposed-reply')+'<p class="small">'+(j.status==='completed'?'See the result below for the execution outcome and receipt.':'A draft is not a sent message. Sending requires approval of the exact action below.')+'</p></section>':'')+
 capacityView(j)+
 (j.question&&['waiting_input','blocked','failed'].includes(j.status)?'<section class="warning"><h3>Decision or information needed</h3>'+(j.question.length>650?'<p>'+esc(b.next_step||'Review the details and reply below.')+'</p><details><summary>Full agent question and supporting context</summary><div class="readable">'+esc(j.question)+'</div></details>':'<div class="readable">'+esc(j.question)+'</div>')+'</section>':'')+
 (j.org!=='Communications'&&['waiting_input','blocked','failed'].includes(j.status)?'<form id="answer-form"><div class="field"><label for="answer">Reply to your Chief of Staff</label><textarea id="answer" required placeholder="Provide the missing information or instructions to retry…"></textarea></div>'+btn('Send follow-up & resume','type="submit"')+'</form>':'')+
 (j.read_targets||[]).map(t=>btn(t.label,'data-summary-read="'+esc(t.args.id||t.args.chat_id)+'" data-read-tool="'+esc(t.tool)+'"','secondary')).join('')+
 reviewView(j)+
 j.approvals.filter(a=>['pending','uncertain'].includes(a.status)).map(a=>{const args=JSON.parse(a.args);return '<section class="approval"><h3>'+esc(a.status==='uncertain'?'Action outcome needs verification':a.tool==='delegate'?'Start delegated work':({calendar_rsvp:'Respond to calendar invitation',delete_email:'Delete email',mark_email_read:'Mark email as read',mark_teams_read:'Mark Teams chat as read',create_feature_branch:'Create feature branch'}[a.tool]||a.tool)+' · '+a.risk+' risk')+'</h3><p class="small">'+esc(a.reason)+'</p>'+(['send_email','reply_email','send_teams'].includes(a.tool)?readable('To',args.to||args.chat_id||'Original thread')+(args.cc?readable('Cc',args.cc):'')+(args.bcc?readable('Bcc',args.bcc):'')+readable('Subject',args.subject||b.email_subject):a.tool==='delegate'?'<p>No delegated work starts until you approve. Sending messages requires separate approval.</p>'+args.tasks.map(t=>'<article class="panel">'+readable('Organization',t.org)+readable('Project',t.project)+readable('Task scope and expected result',t.request)+'</article>').join(''):actionPreview(a.tool,args))+''+
 (a.status==='pending'?(args.body&&['send_email','reply_email','send_teams'].includes(a.tool)?'<div class="field"><label for="body-'+a.id+'">Final message body</label><textarea id="body-'+a.id+'">'+esc(args.body)+'</textarea></div>':'')+'<div class="row">'+btn('Approve exact action','data-approve="'+a.id+'"')+btn('Reject & revise','data-reject="'+a.id+'"','secondary')+'</div>':'<p class="small">Verify external state before choosing an outcome. No automatic retry will occur.</p><div class="row">'+btn('Verified: executed','data-resolve="'+a.id+'" data-outcome="executed"')+btn('Verified: did not execute','data-resolve="'+a.id+'" data-outcome="not_executed"','secondary')+'</div>')+'</section>'}).join('')+
 (j.result&&!j.result_review?'<section class="panel"><h3>Result</h3><p class="result">'+esc(j.result)+'</p></section>':'')+
 jobFilesView(j.files)+
 (j.children.length?'<h3>Delegated work</h3>'+j.children.map(c=>'<div class="child" data-child="'+c.id+'">'+badge(c.status)+' '+esc(c.org)+' · '+esc(c.display_name||c.title)+'</div>').join(''):'')+
 '<p><a class="file-link" href="/api/jobs/'+j.id+'/diagnostics" download>Download diagnostics & provenance</a></p><details id="activity-details"><summary>Full activity timeline ('+j.events.filter(e=>e.kind!=="model").length+' events)</summary><div class="section-title"><h3>Activity timeline</h3>'+btn('Refresh','id="refresh-job"','link-btn')+'</div><div class="timeline">'+j.events.filter(e=>e.kind!=='model').map(e=>'<div class="event"><div class="event-head">'+esc(e.kind.toUpperCase())+'<time>'+date(e.created)+'</time></div><div id="event-body-'+e.id+'" class="event-body">'+esc(e.body)+'</div></div>').join('')+'</div></details>'+
 (!['completed','cancelled'].includes(j.status)?btn('Cancel job and sub-agents','id="cancel-job"','danger'):'');
 if(!opening&&$('#detail')._renderedHTML===html)return;
 selected=id;$('#detail').innerHTML=html;$('#detail')._renderedHTML=html;
 if(!$('#job-dialog').open)$('#job-dialog').showModal();if(snapshot)restoreView($('#job-dialog'),snapshot);else $('#job-dialog').scrollTop=0;$('#refresh-job').onclick=()=>openJob(id);
 if($('#answer-form'))$('#answer-form').onsubmit=handle(async e=>{e.preventDefault();await api('jobs/'+id+'/answer',{answer:$('#answer').value});await openJob(id);await refresh()});
 document.querySelectorAll('[data-child]').forEach(b=>b.onclick=()=>openJob(b.dataset.child));
 document.querySelectorAll('[data-approve],[data-reject]').forEach(b=>b.onclick=handle(async()=>{b.disabled=true;try{const aid=b.dataset.approve||b.dataset.reject,body=$('#body-'+aid);await api('approvals/'+aid,{approved:!!b.dataset.approve,...(body?{edited:{body:body.value}}:{})});await openJob(id);await refresh()}finally{b.disabled=false}}));
 document.querySelectorAll('[data-resolve]').forEach(b=>b.onclick=handle(async()=>{await api('approvals/'+b.dataset.resolve+'/resolve',{outcome:b.dataset.outcome});await openJob(id);await refresh()}));
 bindReview(id);
 if($('#mark-result-reviewed'))$('#mark-result-reviewed').onclick=handle(async()=>{
  const button=$('#mark-result-reviewed');button.disabled=true;
  try{await api('jobs/'+id+'/result-review',{revision:j.result_review.revision});await openJob(id);await refresh();toast('Result marked reviewed');}finally{button.disabled=false;}
 });
 document.querySelectorAll('[data-summary-read]').forEach(b=>b.onclick=handle(async()=>{b.disabled=true;try{await api('jobs/'+id+'/mark-read',{tool:b.dataset.readTool,target_id:b.dataset.summaryRead});await openJob(id);await refresh()}finally{b.disabled=false}}));
 if($('#load-source'))$('#load-source').onclick=()=>loadSource(id);
 document.querySelectorAll('[data-collapse-source]').forEach(button=>button.onclick=collapseSourceConversation);
 if(opening&&j.org==='Communications'&&(!(j.source_messages||[]).length||(j.source_messages||[]).some(m=>!m.complete)))loadSource(id);
 if($('#retry-capacity'))$('#retry-capacity').onclick=handle(async()=>{await api('jobs/'+id+'/retry-capacity',{});await openJob(id);await refresh()});
 if($('#cancel-job'))$('#cancel-job').onclick=handle(async()=>{await api('jobs/'+id+'/cancel',{});await openJob(id);await refresh()});
}
async function navigate(next){view=next;if(view==='schedules')await loadSchedules();if(view==='commitments')await loadCommitments();if(view==='connections'){con=await api('connections');atlassian=await api('atlassian');}if(view==='acceptance')tests=await api('acceptance');if(view==='knowledge')memories=await api('knowledge');if(view==='organizations')organizationAgents=await api('organization-agents');render()}
async function refresh(force=false){const [nextState,nextJobs,nextResults,nextAgents]=await Promise.all([api('status'),api('jobs'),api('results'),view==='organizations'?api('organization-agents'):Promise.resolve(null)]);state=nextState;renderQueueControl();jobs=nextJobs;notifyResults(nextResults);returnedResults=nextResults;$('#results-count').textContent=returnedResults.length;if(nextAgents!==null)organizationAgents=nextAgents;if(view==='knowledge'&&!['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName))memories=await api('knowledge');if(view==='schedules')await loadSchedules();if(view==='commitments'&&!['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName))await loadCommitments();if(force||!['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName))render()}
document.querySelectorAll('[data-view]').forEach(b=>b.onclick=handle(()=>navigate(b.dataset.view)));$('#close-dialog').onclick=()=>{$('#job-dialog').close();selected=null};$('#job-dialog').addEventListener('close',()=>selected=null);
refresh().catch(e=>toast(e.message,true));
setInterval(async()=>{if(busy)return;busy=true;try{await refresh();if(view==='connections'&&!['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName)){con=await api('connections');render()}if(selected&&$('#job-dialog').open&&!['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName)&&!document.querySelector('#detail button[disabled]'))await openJob(selected,{background:true})}catch(e){}finally{busy=false}},4000);

function captureView(root){
 if(!root)return null;
 const elements=[root,...root.querySelectorAll('*')];
 return {checks:elements.filter(el=>el.type==='checkbox'&&el.id).map(el=>({id:el.id,checked:el.checked})),fields:elements.filter(el=>['INPUT','TEXTAREA','SELECT'].includes(el.tagName)&&!['password','file','checkbox'].includes(el.type)).map(el=>({id:el.id,value:el.value,height:el.style.height})),scroll:elements.map((el,i)=>({i,id:el.id,x:el.scrollLeft,y:el.scrollTop})).filter(p=>p.x||p.y),details:[...root.querySelectorAll('details')].filter(el=>el.open).map(el=>el.id||el.querySelector('summary')?.textContent),windowX:window.scrollX,windowY:window.scrollY};
}
function restoreView(root,snapshot){
 if(!root||!snapshot)return;
 for(const el of root.querySelectorAll('details'))el.open=snapshot.details.includes(el.id||el.querySelector('summary')?.textContent);
 for(const c of snapshot.checks||[]){const el=document.getElementById(c.id);if(el&&root.contains(el))el.checked=c.checked}
 for(const f of snapshot.fields||[]){const el=f.id&&document.getElementById(f.id);if(el&&root.contains(el)){el.value=f.value;if(f.height)el.style.height=f.height}}
 const elements=[root,...root.querySelectorAll('*')];
 for(const p of snapshot.scroll){const el=p.id?document.getElementById(p.id):elements[p.i];if(el){el.scrollLeft=p.x;el.scrollTop=p.y}}
 window.scrollTo(snapshot.windowX,snapshot.windowY);
}
function readable(label,value,id=''){return '<div class="brief-field"><div class="brief-label">'+esc(label)+'</div><div '+(id?'id="'+id+'"':'')+' class="readable">'+esc(value||'Not yet recorded')+'</div></div>'}
function knowledgeView(){
 return intro('A Chief of Staff that remembers.','Reviewed preferences, project context, lessons, and searchable task outcomes. You control what is remembered.')+
 '<section class="panel"><h2 id="memory-form-title">Add knowledge</h2><form id="memory-form"><div class="form-grid">'+input('memory-title','Title','')+'<div class="field"><label for="memory-kind">Type</label><select id="memory-kind">'+['preference','profile','project','lesson'].map(k=>'<option>'+k+'</option>').join('')+'</select></div>'+input('memory-scope','Scope (global, project, or organization)','global')+'<div class="field"><label for="memory-source">Conversation binding</label><select id="memory-source"><option value="">General guidance</option><option value="email">Email conversation</option><option value="teams">Teams conversation</option></select></div>'+input('memory-thread','Exact conversation ID (when bound)','')+'</div><div class="field"><label for="memory-content">What should the Chief of Staff remember?</label><textarea id="memory-content" required maxlength="4000"></textarea></div>'+btn('Save knowledge','type="submit"')+' '+btn('Clear form','id="memory-clear" type="button"','secondary')+'</form></section>'+
 '<div class="toolbar"><input id="memory-search" placeholder="Search knowledge…" aria-label="Search knowledge" value="'+esc(memoryQuery)+'"><select id="memory-filter"><option value="">All knowledge</option><option value="active" '+(memoryFilter==='active'?'selected':'')+'>Active</option><option value="suggested" '+(memoryFilter==='suggested'?'selected':'')+'>Suggestions to review</option></select></div><div id="memory-list">'+memoryCards()+'</div><p class="small">Task outcomes are reported observations, not verified facts. Agent suggestions are excluded from context until you accept them. Forget removes an entry from future retrieval; original job history and backups remain.</p>';
}
function memoryCards(){
 const list=memories.filter(r=>(!memoryFilter||r.status===memoryFilter)&&(r.title+' '+r.content+' '+r.scope).toLowerCase().includes(memoryQuery.toLowerCase()));
 return list.map(r=>'<article class="panel"><div class="section-title"><h2>'+esc(r.title)+'</h2><span class="tag">'+esc(r.kind)+' · '+esc(r.status)+'</span></div><div class="readable">'+esc(r.content)+'</div><p class="small">'+esc(r.scope)+(r.target?.thread_id?' · '+esc(r.target.source)+' conversation '+esc(r.target.thread_id):'')+' · '+esc(r.source)+' · '+date(r.updated)+' · Used '+r.uses+' times · Feedback '+r.helpful+'</p>'+(r.source_job?btn('Source job','data-job="'+r.source_job+'"','link-btn'):'')+'<div class="row">'+(r.status==='suggested'?btn('Accept','data-memory-action="accept" data-memory="'+r.id+'"'):'')+btn('Correct','data-memory-edit="'+r.id+'"','secondary')+btn('Helpful','data-memory-action="feedback" data-helpful="yes" data-memory="'+r.id+'"','secondary')+btn('Unhelpful','data-memory-action="feedback" data-helpful="no" data-memory="'+r.id+'"','secondary')+btn('Forget','data-memory-action="forget" data-memory="'+r.id+'"','danger')+'</div></article>').join('')||'<div class="panel empty">No matching knowledge yet.</div>';
}
function bindMemoryCards(){
 bindRows();
 document.querySelectorAll('[data-memory-action]').forEach(b=>b.onclick=handle(async()=>{await api('knowledge/'+b.dataset.memory,{action:b.dataset.memoryAction,...(b.dataset.memoryAction==='feedback'?{helpful:b.dataset.helpful==='yes'}:{})});memories=await api('knowledge');render()}));
 document.querySelectorAll('[data-memory-edit]').forEach(b=>b.onclick=()=>{const r=memories.find(x=>x.id===b.dataset.memoryEdit);editingMemory=r.id;$('#memory-form-title').textContent='Correct knowledge';$('#memory-title').value=r.title;$('#memory-kind').value=r.kind==='task'?'lesson':r.kind;$('#memory-scope').value=r.scope;$('#memory-content').value=r.content;$('#memory-source').value=r.target?.source||'';$('#memory-thread').value=r.target?.thread_id||'';$('#memory-title').focus()});
}
function bindKnowledge(){
 if(!$('#memory-form'))return;
 const filter=()=>{memoryQuery=$('#memory-search').value;memoryFilter=$('#memory-filter').value;$('#memory-list').innerHTML=memoryCards();bindMemoryCards()};
 $('#memory-search').oninput=filter;$('#memory-filter').onchange=filter;bindMemoryCards();
 $('#memory-clear').onclick=()=>{editingMemory=null;$('#memory-form').reset();$('#memory-scope').value='global';$('#memory-form-title').textContent='Add knowledge'};
 $('#memory-form').onsubmit=handle(async e=>{e.preventDefault();const entry={title:$('#memory-title').value,kind:$('#memory-kind').value,scope:$('#memory-scope').value,content:$('#memory-content').value,target:$('#memory-source').value?{source:$('#memory-source').value,thread_id:$('#memory-thread').value.trim()}:{}};await api(editingMemory?'knowledge/'+editingMemory:'knowledge',editingMemory?{action:'edit',entry}:entry);editingMemory=null;memories=await api('knowledge');$('#memory-form').reset();$('#memory-scope').value='global';toast('Knowledge saved');render()});
}

function reviewView(j){
 if(j.org!=='Communications'){
  if(!j.result_review&&!(j.review_messages||[]).length)return '';
  const ready=['completed','waiting_input','blocked','failed'].includes(j.status)&&!j.approvals.some(a=>['executing','uncertain'].includes(a.status));
  return '<section class="panel"><h3>Request changes or ask a question</h3>'+((j.review_messages||[]).map(m=>'<div class="readable"><b>'+esc(m.role==='user'?'You':'Chief of Staff')+'</b>: '+esc(m.body)+'</div>').join(''))+'<form id="result-feedback-form"><fieldset '+(ready?'':'disabled')+'><label for="result-feedback">Direction for COS</label><textarea id="result-feedback" required maxlength="8000"></textarea>'+btn('Send direction to COS','type="submit"')+'</fieldset></form></section>';
 }
 const ready=['awaiting_approval','waiting_input','blocked','failed','completed'].includes(j.status)&&!j.approvals.some(a=>['executing','uncertain'].includes(a.status));
 const history=j.review_messages||[];
 return '<section class="panel review-panel"><h3>Discuss or revise with your Chief of Staff</h3><p class="small">Ask a question, revise a response, or change the proposed action and destination organization. Giving direction supersedes the pending approval; revised actions return for fresh approval.</p><div id="review-history" class="review-history" aria-label="Review conversation">'+(history.length?history.map(m=>'<article class="review-message '+esc(m.role)+'"><div class="brief-label">'+(m.role==='user'?'You':m.role==='assistant'?'Chief of Staff':'Remembered guidance')+' <span class="small">'+date(m.created)+'</span></div><div class="readable">'+esc(m.body)+'</div></article>').join(''):'<p class="small">For example: “Make this shorter and ask for the deadline,” or “No reply is needed; this is informational.”</p>')+'</div>'+
 (j.status==='cancelled'?'<p class="small">This job is cancelled.</p>':'<form id="review-form"><fieldset '+(ready?'':'disabled')+'><div class="field"><label for="review-message">Your direction or question</label><textarea id="review-message" maxlength="8000" required placeholder="Tell the Chief of Staff what to change or how to handle this email…"></textarea></div><div class="field"><label><input id="review-remember" type="checkbox"> Remember guidance for similar emails</label></div><div id="review-rule-fields" hidden><div class="field"><label for="review-target">Apply guidance to</label><select id="review-target"><option value="general">Similar communications</option><option value="conversation">Only this conversation</option></select></div><div class="field"><label for="review-applies">Which emails should this apply to?</label><input id="review-applies" maxlength="500" placeholder="For example: automated Jira owner-change notifications"></div><div class="field"><label for="review-guidance">Reusable guidance (leave blank to use your direction above)</label><textarea id="review-guidance" maxlength="3000" placeholder="For example: Close informational updates unless they explicitly assign me an action."></textarea></div><p class="small">Saved in Knowledge base under Communications. This guides drafting and triage; sending still requires approval.</p></div>'+btn('Send direction to COS','type="submit" id="send-review"')+'<p class="small">'+(ready?'This does not approve or send the email.':'Waiting for the current step or action verification before accepting more direction.')+'</p></fieldset></form>')+'</section>';
}
function bindReview(id){
 if($('#result-feedback-form'))$('#result-feedback-form').onsubmit=handle(async e=>{
  e.preventDefault();const form=$('#result-feedback-form'),fields=form.querySelector('fieldset');fields.disabled=true;
  try{await api('jobs/'+id+'/review',{message:$('#result-feedback').value});await openJob(id);await refresh();toast('Direction received; revised work will return for review');}finally{fields.disabled=false;}
 });
 if(!$('#review-form'))return;
 const sync=()=>{const checked=$('#review-remember').checked;$('#review-rule-fields').hidden=!checked;$('#review-applies').required=checked};
 $('#review-remember').onchange=sync;sync();
 $('#review-form').onsubmit=handle(async e=>{
  e.preventDefault();$('#send-review').disabled=true;
  try{
   const draft=document.querySelector('#detail .approval textarea[id^="body-"]');
   const result=await api('jobs/'+id+'/review',{message:$('#review-message').value,remember:$('#review-remember').checked,remember_target:$('#review-target').value,guidance:$('#review-guidance').value,applies_to:$('#review-applies').value,...(draft?{draft:draft.value}:{})});
   $('#review-form').reset();sync();
   toast(result.memory_id?'Direction received and guidance remembered':'Direction received; the Chief of Staff will respond');
   await openJob(id);await refresh();
  }finally{if($('#send-review'))$('#send-review').disabled=false}
 });
}
