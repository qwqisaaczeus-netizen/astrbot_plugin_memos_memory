/* Shared navigation preserves each existing page's operational controls. */
(()=>{
 function boot(){
  const path=location.pathname.replace(/\.html$/,'').replace('/dashboard','/')||'/';
  if(path==='/house'){
   const keys=['house_api_base_url','house_model','house_api_key','clear_api_key','house_timeout_seconds','house_max_output_tokens','house_max_retries','house_temperature'];
   const original=window.settingsBody;
   if(original)window.settingsBody=()=>{const values=original();keys.forEach(k=>delete values[k]);return values;};
   ['s-url','s-model','s-key','s-clear-key','s-timeout','s-max-tokens','s-retries','s-temp'].forEach(id=>{const e=document.getElementById(id);const row=e?.closest('.field')||e?.closest('label');if(row)row.hidden=true;});
   const actions=document.querySelector('.settings-actions');if(actions){const a=document.createElement('a');a.href='/models';a.textContent='API 与模型 → 调度中心';a.style.color='inherit';actions.prepend(a);actions.querySelector('[onclick="testConnection()"]')?.remove();}
   return;
  }
  document.body.classList.add('wb');if(['/console','/logs'].includes(path))document.body.classList.add('wb-console');
  if(path==='/xinchao')document.body.classList.add('wb-mind');
  if(path==='/')document.body.classList.add('wb-dashboard');
  if(['/models','/production','/compensation','/threads','/access','/forgetting'].includes(path))document.body.classList.add('wb-'+path.slice(1));
  const old=document.querySelector('.app>.side,.app>.rail,.shell>.side,.shell>.rail,.layout>aside,body>.rail,.layout>.rail');
  if(old){old.classList.add('wb-legacy-nav');old.parentElement.classList.add('wb-layout');
   const local=old.querySelector('nav');
   if(path!=='/'&&local?.querySelector('button')){local.querySelectorAll('a').forEach(a=>a.remove());local.classList.add('wb-tabs','wb-local-tabs');const main=document.querySelector('main');const header=main?.querySelector('header');if(header)header.after(local);else main?.prepend(local);}
  }
  const groups=[['记忆',[['/#memories','记忆库','library'],['/production','记忆生产','notebook-pen'],['/#timeline','月份档案','calendar-days'],['/#profile','人格与状态','contact-round'],['/#eval','召回实验室','flask-conical'],['/threads','记忆脉络','network'],['/forgetting','遗忘与可达性','layers']]],
   ['心智',[['/xinchao','心潮','activity'],['/house','心笺小院','house'],['/#context','对话与上下文','messages-square']]],
   ['运行维护',[['/models','调度中心','workflow'],['/compensation','补偿与恢复','rotate-ccw'],['/#settings','插件设置','settings-2'],['/#health','健康检查','shield-check'],['/#backups','数据备份','archive'],['/console','控制台','terminal']]],
   ['更多工具',[['/#episodic','原文与证据','file-text'],['/access','可达性分析','scan-search'],['/#feedback','召回反馈','message-square-more'],['/#activity','活动日志','list'],['/#debug','调试与测试','wrench']]]];
  const side=document.createElement('header');side.className='atelier-header';
  side.innerHTML='<a class="atelier-brand" href="/" aria-label="Memos Memory 首页"><i data-lucide="book-open-text"></i><span>Memos<small>MEMORY</small></span></a><nav aria-label="主导航"><a href="/" class="atelier-home">记忆总览</a>'+groups.map(([name,links],index)=>'<details class="atelier-nav-group"><summary aria-controls="atelier-links-'+index+'" aria-expanded="false">'+name+'</summary><div id="atelier-links-'+index+'">'+links.map(([href,label,icon])=>'<a href="'+href+'"><i data-lucide="'+icon+'" aria-hidden="true"></i>'+label+'</a>').join('')+'</div></details>').join('')+'</nav><span class="atelier-version">6.1.0</span>';
  document.body.prepend(side);
  const trail=document.createElement('div');trail.className='atelier-trail';trail.innerHTML='<span>本地工作空间 <i data-lucide="chevron-right"></i> Memos Memory</span><span class="atelier-location"></span>';side.after(trail);
  function close(){side.querySelectorAll('details').forEach(e=>e.open=false);}
  document.addEventListener('click',e=>{if(!side.contains(e.target))close();});
  side.querySelectorAll('details').forEach(e=>{
   e.addEventListener('toggle',()=>{e.querySelector('summary').setAttribute('aria-expanded',String(e.open));if(e.open)side.querySelectorAll('details').forEach(x=>{if(x!==e)x.open=false;});});
   e.querySelector('summary').addEventListener('keydown',event=>{if(event.key==='ArrowDown'){event.preventDefault();e.open=true;requestAnimationFrame(()=>e.querySelector('a')?.focus());}});
  });
  document.addEventListener('keydown',e=>{if(e.key==='Escape'){const menu=side.querySelector('details[open]');const focused=menu?.contains(document.activeElement);close();if(focused)menu.querySelector('summary').focus();}});
  const validTabs=new Set([...document.querySelectorAll('.nav [data-tab]')].map(x=>x.dataset.tab));
  function sync(){const hash=location.hash==='#overview'?'':location.hash;const target=path==='/'?'/'+hash:path;let label='工作台';side.querySelectorAll('nav a').forEach(a=>{if(a.getAttribute('href')===target){a.setAttribute('aria-current','page');label=a.textContent;}else a.removeAttribute('aria-current');});side.querySelectorAll('.atelier-nav-group').forEach(group=>group.toggleAttribute('data-current',!!group.querySelector('[aria-current=page]')));trail.querySelector('.atelier-location').textContent=label;}
  side.querySelectorAll('a[href^="/#"]').forEach(a=>a.addEventListener('click',e=>{if(path==='/'&&typeof window.setTab==='function'){const tab=a.hash.slice(1);if(validTabs.has(tab)){e.preventDefault();history.replaceState(null,'',a.getAttribute('href'));window.setTab(tab);sync();close();window.scrollTo({top:0,behavior:'instant'});}}}));
  if(path==='/'&&validTabs.has(location.hash.slice(1))&&typeof window.setTab==='function')window.setTab(location.hash.slice(1));
  function resetTabScroll(){if(path==='/'&&validTabs.has(location.hash.slice(1)))window.scrollTo({top:0,behavior:'instant'});}
  window.addEventListener('hashchange',()=>{if(path==='/'&&validTabs.has(location.hash.slice(1)))window.setTab(location.hash.slice(1));sync();resetTabScroll();});sync();
  window.addEventListener('load',()=>setTimeout(resetTabScroll,0),{once:true});
  if(path==='/'){
   const band=document.querySelector('.architecture-band');
   if(band){const title=document.createElement('h2');title.className='atelier-section-title';title.textContent='一份记忆的四个层次';band.before(title);
    [['source','archive','episodic'],['state','fingerprint','profile'],['story','book-open','memories'],['evidence','scan-text','episodic']].forEach(([cls,icon,target])=>{const card=band.querySelector('.'+cls);if(!card)return;card.insertAdjacentHTML('afterbegin','<span class="atelier-layer-icon"><i data-lucide="'+icon+'" aria-hidden="true"></i></span><i data-lucide="arrow-up-right" class="atelier-layer-arrow" aria-hidden="true"></i>');card.tabIndex=0;card.setAttribute('role','button');card.setAttribute('aria-label','查看'+card.querySelector('.layer-kicker').textContent);const open=()=>{location.hash=target;};card.onclick=open;card.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();open();}};});
   }
  }
  function renderIcons(root){
   if(!window.lucide)return;
   root.querySelectorAll('i[data-lucide]').forEach(node=>node.setAttribute('data-workbench-icon',node.dataset.lucide));
   window.lucide.createIcons({nameAttr:'data-workbench-icon'});
   root.querySelectorAll('svg[data-workbench-icon]').forEach(node=>{node.removeAttribute('data-workbench-icon');node.removeAttribute('data-lucide');});
  }
  function decorateButtons(root){
   let changed=false;
   root.querySelectorAll('button.btn,button[onclick^="load("]').forEach(button=>{
    if(button.dataset.wbIcon||button.querySelector('svg,i[data-lucide]'))return;
    const label=button.textContent.trim();
    const icon=/^刷新/.test(label)?'refresh-cw':/^保存/.test(label)?'save':/^新增模型$/.test(label)?'plus':null;
    if(!icon)return;
    button.dataset.wbIcon=icon;
    const i=document.createElement('i');i.dataset.lucide=icon;i.setAttribute('aria-hidden','true');
    if(/^刷新/.test(label)){button.textContent='';button.title=label;button.setAttribute('aria-label',label);}
    button.prepend(i);changed=true;
   });
   if(changed)renderIcons(root);
  }
  decorateButtons(document);renderIcons(document);
  let decorationPending=false;
  new MutationObserver(()=>{if(!decorationPending){decorationPending=true;requestAnimationFrame(()=>{decorationPending=false;decorateButtons(document);});}}).observe(document.querySelector('main')||document.body,{childList:true,subtree:true});
  function foldSection(section,title){
   if(!section)return;const details=document.createElement('details');details.className='wb-fold wb-section-fold';
   const summary=document.createElement('summary');summary.textContent=title;section.before(details);details.append(summary,section);
   const heading=section.querySelector('h2');if(heading)heading.hidden=true;
   details.addEventListener('toggle',()=>{if(details.open)window.dispatchEvent(new Event('resize'));});
  }
  if(path==='/access'){
   for(const [selector,title] of [['.graph-panel','可达与干扰图'],['.observation-panel','真实 Shadow 评测流水'],['.eval-panel','自动校准与回归评测'],['.takeover-panel','补充支路与断路器']])foldSection(document.querySelector(selector),title);
  }
  if(path==='/forgetting'){
   foldSection(document.getElementById('settings')?.closest('section'),'ACCESS 高级参数');
   foldSection(document.getElementById('llm-runtime')?.closest('section'),'调用运行快照');
  }
  if(path==='/threads'){
   document.getElementById('policy-provider')?.closest('label')?.setAttribute('hidden','');
   document.getElementById('provider-save')?.setAttribute('hidden','');
   const button=document.getElementById('preset-apply');if(button){const a=document.createElement('a');a.href='/models';a.textContent='任务模型';button.after(a);}
  }
  if(path==='/xinchao'){
   const moved=k=>/_provider_id$/.test(k)||k.startsWith('perception_api')||k.includes('perception_api_key')||['live_perception_timeout_seconds','post_perception_timeout_seconds','dream_timeout_seconds','proactive_timeout_seconds','time_insight_llm_timeout'].includes(k);
   function hideMoved(){document.querySelectorAll('[data-key],[data-insight-key]').forEach(e=>{if(moved(e.dataset.key||e.dataset.insightKey||'')){const row=e.closest('.setting');if(row)row.hidden=true;}});}
   const observer=new MutationObserver(hideMoved);for(const id of ['settings-sections','time-insight']){const e=document.getElementById(id);if(e)observer.observe(e,{childList:true,subtree:true});}hideMoved();
   for(const name of ['readSettings','readInsightSettings']){const fn=window[name];if(fn)window[name]=()=>{const values=fn();Object.keys(values).filter(moved).forEach(k=>delete values[k]);return values;};}
   const root=document.getElementById('settings-sections');if(root){const a=document.createElement('a');a.href='/models';a.textContent='模型、调用超时与独立 API → 调度中心';root.before(a);}
   document.getElementById('test-perception-api')?.setAttribute('hidden','');
  }
 }
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot);else boot();
})();
