/* Portable Agent Board: no credential persistence, no third-party scripts. */
'use strict';
const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const stamp = v => v ? new Date(v).toLocaleString('zh-CN', {hour12:false}) : '尚未连接';
const names = {ready:'待认领',active:'进行中',blocked:'遇到阻塞',handoff:'等待交接',review:'待验收',done:'已完成',cancelled:'已取消',executing:'设备执行中',queued:'已排队',running:'正在执行',unknown:'待核实',prepared:'等待设备',failed:'执行失败',online:'在线',offline:'离线',idle:'空闲',working:'工作中',passed:'通过',unverified:'未验证'};
const badge = status => `<span class="badge ${esc(status)}">${esc(names[status] || status)}</span>`;
const events = {'work.created':'创建任务','work.claim':'认领任务','work.progress':'更新进度','work.block':'报告阻塞','work.handoff':'发起交接','work.handoff-accept':'接收交接','work.result':'提交结果','work.accept':'验收通过','work.reopen':'退回任务','work.assign':'分配 Agent','work.cancel':'取消任务','execution.reserved':'启动设备能力','execution.finished':'设备返回结果','work.assess':'逐项审核依据'};
let state = {project:'',page:'tasks',work:[],agents:[],devices:[],messages:[],filter:'all',search:'',detail:null,me:null,projects:[],capabilities:[]};
let busy = false;
function randomId(){const a=new Uint8Array(16);crypto.getRandomValues(a);return 'request-'+Array.from(a,x=>x.toString(16).padStart(2,'0')).join('');}
async function api(path, body, headers={}) {
  const response = await fetch(path, {method:body === undefined ? 'GET' : 'POST',headers:{'Content-Type':'application/json',...headers},body:body === undefined ? undefined : JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) {if(response.status===401) showLogin(); throw new Error(data.error || '请求失败');}
  return data;
}
function showLogin(){ $('#login').hidden=false;$('#shell').hidden=true; }
function toast(message){$('#toast').textContent=message;$('#toast').hidden=false;setTimeout(()=>$('#toast').hidden=true,3200);}
function failure(e){$('#error').textContent=e.message;$('#error').hidden=false;}
function agentName(id){return state.agents.find(a=>a.id===id)?.name || id || '尚未分配';}
function taskOwner(w){return w.owner?.agent_id?agentName(w.owner.agent_id):w.execution?.target?.device_id?('设备 · '+w.execution.target.device_id):agentName(w.target_agent_id);}
const list = values => `<ul>${values.map(v=>`<li>${esc(v)}</li>`).join('')}</ul>`;
function empty(title, text, button=''){return `<div class="empty"><h3>${esc(title)}</h3><p>${esc(text)}</p>${button}</div>`;}
function project(){return state.projects.find(p=>p.project_id===state.project);}
function can(operation){return project()?.operations.includes(operation);}
async function boot(){
  const ticket = new URLSearchParams(location.hash.slice(1)).get('ticket');
  if(ticket){history.replaceState(null,'',location.pathname);await api('/v1/browser-session',{ticket});}
  state.me=await api('/v1/me');state.projects=(await api('/v1/projects')).projects;
  state.capabilities=(await api('/v1/capabilities')).capabilities;
  $('#project').innerHTML=state.projects.map(p=>`<option value="${esc(p.project_id)}">${esc(p.name)}</option>`).join('');
  state.project=state.projects[0]?.project_id || '';$('#identity').textContent=`${state.me.device_id} · v${state.me.version}`;
  $('#login').hidden=true;$('#shell').hidden=false;
  await refresh();
}
async function refresh(){
  if(busy || !state.me || $('#shell').hidden) return;busy=true;
  try{
    const q='?project_id='+encodeURIComponent(state.project);
    const [w,a,d,m]=await Promise.all([api('/v1/work'+q),api('/v1/agents'+q),api('/v1/devices'),api('/v1/messages'+q)]);
    const executions=await Promise.allSettled(w.work.filter(t=>t.status==='executing').map(t=>api('/v1/work/'+t.id+'/reconcile',{})));
    for(const r of executions)if(r.status==='fulfilled'){const i=w.work.findIndex(t=>t.id===r.value.id);w.work[i]=r.value;}
    state.work=w.work;state.agents=a.agents;state.devices=d.devices;state.messages=m.messages;
    $('#connection-state').textContent='已连接服务端';$('#connection-state').classList.remove('offline');$('#error').hidden=true;
    $('#task-count').textContent=state.work.filter(w=>!['done','cancelled'].includes(w.status)).length || '';
    $('#message-count').textContent=state.messages.filter(m=>!m.acknowledged_at).length || '';
    if(!$('#dialog').open && !document.activeElement?.matches('input,textarea,select')) {
      if(state.detail) await detail(state.detail); else if(state.page!=='knowledge') render();
    }
  }catch(e){$('#connection-state').textContent='连接中断 · 正在重试';$('#connection-state').classList.add('offline');failure(e);}finally{busy=false;}
}
const subtitles={tasks:'把目标交给合适的 Agent，持续跟进到结果验收。',devices:'主电脑保存协作记录，每台设备运行自己的客户端。',agents:'接入已有的 Codex、Claude Code，保持各自的会话与工具。',messages:'联系同项目的 Agent，跟进消息送达和处理确认。',knowledge:'检索已授权的资料，查看原文来源与当前版本。'};
function render(){
  document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('active',b.dataset.page===state.page));
  $('#heading').textContent={tasks:'任务',devices:'设备',agents:'Agent',messages:'消息',knowledge:'知识库'}[state.page];
  $('#subtitle').textContent=subtitles[state.page];$('#eyebrow').textContent=project()?.name||'协作空间';
  $('#header-actions').innerHTML=state.page==='tasks'&&can('collaborate')?'<button data-action="create">＋ 创建任务</button>':state.page==='messages'&&can('collaborate')?'<button data-action="message">写消息</button>':state.page==='agents'?'<button data-action="connect">接入 Agent</button>':'';
  ({tasks:renderTasks,devices:renderDevices,agents:renderAgents,messages:renderMessages,knowledge:renderKnowledge})[state.page]();
}
function renderTasks(){
  const active=state.work.filter(w=>['active','executing'].includes(w.status)).length;
  const attention=state.work.filter(w=>['blocked','handoff','review'].includes(w.status)).length;
  $('#content').innerHTML=`<div class="stats"><div class="stat"><strong>${active}</strong><span>进行中的任务</span></div><div class="stat"><strong>${attention}</strong><span>需要跟进</span></div><div class="stat"><strong>${state.work.filter(w=>w.status==='done').length}</strong><span>已完成</span></div><div class="stat"><strong>${state.agents.filter(a=>a.online).length}</strong><span>在线 Agent</span></div></div><div class="filters">${[['all','全部'],['ready','待认领'],['active','进行中'],['attention','需跟进'],['done','已完成']].map(([v,l])=>`<button data-filter="${v}" class="${state.filter===v?'selected':''}">${l}</button>`).join('')}<input id="search-task" class="search" placeholder="搜索任务" aria-label="搜索任务" value="${esc(state.search)}"></div><div id="task-list"></div>`;
  taskRows();
  $('#search-task').addEventListener('input',e=>{state.search=e.target.value;taskRows();});
}
function taskRows(){
  const work=state.work.filter(w=>(state.filter==='all'||state.filter===w.status||(state.filter==='attention'&&['review','blocked','handoff'].includes(w.status))||(state.filter==='active'&&w.status==='executing'))&&(w.title+' '+w.goal).toLowerCase().includes(state.search.toLowerCase()));
  $('#task-list').innerHTML=work.length?`<div class="list">${work.map(w=>`<div class="row clickable" role="button" tabindex="0" data-work="${w.id}"><div class="row-body"><h3>${esc(w.title)}</h3><small>${esc((w.status==='done'?w.result?.summary:null)||w.blocker||w.progress||w.goal.slice(0,110))}</small><small>${esc(taskOwner(w))} · ${stamp(w.updated_at)}</small></div>${badge(w.status)}<span class="muted">→</span></div>`).join('')}</div>`:empty('从一个明确的目标开始','填写任务目标、范围和验收标准，交给已接入的 Agent，或调用设备能力。');
}
function renderDevices(){
  $('#content').innerHTML=`<div class="connection-help"><h3>主电脑：服务端 + 客户端　／　副电脑：客户端</h3><p>设备在线以心跳为准。副电脑接入后，共用这里的任务、消息与授权知识；执行仍发生在目标设备上。</p></div><div class="list">${state.devices.map(d=>`<div class="row"><div class="device-icon">▱</div><div class="row-body"><h3>${esc(d.name)}</h3><small>${esc(d.os||'尚无系统信息')} · ${esc(d.environment_id||d.id)}</small><small>最近连接：${stamp(d.last_seen)} · 客户端 ${esc(d.client_version||'待接入')}</small><small>本机工具：${esc(d.tools.join('、')||'未检测到 Agent 工具')}</small></div>${badge(d.online?'online':'offline')}</div>`).join('')}</div><div class="section"><h3>设备能力</h3><p class="muted">环境检查、目录检查由 Dagu 执行。Codex / Claude Code 只读分析需要目标设备在统一配置中明确开启。设备离线时不会重复派发已经启动的执行。</p><button class="secondary" data-action="device-help">查看副电脑接入步骤</button></div>`;
}
function renderAgents(){
  $('#content').innerHTML=`<div class="connection-help"><h3>使用你已经安装的 Agent</h3><p>通过客户端启动 Codex / Claude Code，或把 MCP 配置接入现有工具。会话连接后会自动出现在这里，并可认领任务、联系其他 Agent、查询知识和交接。</p></div>`+(state.agents.length?`<div class="list">${state.agents.map(a=>`<div class="row"><div class="device-icon">◉</div><div class="row-body"><h3>${esc(a.name)} <span class="badge">${esc(a.provider)}</span></h3><small>${esc(a.device_id)} · ${esc(a.workspace_id)} · ${esc(names[a.state])}</small><small>会话：${esc(a.session_id)}</small><small>最近心跳：${stamp(a.last_seen)}</small></div>${badge(a.online?'online':'offline')}<button class="secondary" data-message-agent="${a.id}">联系</button></div>`).join('')}</div>`:empty('还没有接入的 Agent','已安装工具会显示在设备页。只有实际建立连接的会话才会出现在这里。','<button data-action="connect">接入第一个 Agent</button>'));
}
function messageRows(messages){return messages.map(m=>`<div class="message"><div class="message-head"><span>${esc(m.from_agent_id?agentName(m.from_agent_id):m.from_principal)} → ${esc(agentName(m.to_agent_id))}</span><span>${stamp(m.created_at)}</span></div><div class="body-text">${esc(m.body)}</div><p class="muted">${m.acknowledged_at?'已确认 · '+stamp(m.acknowledged_at):m.delivered_at?'已送达，等待处理确认':'等待接收方读取'}${m.work_id?' · 任务消息':''}</p></div>`).join('');}
function renderMessages(){
  $('#content').innerHTML=state.messages.length?`<div class="panel">${messageRows(state.messages)}</div>`:empty('暂时没有消息','向已接入的 Agent 发送目标、问题或补充资料。接收方读取后显示送达，确认处理后显示已确认。');
}
function renderKnowledge(){
  $('#content').innerHTML='<form id="knowledge-form" class="knowledge-form"><input name="query" required placeholder="搜索项目资料和已接入的知识库" aria-label="知识查询"><button>搜索</button></form><div id="knowledge-result">'+empty('知识跟随任务流动','查询返回原文来源、摘要和校验值。可将来源与版本复制到任务消息或结果依据中。')+'</div>';
  $('#knowledge-form').onsubmit=async e=>{e.preventDefault();const b=e.submitter;b.disabled=true;try{const r=await api('/v1/knowledge/search',{project_id:state.project,query:new FormData(e.target).get('query')});$('#knowledge-result').innerHTML=`<p class="muted">${r.sources.map(s=>`${esc(s.source_id)}：${s.status==='available'?'可用':'暂不可用 · '+esc(s.error)}`).join('　')}</p>`+(r.results.length?r.results.map(r=>`<article class="panel"><h3>${esc(r.title)}</h3><div class="body-text">${esc(r.snippet)}</div><p class="source">${esc(r.uri)} · 第 ${r.line} 行</p><p class="source">${esc(r.version)}</p><p class="source">原文核验：${stamp(r.verified_at)}</p><button class="secondary" data-copy="${esc(r.uri+'\n'+r.version+'\n'+r.snippet)}">复制来源与摘要</button></article>`).join(''):empty('没有匹配的原文','尝试更短的关键词，或检查资料是否在该项目的授权范围内。'));}catch(err){failure(err);}finally{b.disabled=false;}};
}
async function detail(key){
  const w=await api('/v1/work/'+key);state.detail=key;
  const owner=state.agents.find(a=>a.id===w.owner?.agent_id);
  const mine=owner?.principal_id===state.me.principal_id&&owner?.session_id===w.owner?.session_id&&owner.online;
  const local=state.agents.filter(a=>a.principal_id===state.me.principal_id&&a.online);
  $('#heading').textContent='任务详情';$('#subtitle').textContent='目标、执行与交接记录保存在同一条任务中。';$('#header-actions').innerHTML='';
  let actions='';
  if(w.status==='ready'&&can('collaborate')) actions+='<button data-action="claim">认领任务</button><button class="secondary" data-action="assign">分配 Agent</button>'+(can('execute')?'<button class="secondary" data-action="run">调用设备能力</button>':'');
  if(mine&&['active','blocked'].includes(w.status)) actions+='<button data-action="progress">汇报进度</button><button class="secondary" data-action="handoff">交接任务</button><button class="secondary" data-action="block">报告阻塞</button><button class="secondary" data-action="result">提交结果</button>';
  if(w.status==='handoff'&&local.some(a=>a.id===w.handoff.to_agent_id)) actions+='<button data-action="receive">确认接收交接</button>';
  if(w.status==='review'&&can('accept')) actions+=(w.result?.kind==='device-execution'?'<button class="secondary" data-action="assess">逐项审核</button>':'')+'<button data-action="accept">验收通过</button><button class="secondary" data-action="reopen">退回补充</button>';
  if(w.status==='executing') actions+='<button data-action="reconcile">核对执行状态</button>'+(w.execution?.status==='prepared'?'<button class="secondary" data-action="dispatch-retry">重试连接设备</button>':'');
  $('#content').innerHTML=`<button class="text-button back" data-action="back">← 返回任务列表</button><div class="split"><div><article class="panel"><div class="detail-title"><h2>${esc(w.title)}</h2>${badge(w.status)}</div><p class="body-text">${esc(w.goal)}</p><div class="section"><h3>范围</h3>${list(w.scope)}</div>${w.constraints.length?`<div class="section"><h3>约束</h3>${list(w.constraints)}</div>`:''}<div class="section"><h3>验收标准</h3>${list(w.acceptance)}</div><dl class="meta"><dt>执行方</dt><dd>${esc(taskOwner(w))}</dd><dt>执行编号</dt><dd>${esc(w.attempt_id||'尚未开始')}</dd><dt>任务编号</dt><dd>${esc(w.id)}</dd></dl><div class="actions">${actions}</div></article>${w.progress||w.blocker?`<section class="panel"><h3>${w.blocker?'当前阻塞':'最新进度'}</h3><p class="body-text">${esc(w.blocker||w.progress)}</p></section>`:''}${w.handoff?`<section class="panel"><h3>交接 · ${esc(agentName(w.handoff.from_agent_id))} → ${esc(agentName(w.handoff.to_agent_id))}</h3><p class="muted">${w.handoff.accepted_at?'已接收 · '+stamp(w.handoff.accepted_at):'原执行已声明停止修改，等待接收方确认。'}</p><h3>已完成</h3><p class="body-text">${esc(w.handoff.completed)}</p><h3>剩余工作</h3><p class="body-text">${esc(w.handoff.remaining)}</p><p class="body-text">${esc(w.handoff.context)}</p></section>`:''}${w.execution?`<section class="panel"><h3>设备执行 ${badge(w.execution.status)}</h3><p>${esc(w.execution.last_error||'')}</p>${w.execution.evidence?`<details><summary>查看完整执行回执</summary><pre>${esc(JSON.stringify(w.execution.evidence.receipt||w.execution.evidence,null,2))}</pre></details>`:''}</section>`:''}${w.result?`<section class="panel"><h3>提交结果</h3><p class="body-text">${esc(w.result.summary)}</p>${w.result.checks.map(c=>`<div class="check">${badge(c.status)} ${esc(c.criterion)}<p class="body-text">${esc(c.evidence)}</p></div>`).join('')}${w.review?`<p class="muted">验收：${esc(w.review.actor)} · ${stamp(w.review.time)}</p><p>${esc(w.review.note)}</p>`:''}${w.artifacts.map(a=>`<p><button class="text-button" data-artifact="${a.id}">${esc(a.name)} · ${a.size} 字节</button><small class="source"> SHA256 ${esc(a.sha256)}</small></p>`).join('')}</section>`:''}</div><div><section class="panel"><h3>任务时间线</h3><div class="timeline">${w.events.slice().reverse().map(e=>`<div class="timeline-item">${esc(events[e.type]||e.type)}<small>${stamp(e.time)} · ${esc(e.actor)}</small>${e.details.note||e.details.summary?`<p>${esc(e.details.note||e.details.summary)}</p>`:e.details.to_agent_id?`<p>接收方：${esc(agentName(e.details.to_agent_id))}</p>`:''}</div>`).join('')}</div></section><section class="panel"><h3>任务消息</h3>${messageRows(state.messages.filter(m=>m.work_id===key))||'<p class="muted">还没有相关消息</p>'}<button class="secondary" data-action="message">写消息</button></section></div></div>`;
  state.current=w;
}
function modal(title, html, submit){
  $('#dialog-title').textContent=title;$('#dialog-body').innerHTML=`<form id="action-form">${html}<div class="form-error" hidden></div><div class="actions"><button type="submit">确认</button><button class="secondary" type="button" data-action="close">取消</button></div></form>`;$('#dialog').showModal();
  $('#action-form').onsubmit=async e=>{e.preventDefault();const button=e.submitter;button.disabled=true;try{await submit(Object.fromEntries(new FormData(e.target)));$('#dialog').close();toast('已保存');await refresh();}catch(err){const box=$('.form-error');box.textContent=err.message;box.hidden=false;}finally{button.disabled=false;}};
}
function field(name,label,type='input',value='',required=true){return `<label>${label}${type==='textarea'?`<textarea name="${name}" ${required?'required':''}>${esc(value)}</textarea>`:`<input name="${name}" value="${esc(value)}" ${required?'required':''}>`}</label>`;}
function select(name,label,values,optional=false){return `<label>${label}<select name="${name}" ${optional?'':'required'}>${optional?'<option value="">暂不分配</option>':''}${values.map(([v,l])=>`<option value="${esc(v)}">${esc(l)}</option>`).join('')}</select></label>`;}
const lines=v=>String(v||'').split('\n').map(v=>v.trim()).filter(Boolean);
function openMessage(target){
  if(!state.agents.length) return toast('请先接入一位 Agent');
  let choices=state.agents.map(a=>[a.id,a.name+' · '+a.device_id]);if(target) choices.sort((a,b)=>(b[0]===target)-(a[0]===target));
  const request_id=randomId();
  modal('发送消息',select('to_agent_id','接收方',choices)+field('body','正文','textarea')+`<p class="muted">${state.detail?'消息将关联当前任务，并对项目成员可见。':'消息只对发送方及接收设备可见。'}</p>`,data=>api('/v1/messages',{...data,project_id:state.project,request_id,needs_ack:true,...(state.detail?{work_id:state.detail}:{})}));
}
function connectHelp(){
  const local=project()?.workspaces.filter(w=>w.device_id===state.me.device_id)||[];
  const ws=local[0]?.workspace_id||'本机工作区编号';
  $('#dialog-title').textContent='接入已有 Agent';
  $('#dialog-body').innerHTML=`<p>在对应设备的 Agent Board 目录打开终端，运行：</p><h3>Codex</h3><pre>python${navigator.platform.includes('Win')?'':'3'} agent_board.py network --config .runtime/config.json agent --project ${esc(state.project)} --workspace ${esc(ws)} --name codex-local --provider codex</pre><h3>Claude Code</h3><pre>python${navigator.platform.includes('Win')?'':'3'} agent_board.py network --config .runtime/config.json agent --project ${esc(state.project)} --workspace ${esc(ws)} --name claude-local --provider claude</pre><p>副电脑请把配置路径改成配对的 <code>.runtime/client.json</code>。工具继续使用本机登录与权限设置。</p><p class="muted">已有支持 MCP 的应用可把命令中的 agent 换成 connection，复制输出的配置。应用重载 MCP 后才会建立会话。收件箱由 Agent 调用工具读取；不会向未接入的聊天窗口注入消息。</p>`;$('#dialog').showModal();
}
async function action(name){
  const w=state.current;const local=state.agents.filter(a=>a.principal_id===state.me.principal_id&&a.online);
  const trans=(verb,data)=>api('/v1/work/'+w.id+'/'+verb,{revision:w.revision,...data});
  const own=()=>{const a=state.agents.find(a=>a.id===w.owner?.agent_id);return {agent_id:a.id,session_id:a.session_id,attempt_id:w.attempt_id};};
  if(name==='close') return $('#dialog').close();
  if(name==='back'){state.detail=null;return render();}
  if(name==='connect') return connectHelp();
  if(name==='device-help') {$('#dialog-title').textContent='副电脑接入';$('#dialog-body').innerHTML='<p>1. 主电脑用 <code>network pair</code> 生成独立设备配置与令牌。</p><p>2. Windows 拉取主分支，把配对文件放入 <code>.runtime/</code>。</p><p>3. 运行 <code>scripts/client.ps1 -Config .runtime/client.json</code>。</p><p>4. 运行 <code>scripts/open.ps1 -Config .runtime/client.json</code> 打开同一个协作空间。</p><p class="muted">协作功能只需连接主电脑服务地址；需要设备执行时，再安装 Dagu 并配置执行连接证书。完整命令见 docs/跨设备使用指南.md。</p>';return $('#dialog').showModal();}
  if(name==='message')return openMessage(w?.target_agent_id);
  if(name==='create'){
    const request_id=randomId();
    return modal('创建协作任务',field('title','任务名称')+field('goal','要达成什么目标','textarea')+field('scope','工作区内范围 · 每行一项','textarea','.')+field('constraints','必须遵守的约束 · 每行一项','textarea','',false)+field('acceptance','怎样算完成 · 每行一项','textarea')+select('target_agent_id','分配给',state.agents.map(a=>[a.id,a.name+' · '+a.device_id]),true),d=>api('/v1/work',{...d,scope:lines(d.scope),constraints:lines(d.constraints),acceptance:lines(d.acceptance),project_id:state.project,request_id}));
  }
  if(name==='reconcile'){await api('/v1/work/'+w.id+'/reconcile',{});return detail(w.id);}
  if(name==='dispatch-retry'){await api('/v1/work/'+w.id+'/run',w.run_request.body);return detail(w.id);}
  if(name==='run')return modal('调用目标设备能力',select('workspace_id','目标工作区',project().workspaces.map(ws=>[ws.workspace_id,ws.device_id+' · '+ws.workspace_id]))+select('capability','执行能力',state.capabilities.map(c=>[c.id,c.name]))+'<p class="muted">本次按任务目标和范围执行。Agent 分析使用目标设备现有登录，需要先在该设备授权只读能力。执行结束后仍需逐项验收。</p>',d=>trans('run',d));
  if(name==='assign')return modal('分配 Agent',select('target_agent_id','目标 Agent',state.agents.map(a=>[a.id,a.name+' · '+a.device_id])),d=>trans('assign',d));
  if(name==='claim'||name==='receive'){
    const choices=local.filter(a=>name==='receive'?a.id===w.handoff.to_agent_id:!w.target_agent_id||a.id===w.target_agent_id);
    if(!choices.length)return toast('本设备没有符合分配条件的在线 Agent');
    return modal(name==='claim'?'认领任务':'接收交接',select('agent_id','由本机在线会话执行',choices.map(a=>[a.id,a.name]))+'<p class="muted">此操作为所选会话认领任务。请在该 Agent 中调用 board_task 读取目标后执行。</p>',d=>trans(name==='claim'?'claim':'handoff-accept',{...d,session_id:choices.find(a=>a.id===d.agent_id).session_id}));
  }
  if(['progress','block','accept','reopen'].includes(name))return modal({progress:'汇报进度',block:'报告阻塞',accept:'验收任务',reopen:'退回补充'}[name],field('note',{progress:'已完成什么、下一步是什么',block:'阻塞原因和所需帮助',accept:'验收意见',reopen:'需要补充的内容'}[name],'textarea'),d=>trans(name,{...d,...(['progress','block'].includes(name)?own():{})}));
  if(name==='handoff')return modal('交接任务',select('to_agent_id','接收方',state.agents.filter(a=>a.id!==w.owner.agent_id).map(a=>[a.id,a.name+' · '+a.device_id]))+field('completed','已经完成','textarea')+field('remaining','剩余工作','textarea')+field('context','资料、依据与注意事项','textarea','',false)+'<label><span><input type="checkbox" name="released" required class="checkbox"> 我确认当前执行已停止修改，可以交接</span></label>',d=>trans('handoff',{...d,...own(),released:d.released==='on'}));
  if(name==='result'||name==='assess')return modal(name==='result'?'提交结果':'逐项审核设备输出',field('summary','结果摘要','textarea',w.result?.summary||'')+w.acceptance.map((c,i)=>`<label>${i+1}. ${esc(c)}<div class="evidence-line"><select name="status${i}"><option value="unverified">未验证</option><option value="passed">通过</option><option value="failed">失败</option></select><input name="evidence${i}" placeholder="实际验证依据" required></div></label>`).join(''),d=>trans(name==='result'?'result':'assess',{...(name==='result'?own():{}),summary:d.summary,checks:w.acceptance.map((c,i)=>({status:d['status'+i],evidence:d['evidence'+i]})),artifact_ids:[]}));
}
document.addEventListener('click',async e=>{
  const b=e.target.closest('button,[data-work]');if(!b)return;
  try{
    if(b.dataset.page){state.page=b.dataset.page;state.detail=null;render();}
    else if(b.dataset.filter){state.filter=b.dataset.filter;renderTasks();}
    else if(b.dataset.work)await detail(b.dataset.work);
    else if(b.dataset.action)await action(b.dataset.action);
    else if(b.dataset.messageAgent)openMessage(b.dataset.messageAgent);
    else if(b.dataset.copy){if(navigator.clipboard){await navigator.clipboard.writeText(b.dataset.copy);}else{const area=document.createElement('textarea');area.value=b.dataset.copy;document.body.append(area);area.select();const ok=document.execCommand('copy');area.remove();if(!ok)throw Error('浏览器未允许复制，请手动选择来源文本');}toast('已复制来源与摘要');}
    else if(b.dataset.artifact){const a=await api('/v1/artifacts/'+b.dataset.artifact);const bytes=Uint8Array.from(atob(a.content),c=>c.charCodeAt(0));if(crypto.subtle){const hash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))).map(x=>x.toString(16).padStart(2,'0')).join('');if(hash!==a.sha256)throw Error('产物校验失败');}const url=URL.createObjectURL(new Blob([bytes]));const link=document.createElement('a');link.href=url;link.download=a.name;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  }catch(err){failure(err);}
});
document.addEventListener('keydown',e=>{if(e.key==='Enter'&&e.target.dataset.work)e.target.click();});
$('#dialog-close').onclick=()=>$('#dialog').close();
$('#project').onchange=async e=>{state.project=e.target.value;state.detail=null;state.filter='all';state.search='';await refresh();render();};
$('#logout').onclick=async()=>{await api('/v1/logout',{});state.me=null;showLogin();};
$('#login-form').onsubmit=async e=>{e.preventDefault();const token=$('#token').value;$('#token').value='';try{await api('/v1/browser-session',{}, {Authorization:'Bearer '+token});$('#login-error').textContent='';await boot();}catch(err){$('#login-error').textContent=err.message;}};
boot().catch(e=>{$('#login-error').textContent=e.message;showLogin();});
setInterval(refresh,10000);

window.addEventListener('hashchange',()=>{if(new URLSearchParams(location.hash.slice(1)).has('ticket'))boot().catch(failure);});
