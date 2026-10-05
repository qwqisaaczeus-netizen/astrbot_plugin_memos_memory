/* Only dirty values are submitted; model credentials remain in their owning store. */
const center={items:[],dirty:{},mind:{},mindDirty:{},house:{},houseDirty:{},poolDirty:false,routeOptions:[]};
const originalRequest=request,originalRender=render,originalModelHtml=modelHtml;
request=async function(path,options){
 if(path==='/api/models/save'){
  const body=JSON.parse(options.body);body.astr_followup_models=state.astr_followup_models||{};body.task_call_policies=state.task_call_policies||{};
  options={...options,body:JSON.stringify(body)};
 }
 const data=await originalRequest(path,options);
 if(path==='/api/models'||path==='/api/models/save'){
  if(data.task_route_options)center.routeOptions=data.task_route_options;
  if(data.probe_provider_options)center.probeProviders=data.probe_provider_options;
  data.task_route_options=center.routeOptions;
  if(path==='/api/models/save')center.poolDirty=false;
 }
 return data;
};
modelHtml=function(x,i){
 const html=originalModelHtml(x,i),node=document.createElement('div');node.innerHTML=html;
 const article=node.firstElementChild;article.querySelector('.model-head')?.remove();
 const capacity=document.createElement('label');capacity.className='field';capacity.textContent='模型上下文窗口（tokens，0 未指定）';
 const input=document.createElement('input');input.type='number';input.min='0';input.max='2097152';input.value=Number(x.context_window_tokens||0);
 input.setAttribute('oninput',"setv('"+x.id+"','context_window_tokens',Number(this.value))");capacity.append(input);
 const hint=document.createElement('small');hint.textContent='模型输入和输出的总容量，不是聊天轮数。按服务商规格填写，不能扩大模型实际窗口。';capacity.append(hint);article.querySelector('.grid').append(capacity);
 return '<details class="model" data-model="'+esc(x.id)+'"'+(!x.has_api_key?' open':'')+'><summary><span>'+esc(x.name||x.model||'新模型')+'</span><span class="health">'+(x.enabled?'启用':'停用')+' · '+esc(x.model||'未配置')+'</span></summary><div class="model-body"><label class="switch"><input type="checkbox" '+(x.enabled?'checked':'')+' onchange="toggleModel(\''+x.id+'\',this.checked)">启用</label>'+article.innerHTML+'</div></details>';
};
function capturePool(){
 state.enabled=document.getElementById('pool-enabled').checked;
 state.fallback_enabled=document.getElementById('fallback-enabled').checked;
 state.default_model_id=document.getElementById('default-model').value;
 state.astr_followup_enabled=document.getElementById('followup-enabled').checked;
 state.astr_followup_model_id=document.getElementById('followup-model').value;
 state.astr_followup_timeout=Number(document.getElementById('followup-timeout').value||90);
 state.astr_followup_tasks=[...document.querySelectorAll('#followup-tasks input:checked')].map(e=>e.value);
}
render=function(){
 const open=new Set([...document.querySelectorAll('[data-model][open]')].map(e=>e.dataset.model));
 originalRender();
 const select=document.getElementById('followup-model');select.insertAdjacentHTML('afterbegin','<option value="">仅使用任务专属路线</option>');select.value=state.astr_followup_model_id||'';
 document.querySelectorAll('[data-model]').forEach(e=>{if(open.has(e.dataset.model))e.open=true;});
 const models=(state.models||[]).filter(x=>x.enabled);
 document.getElementById('route-settings').innerHTML=center.routeOptions.map(t=>{
  const map=state.astr_followup_models||{},value=Object.hasOwn(map,t.value)?map[t.value]:'__inherit__';
  const options=[['__inherit__','继承类别默认'],['','禁用此任务跟接'],...models.map(m=>[m.id,m.name||m.model||m.id])];
  if(value&&!options.some(o=>o[0]===value))options.push([value,value+' · 不可用']);
  return '<div class="wb-form-row"><div><label for="route-'+esc(t.value)+'">'+esc(t.label)+'</label><small>'+esc(t.value)+'</small></div><select id="route-'+esc(t.value)+'" data-route="'+esc(t.value)+'">'+options.map(([v,l])=>'<option value="'+esc(v)+'"'+(v===value?' selected':'')+'>'+esc(l)+'</option>').join('')+'</select></div>';
 }).join('');
 document.querySelectorAll('[data-route]').forEach(e=>e.onchange=()=>{state.astr_followup_models??={};if(e.value==='__inherit__')delete state.astr_followup_models[e.dataset.route];else state.astr_followup_models[e.dataset.route]=e.value;center.poolDirty=true;});
};
const oldAdd=addModel,oldRemove=removeModel;addModel=()=>{capturePool();center.poolDirty=true;oldAdd();};removeModel=id=>{capturePool();oldRemove(id);center.poolDirty=true;};
testModel=id=>openModelProbe('external:'+id);
for(const id of ['pane-pool','pane-fallback'])document.getElementById(id).addEventListener('change',()=>{capturePool();center.poolDirty=true;});
for(const id of ['pane-pool','pane-fallback'])document.getElementById(id).addEventListener('input',()=>{center.poolDirty=true;});
function groupName(k){if(k==='generation_v2_job_cap')return '6.1 生产预算';if(k.startsWith('llm_runtime'))return '兼容任务并发与队列';if(/^(compress|episode_extraction|diary_|narrative_)/.test(k))return '记忆生产';if(/^(semantic_state|profile)/.test(k))return '人格与状态';if(k.startsWith('time_insight'))return '时间洞察';if(/^(thread|consistency)/.test(k))return '脉络与审校';return '检索与其他任务';}
function controlHtml(owner,item){
 const id=owner+'-'+item.key,value=item.value??'',options=item.options||[];
 let input='';
 if(item.type==='bool')input='<input id="'+id+'" type="checkbox" '+(value?'checked':'')+'>';
 else if(options.length){const list=[...options];if(!list.some(o=>String(o.value)===String(value)))list.push({value,label:String(value)+' · 当前值'});input='<select id="'+id+'">'+list.map(o=>'<option value="'+esc(o.value)+'"'+(String(o.value)===String(value)?' selected':'')+'>'+esc(o.label)+'</option>').join('')+'</select>';}
 else input='<input id="'+id+'" type="'+(item.sensitive?'password':(['int','float'].includes(item.type)?'number':'text'))+'" value="'+esc(value)+'" '+(item.sensitive?'autocomplete="new-password" placeholder="留空保留已存密钥"':'')+(item.min!=null?' min="'+Number(item.min)+'"':'')+(item.max!=null?' max="'+Number(item.max)+'"':'')+(item.type==='float'?' step="any"':'')+'>';
 return '<div class="wb-form-row" data-search="'+esc((item.description+' '+item.key).toLowerCase())+'"><div><label for="'+id+'">'+esc(item.description||item.key)+'</label><small>'+esc(item.hint||item.key)+'</small></div>'+input+'</div>';
}
function bindFields(owner,items,dirty){items.forEach(item=>{const e=document.getElementById(owner+'-'+item.key);if(!e)return;e.addEventListener('input',()=>{let v=item.type==='bool'?e.checked:(['int','float'].includes(item.type)?Number(e.value):e.value);if(item.sensitive&&!String(v).trim())delete dirty[item.key];else if(String(v)===String(item.value))delete dirty[item.key];else dirty[item.key]=v;});});}
async function loadTaskSettings(){
 try{const d=await request('/api/scheduling/settings');center.items=d.items||[];center.dirty={};const groups={};center.items.forEach(i=>{if(['compress_llm_timeout','episode_extraction_timeout','diary_render_timeout'].includes(i.key))i.hint='兼容生产链路参数；6.1 流式首次有效输出默认等待 180 秒；开始输出后使用静默闸门，在途作业总期限仍有效。 '+(i.hint||'');(groups[groupName(i.key)]??=[]).push(i);});
 document.getElementById('task-settings').innerHTML=Object.entries(groups).map(([name,items],i)=>'<details class="wb-fold"'+(!i?' open':'')+'><summary>'+esc(name)+' · '+items.length+' 项</summary>'+items.map(x=>controlHtml('task',x)).join('')+'</details>').join('');bindFields('task',center.items,center.dirty);
 document.getElementById('task-notice').textContent='仅保存修改项；需要重载的字段在保存后列出。6.1 在途任务保持原路线。';
 document.getElementById('task-search').dispatchEvent(new Event('input',{bubbles:true}));
 }catch(e){document.getElementById('task-settings').textContent='读取失败：'+e.message;}
}
async function saveTaskSettings(){
 if(!validFields('task-settings'))return;
 try{const d=await request('/api/settings/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({values:center.dirty})});await loadTaskSettings();toast(d.message||'任务设置已保存');if(d.restart_required?.length)document.getElementById('task-notice').textContent='已保存，重载插件后生效：'+d.restart_required.join('、');}
 catch(e){toast(e.message,true);}
}
function validFields(id){for(const e of document.getElementById(id).querySelectorAll('input,select'))if(!e.reportValidity())return false;return true;}
const mindFields=[['live_perception_provider_id','即时感知模型','provider'],['perception_provider_id','后台感知模型','provider'],['dream_provider_id','梦境模型','provider'],['proactive_provider_id','主动内容模型','provider'],['live_perception_timeout_seconds','即时感知超时（秒）','int'],['post_perception_timeout_seconds','后台感知超时（秒）','int'],['dream_timeout_seconds','梦境超时（秒）','int'],['proactive_timeout_seconds','主动内容超时（秒）','int']];
const mindApiFields=[['perception_api_mode','独立 API 范围','mode'],['perception_api_base_url','API Base URL','string'],['perception_api_model','模型名称','string'],['perception_api_key','API Key','secret'],['clear_perception_api_key','清除已存密钥','bool'],['perception_api_max_retries','内部重试上限','int']];
const houseFields=[['house_api_base_url','API Base URL','string'],['house_model','模型名称','string'],['house_api_key','API Key','secret'],['clear_api_key','清除已存密钥','bool'],['house_timeout_seconds','超时（秒）','int'],['house_max_output_tokens','输出 token 上限','int'],['house_max_retries','重试上限','int'],['house_temperature','温度','float']];
function fields(defs,values,providers=[]){return defs.map(([key,description,type])=>({key,description,type:type==='secret'?'string':type,value:type==='secret'?'':(values[key]??(type==='bool'?false:'')),sensitive:type==='secret',options:type==='provider'?[{value:'',label:'继承默认模型'},...providers.map(p=>({value:p.value??p.id,label:p.label??p.id}))]:type==='mode'?[{value:'off',label:'关闭'}, {value:'post',label:'仅后台感知'},{value:'all',label:'即时与后台感知'}]:[]}));}
async function loadIndependent(){
 await Promise.all([loadMind(),loadHouse()]);
}
async function loadMind(){try{
 const d=await request('/api/xinchao/settings');center.mind=d.settings||{};center.mindDirty={};
 const a=fields(mindFields,center.mind,d.providerOptions||[]),b=fields(mindApiFields,center.mind);
 document.getElementById('mind-tasks').innerHTML='<details class="wb-fold"><summary>心潮任务 · '+a.length+' 项</summary>'+a.map(i=>controlHtml('mind',i)).join('')+'<div class="scope-save"><button class="btn primary" onclick="saveIndependent(\'mind\')">保存心潮模型设置</button></div></details>';
 document.getElementById('mind-api').innerHTML='<details class="wb-fold" open><summary>心潮独立 API · '+(center.mind.has_perception_api_key?'密钥已配置':'未配置密钥')+'</summary>'+b.map(i=>controlHtml('mind',i)).join('')+'<div class="scope-save"><button class="btn primary" onclick="saveIndependent(\'mind\')">保存心潮通道</button></div></details>';
 bindFields('mind',[...a,...b],center.mindDirty);
 }catch(e){for(const id of ['mind-tasks','mind-api'])document.getElementById(id).textContent='心潮配置不可用：'+e.message;}}
async function loadHouse(){try{const d=await request('/api/house/settings');center.house=d;center.houseDirty={};const items=fields(houseFields,d);
 document.getElementById('house-api').innerHTML='<details class="wb-fold"><summary>小院独立 API · '+(d.has_api_key?'密钥已配置':'未配置密钥')+'</summary>'+items.map(i=>controlHtml('house',i)).join('')+'<div class="scope-save"><button class="btn primary" onclick="saveIndependent(\'house\')">保存小院通道</button></div></details>';bindFields('house',items,center.houseDirty);
 }catch(e){document.getElementById('house-api').textContent='小院配置不可用：'+e.message;}}
async function saveIndependent(owner){
 const mind=owner==='mind';if(!validFields(mind?'mind-api':'house-api')||(mind&&!validFields('mind-tasks')))return;
 try{await request(mind?'/api/xinchao/settings/save':'/api/house/settings/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({settings:mind?center.mindDirty:center.houseDirty})});toast('设置已保存，其他配置保持不变');await (mind?loadMind():loadHouse());}catch(e){toast(e.message,true);}
}
function attemptDetails(a){const d=a.diagnostics||{},parts=[];
 if(d.stream!=null)parts.push(d.stream?'流式':'非流式');
 if(d.finish_reason)parts.push('结束：'+d.finish_reason);
 if(d.max_tokens!=null)parts.push('输出上限 '+d.max_tokens);
 if(d.reasoning_chars!=null)parts.push('推理 '+d.reasoning_chars+' 字');
 if(d.content_chars!=null)parts.push('正文 '+d.content_chars+' 字');
 if(d.first_event_ms!=null)parts.push('首事件 '+(d.first_event_ms/1000).toFixed(1)+' 秒');
 if(d.first_content_ms!=null)parts.push('首正文 '+(d.first_content_ms/1000).toFixed(1)+' 秒');
 if(d.timeout_phase)parts.push('计时阶段：'+({first_progress:'等待首次有效输出',progress_idle:'有效输出静默',total:'非流式总耗时'}[d.timeout_phase]||d.timeout_phase));
 if(d.idle_timeout_seconds!=null)parts.push('静默闸门 '+d.idle_timeout_seconds+' 秒');
 return parts.length?'<p class="muted">'+esc(parts.join(' · '))+'</p>':'';
}
async function loadV2Calls(){try{const d=await request('/api/production/v2');document.getElementById('v2-calls').innerHTML=(d.jobs||[]).map(j=>'<details class="wb-fold"><summary>'+esc(j.batch_id)+' · '+esc(j.status)+'</summary><p class="muted">调用 '+Number(j.budget?.used||0)+' / '+Number(j.budget?.cap||0)+'</p>'+(j.attempts||[]).map(a=>'<p class="muted">'+esc(a.task)+' · '+esc(a.error_kind||a.outcome)+' · '+(Number(a.elapsed_ms)/1000).toFixed(1)+' 秒</p>'+attemptDetails(a)).join('')+'</details>').join('')||'<div class="empty">暂无 6.1 调用记录</div>';}catch(e){document.getElementById('v2-calls').textContent=e.message;}}
async function reloadCenter(){if(hasDirty()&&!confirm('放弃未保存的设置并重新读取？'))return;center.poolDirty=false;await Promise.all([load(),loadTaskSettings(),loadIndependent(),loadV2Calls()]);}
function hasDirty(){return center.poolDirty||[center.dirty,center.mindDirty,center.houseDirty].some(x=>Object.keys(x).length);}
window.addEventListener('beforeunload',e=>{if(hasDirty()){e.preventDefault();e.returnValue='';}});
document.querySelectorAll('[data-pane]').forEach(b=>b.onclick=()=>{document.querySelectorAll('[data-pane]').forEach(x=>x.setAttribute('aria-selected',String(x===b)));document.querySelectorAll('.wb-pane').forEach(x=>x.hidden=x.id!=='pane-'+b.dataset.pane);if(b.dataset.pane==='calls'){loadCalls();loadV2Calls();}});
document.getElementById('task-search').oninput=e=>{const q=e.target.value.trim().toLowerCase();document.querySelectorAll('#task-settings .wb-form-row').forEach(x=>x.hidden=!x.dataset.search.includes(q));document.querySelectorAll('#task-settings details').forEach(x=>{x.hidden=![...x.querySelectorAll('.wb-form-row')].some(r=>!r.hidden);if(q&&!x.hidden)x.open=true;});};
reloadCenter();

/* The route editor uses the existing API stores, never a second config copy. */
function renderSchedulingIcons(root){
 if(!window.lucide)return;
 root.querySelectorAll('i[data-lucide]').forEach(node=>node.setAttribute('data-scheduling-icon',node.dataset.lucide));
 window.lucide.createIcons({nameAttr:'data-scheduling-icon'});
 root.querySelectorAll('svg[data-scheduling-icon]').forEach(node=>{node.removeAttribute('data-scheduling-icon');node.removeAttribute('data-lucide');});
}
const routeBindings={episode_extract:['task','episode_extraction_provider_id'],narrative_plan:['task','narrative_plan_provider_id'],diary_write:['task','diary_render_provider_id'],diary_review:['task','diary_review_provider_id'],compress_llm:['task','compress_provider_id'],semantic_state:['task','semantic_state_provider_id'],profile:['task','profile_provider_id'],query_plan:['task','query_plan_llm_provider_id'],xinchao_live:['mind','live_perception_provider_id'],xinchao_post:['mind','perception_provider_id'],xinchao_dream:['mind','dream_provider_id'],xinchao_proactive:['mind','proactive_provider_id'],xinchao_daytime:['mind','proactive_provider_id'],time_insight:['task','time_insight_llm_provider_id'],thread_consistency:['task','consistency_llm_provider_id'],thread_arbitration:['task','thread_llm_provider_id']};
const routePanel=document.createElement('div');routePanel.id='atelier-routes';
document.getElementById('task-settings').before(routePanel);
for(const id of ['task-settings','mind-tasks']){const root=document.getElementById(id),fold=document.createElement('details');fold.className='wb-fold atelier-route-details';fold.innerHTML='<summary>'+(id==='task-settings'?'全部模型参数与执行预算':'心潮通道参数')+'</summary>';root.before(fold);fold.append(root);}
function selectedLabel(control){return control?.selectedOptions?.[0]?.textContent||control?.value||'读取中';}
function drawRouteList(){
 const query=document.getElementById('task-search').value.trim().toLowerCase();
 routePanel.innerHTML='<div class="atelier-route-head"><span>任务</span><span>主模型</span><span></span><span>故障跟接</span><span></span></div>'+center.routeOptions.filter(t=>(t.label+' '+t.value).toLowerCase().includes(query)).map(t=>{
  const binding=routeBindings[t.value],primary=binding?document.getElementById(binding.join('-')):null;
  const backup=document.getElementById('route-'+t.value),follow=state.astr_followup_enabled?selectedLabel(backup):'总开关已关闭';
  return '<button class="atelier-route-row" data-edit-route="'+esc(t.value)+'"><div><strong>'+esc(t.label)+'</strong><small>'+esc(t.value)+'</small></div><span>'+esc(selectedLabel(primary))+'</span><i data-lucide="arrow-right"></i><span>'+esc(follow)+'</span><i data-lucide="chevron-right"></i></button>';
 }).join('')+'<p class="atelier-route-caption">仅改变后续任务；6.1 在途作业继续使用冻结合同。</p>';
 if(!center.routeOptions.length)routePanel.innerHTML='<div class="empty">正在读取任务路线</div>';
 routePanel.querySelectorAll('[data-edit-route]').forEach(b=>b.onclick=()=>openRouteEditor(b.dataset.editRoute));
 renderSchedulingIcons(routePanel);
}
const routeObserver=new MutationObserver(drawRouteList);
for(const id of ['task-settings','mind-tasks','route-settings'])routeObserver.observe(document.getElementById(id),{childList:true,subtree:true});
document.getElementById('task-search').addEventListener('input',()=>{drawRouteList();if(document.getElementById('task-search').value.trim())document.getElementById('task-settings').parentElement.open=true;});
document.getElementById('followup-enabled').addEventListener('change',drawRouteList);
async function openRouteEditor(key){
 if(hasDirty()){toast('请先保存或撤销其他页面中未保存的模型设置，再编辑任务路线',true);return;}
 const binding=routeBindings[key],source=binding&&document.getElementById(binding.join('-')),backup=document.getElementById('route-'+key),task=center.routeOptions.find(t=>t.value===key);
 if(!source||!backup){toast('任务配置尚未就绪，请刷新后重试',true);return;}
 const focus=document.activeElement,mask=document.createElement('div'),panel=document.createElement('aside');let saving=false,edited=false;
 mask.className='atelier-scrim';panel.className='atelier-drawer';panel.setAttribute('role','dialog');panel.setAttribute('aria-modal','true');panel.setAttribute('aria-labelledby','route-editor-title');
 panel.innerHTML='<header><h2 id="route-editor-title">'+esc(task.label)+'</h2><button title="关闭" class="route-close"><i data-lucide="x"></i></button></header><div class="atelier-drawer-body"><label>主模型<select id="edit-route-primary">'+source.innerHTML+'</select></label><label>故障跟接<select id="edit-route-backup">'+backup.innerHTML+'</select></label><p>'+(state.astr_followup_enabled?'任务专属路线优先于类别默认。':'跟接总开关当前关闭。可保存路线，但不会自动启用跟接。')+'</p>'+(key==='xinchao_daytime'?'<p>白天浮现沿用心潮主动内容模型；修改此项也会影响主动内容。</p>':'')+'<details class="wb-fold"><summary>执行与保存边界</summary><p>不更改既有任务的预算、重试次数或冻结合同。主模型与跟接分别存储；若其中一步失败，会明确显示已保存的部分。</p></details><p class="atelier-drawer-error" role="status"></p></div><footer><span>保存到插件真实配置</span><button class="btn primary route-save">保存路线</button></footer>';
 document.body.append(mask,panel);const oldOverflow=document.body.style.overflow;document.body.style.overflow='hidden';
 const primary=panel.querySelector('#edit-route-primary'),secondary=panel.querySelector('#edit-route-backup'),status=panel.querySelector('[role=status]'),button=panel.querySelector('.route-save');
 primary.value=source.value;secondary.value=backup.value;let initialPrimary=source.value,initialBackup=backup.value;
 const policy={...(state.task_call_defaults?.[key]||{stream:true,thinking:'enabled',reasoning_effort:'low',max_tokens:32768,idle_timeout:60}),...(state.task_call_policies?.[key]||{})};
 const policyBox=document.createElement('fieldset');policyBox.className='wb-fold';policyBox.innerHTML='<legend>任务调用参数</legend><label>流式接收<input type="checkbox" data-policy="stream"'+(policy.stream?' checked':'')+'></label><label>思考模式<select data-policy="thinking"><option value="inherit">继承模型设置</option><option value="enabled">开启思考</option><option value="disabled">关闭思考</option></select></label><label>思考强度<select data-policy="reasoning_effort"><option value="low">低</option><option value="high">高</option><option value="max">最高</option></select></label><label>输出预算（tokens）<input type="number" min="256" max="131072" step="1" data-policy="max_tokens"></label><label>有效输出静默闸门（秒）<input type="number" min="10" max="300" step="1" data-policy="idle_timeout"></label><small>思考控制适用于 DeepSeek；其他模型保留提供商配置。输出预算包含模型生成的推理和正文，仍受外置模型上限限制。持续有效输出可超过单次首次输出等待；默认连续 60 秒无有效正文或推理才停止。空心跳不续时，作业总期限仍有效。</small>';
 panel.querySelector('.atelier-drawer-body').append(policyBox);
 const contextControl=document.createElement('label');contextControl.innerHTML='上下文窗口（tokens，0 不检查）<input type="number" min="0" max="2097152" step="1" data-policy="context_window_tokens">';
 policy.context_window_tokens??=0;policyBox.append(contextControl);
 for(const e of policyBox.querySelectorAll('[data-policy]')){if(e.type!=='checkbox')e.value=policy[e.dataset.policy];e.onchange=()=>{edited=true;};}
 const readPolicy=()=>Object.fromEntries([...policyBox.querySelectorAll('[data-policy]')].map(e=>[e.dataset.policy,e.type==='checkbox'?e.checked:e.type==='number'?Number(e.value):e.value]));
 primary.onchange=secondary.onchange=()=>{edited=true;};
 function close(force=false){if(saving)return;if(edited&&!force&&!confirm('放弃尚未保存的路线修改？'))return;mask.remove();panel.remove();document.body.style.overflow=oldOverflow;document.removeEventListener('keydown',keyDown);focus?.focus();}
 function keyDown(e){if(e.key==='Escape'){e.preventDefault();close();}if(e.key==='Tab'){const nodes=[...panel.querySelectorAll('button,input,select,summary')].filter(n=>!n.disabled&&n.getClientRects().length),first=nodes[0],last=nodes.at(-1);if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}}}
 document.addEventListener('keydown',keyDown);mask.onclick=()=>close();panel.querySelector('.route-close').onclick=()=>close();
 button.onclick=async()=>{
  for(const e of policyBox.querySelectorAll('input'))if(!e.reportValidity())return;
  saving=true;button.disabled=true;primary.disabled=true;secondary.disabled=true;status.textContent='正在保存…';const notes=[];
  try{
   if(primary.value!==initialPrimary){const values={[binding[1]]:primary.value};const d=await request(binding[0]==='mind'?'/api/xinchao/settings/save':'/api/settings/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(binding[0]==='mind'?{settings:values}:{values})});initialPrimary=primary.value;notes.push('主模型已保存');if(d.restart_required?.length)notes.push('部分参数需要重载');await(binding[0]==='mind'?loadMind():loadTaskSettings());}
   if(secondary.value!==initialBackup||JSON.stringify(readPolicy())!==JSON.stringify(policy)){const map={...(state.astr_followup_models||{})};if(secondary.value==='__inherit__')delete map[key];else map[key]=secondary.value;
    const payload={enabled:state.enabled,fallback_enabled:state.fallback_enabled,default_model_id:state.default_model_id,astr_followup_enabled:state.astr_followup_enabled,astr_followup_model_id:state.astr_followup_model_id,astr_followup_tasks:state.astr_followup_tasks,astr_followup_timeout:state.astr_followup_timeout,astr_followup_models:map,models:state.models};
    payload.task_call_policies={...(state.task_call_policies||{}),[key]:readPolicy()};
    const options=state.followup_task_options,data=await originalRequest('/api/models/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});state=data;state.followup_task_options=options;state.task_route_options=center.routeOptions;initialBackup=secondary.value;notes.push('跟接路线及调用参数已保存');render();}
   edited=false;saving=false;close(true);drawRouteList();toast(notes.join('；')||'没有需要保存的更改');
  }catch(e){status.textContent=(notes.length?notes.join('；')+'。':'')+'未全部保存：'+e.message;}
  finally{saving=false;button.disabled=false;primary.disabled=false;secondary.disabled=false;}
 };
 renderSchedulingIcons(panel);panel.querySelector('.route-close').focus();
}
drawRouteList();

const probeButton=document.createElement('button');probeButton.className='btn';
probeButton.innerHTML='<i data-lucide="flask-conical"></i> 单模型测试';
probeButton.onclick=()=>openModelProbe();document.querySelector('.top .actions').prepend(probeButton);
async function openModelProbe(selected=''){
 if(hasDirty()){toast('请先保存或撤销未保存的设置。测试参数不会覆盖正式配置。',true);return;}
 const options=[...(center.probeProviders||[]),...(state.models||[]).filter(m=>m.enabled&&m.has_api_key).map(m=>({value:'external:'+m.id,label:'外置 / 跟接 · '+(m.name||m.model||m.id)}))];
 const focus=document.activeElement,mask=document.createElement('div'),panel=document.createElement('aside');let running=false;
 mask.className='atelier-scrim';panel.className='atelier-drawer';panel.setAttribute('role','dialog');panel.setAttribute('aria-modal','true');panel.setAttribute('aria-labelledby','probe-title');
 panel.innerHTML='<header><h2 id="probe-title">单模型测试</h2><button class="probe-close" title="关闭"><i data-lucide="x"></i></button></header><form class="atelier-drawer-body" id="probe-form"><label>模型<select name="provider" required><option value="">请选择模型</option>'+options.map(o=>'<option value="'+esc(o.value)+'">'+esc(o.label)+'</option>').join('')+'</select></label><label>任务类别<select name="task">'+center.routeOptions.map(t=>'<option value="'+esc(t.value)+'">'+esc(t.label)+'</option>').join('')+'</select></label><button type="button" class="btn probe-load">载入该任务的已存参数</button><label>测试输入<textarea name="prompt" rows="4" maxlength="20000" required>请只回复：连接测试成功。</textarea></label><label>流式接收<input name="stream" type="checkbox" checked></label><label>思考模式<select name="thinking"><option value="disabled">关闭思考</option><option value="enabled">开启思考</option><option value="inherit">继承模型设置</option></select></label><label>思考强度<select name="reasoning_effort"><option value="low">低</option><option value="high">高</option><option value="max">最高</option></select></label><label>输出预算（tokens）<input name="max_tokens" type="number" min="256" max="131072" value="1024" required></label><label>上下文窗口（tokens）<input name="context_window_tokens" type="number" min="0" max="2097152" value="0" required></label><label>有效输出静默闸门（秒）<input name="idle_timeout" type="number" min="10" max="300" value="60" required></label><label>首次有效输出等待 / 非流式总超时（秒）<input name="timeout" type="number" min="5" max="300" value="90" required></label><p class="muted">只尝试选中的模型一次，不自动重试、跟接或写入记忆。参数仅用于本次测试。思考参数适用于 DeepSeek，其他模型保留提供商设置。上下文 0 表示不额外检查；外置模型已有的容量上限仍然生效。容量采用保守字节估算，不是实际 token 数，不会截断输入。</p><label><input name="consent" type="checkbox" required> 我确认本次测试可能产生模型费用</label><button class="btn primary probe-run" type="submit">开始测试</button><section class="probe-result" aria-live="polite"></section></form><footer><span>临时测试 · 不保存生产参数</span></footer>';
 document.body.append(mask,panel);const oldOverflow=document.body.style.overflow;document.body.style.overflow='hidden';
 const form=panel.querySelector('form'),field=n=>form.elements.namedItem(n),result=panel.querySelector('.probe-result');field('provider').value=selected;
 panel.querySelector('.probe-load').onclick=()=>{const p={...(state.task_call_defaults?.[field('task').value]||{}),...(state.task_call_policies?.[field('task').value]||{})};for(const [k,v] of Object.entries(p)){const e=field(k);if(e){if(e.type==='checkbox')e.checked=v;else e.value=v;}}};
 const close=()=>{if(running){toast('测试仍在执行，请等待结果，避免重复调用',true);return;}mask.remove();panel.remove();document.body.style.overflow=oldOverflow;document.removeEventListener('keydown',keyDown);focus?.focus();};
 function keyDown(e){if(e.key==='Escape'){e.preventDefault();close();}if(e.key==='Tab'){const nodes=[...panel.querySelectorAll('button,input,textarea,select')].filter(n=>!n.disabled),first=nodes[0],last=nodes.at(-1);if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}}}
 document.addEventListener('keydown',keyDown);mask.onclick=close;panel.querySelector('.probe-close').onclick=close;
 form.onsubmit=async e=>{
  e.preventDefault();if(running||!form.reportValidity())return;
  const policy={};for(const name of ['stream','thinking','reasoning_effort','max_tokens','context_window_tokens','idle_timeout']){const f=field(name);policy[name]=f.type==='checkbox'?f.checked:f.type==='number'?Number(f.value):f.value;}
  const body={provider_id:field('provider').value,task:field('task').value,prompt:field('prompt').value,timeout:Number(field('timeout').value),policy,confirm_model_calls:true,request_id:crypto.randomUUID()};
  running=true;for(const control of form.elements)control.disabled=true;result.textContent='测试正在执行，等待完整结果…';
  try{const d=await request('/api/models/probe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),diag=d.diagnostics||{};
   result.innerHTML='<h3>'+esc(d.success?'测试成功':'测试未成功：'+(d.error_kind||'未完成'))+'</h3><dl>'+[['耗时',d.elapsed_ms+' ms'],['尝试次数',d.attempts],['结束原因',diag.finish_reason||'未提供'],['实际输出预算',diag.max_tokens??policy.max_tokens],['上下文容量',diag.context_window_tokens||'未限制'],['输入保守估算',diag.input_token_upper_estimate??'未提供'],['首个事件',diag.first_event_ms==null?'未提供':diag.first_event_ms+' ms'],['首个正文',diag.first_content_ms==null?'未提供':diag.first_content_ms+' ms'],['输入 tokens',d.usage?.prompt_tokens??'未提供'],['输出 tokens',d.usage?.completion_tokens??'未提供'],['实际流式',diag.stream===false?'否':diag.stream===true?'是':'未提供']].map(([k,v])=>'<dt>'+esc(k)+'</dt><dd>'+esc(v)+'</dd>').join('')+'</dl><pre style="white-space:pre-wrap;overflow-wrap:anywhere"></pre><small>'+esc(d.job_id)+(d.text_truncated?' · 界面仅显示前 12000 字符':'')+'</small>';
   result.querySelector('pre').textContent=d.text||'没有最终正文';
  }catch(err){result.textContent='测试请求失败：'+err.message+'。若浏览器连接中断，请先查看调用记录，避免重复付费。';}
  finally{running=false;for(const control of form.elements)control.disabled=false;field('consent').checked=false;}
 };
 renderSchedulingIcons(panel);field('provider').focus();
}
