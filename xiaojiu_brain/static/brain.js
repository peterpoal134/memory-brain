/* Memory Brain. Read-only UI; original licensed cortical renderer, local data. */
import {activityView,wavePath} from './brain-activity.js';
const $=id=>document.getElementById(id), TOKEN_KEY='xiaojiu.management.token';
const motionPreference=matchMedia('(prefers-reduced-motion: reduce)');
const regionColors={raw:'#66e8ff',facts:'#ffcf83',summaries:'#c09bff',semantic:'#8dc7ff',self:'#f0b3ff',emotion:'#ffb0c9',life:'#8be0c2',queue:'#f4d28b',imports:'#8fb6d4'};
const memoryColors={fact:'#ffcf83',summary:'#c09bff',raw:'#66e8ff'},kindNames={fact:'事实记忆',summary:'摘要记忆',raw:'原话片段'};
const options={mode:'memories',running:!motionPreference.matches,
  query:'',active:['memory_fact','memory_summary','memory_raw'],density:32,glow:55,art:true,
  autoRotate:!motionPreference.matches,rotationDirection:'cw',rotationSpeed:.045,workIntensity:0};
let token=sessionStorage.getItem(TOKEN_KEY)||'', payload=null, selected='raw', scene=null;
let busy=false, disposed=false, rendererFailed=false, rendererReady=false, cinema=false;
let controller=null, modelClass=null,authenticated=false,authEpoch=0,authTask=null,refreshTask=null;
let activity=null,activityBusy=false,activityController=null,waveRaf=0,lastWaveFrame=0,lastWorkState='';
let memoryItems=[],memoryPage=null,memorySelection=null,memoryFull=null,memoryBusy=false,memoryError='';
let memoryController=null,detailController=null,memoryGeneration=0;
const num=v=>typeof v==='number'&&Number.isFinite(v)?v.toLocaleString('zh-CN'):'—';
const percentage=v=>typeof v==='number'&&Number.isFinite(v)?(v*100).toFixed(1)+'%':'暂无记录';
const date=v=>typeof v==='number'&&Number.isFinite(v)?new Date(v*1000).toLocaleString('zh-CN',{hour12:false}):'未报告';
function el(tag,text='',cls=''){const n=document.createElement(tag);n.textContent=text;if(cls)n.className=cls;return n;}
function text(id,value){const node=$(id);if(node&&node.textContent!==String(value))node.textContent=value;}
function row(label,value,muted=false){const n=el('div','','property-row');n.append(el('span',label),el('strong',value,muted?'muted':''));return n;}
function setConnection(label,detail,warn=false){text('connection-state',label);text('connection-detail',detail);$('connection-state')?.closest('.live')?.classList.toggle('warning',warn);}
function panelLink(node){const allowed=new Set(['overview','messages','memories','recall','maintenance','extensions','life','profile']);
  const p=new URLSearchParams({page:allowed.has(node?.panel_page)?node.panel_page:'overview'});
  if(['raw','fact','summary'].includes(node?.panel_kind))p.set('kind',node.panel_kind);
  return '/panel?'+p.toString();}
function scrubPrivateView(){
  memoryGeneration++;memoryController?.abort();detailController?.abort();memoryItems=[];memoryPage=null;memorySelection=null;memoryFull=null;memoryBusy=false;memoryError='';
  payload=null;scene?.setData({nodes:[],edges:[]});
  text('node-tooltip','');$('node-tooltip').style.display='none';
  for(const id of ['metric-messages','metric-facts','metric-vectors','node-count','usage-input','usage-output','usage-cache','usage-calls','usage-unknown'])text(id,'—');
  for(const id of ['domain-list','pipeline-list','policy-summary','life-signals','pad-signals'])$(id)?.replaceChildren();
  text('node-title','在皮层上选择一条记忆');text('node-unit','');text('node-description','登录后查看真实记忆。');
  text('memory-meta','');text('memory-map-status','登录后读取真实记忆。');$('memory-sources').replaceChildren();$('memory-expand').hidden=true;
  $('memory-source-content').replaceChildren();if($('memory-source-dialog').open)$('memory-source-dialog').close();
  text('life-mode','未读取');text('last-sync','尚未读取数据');
  activity=null;renderActivity();
}
function showLogin(show=true){authenticated=false;authEpoch++;token='';sessionStorage.removeItem(TOKEN_KEY);controller?.abort();activityController?.abort();scrubPrivateView();
  $('login-overlay').hidden=false;$('management-token').value='';setConnection('尚未登录','LOCAL MEMORY CORE',true);
  if(show)text('login-error','');}
async function logout(){const b=$('logout');b.disabled=true;
  try{await api('/v1/session/logout',{});showLogin();text('login-error','已退出此浏览器的管理登录。需要时可从桌面入口重新打开。');}
  catch{if(authenticated)text('connection-detail','暂未能退出，请重试；登录尚未撤销。');}
  finally{b.disabled=false;}}
async function api(path,body,auth=true,signal){
  const requestEpoch=authEpoch;
  const ac=new AbortController(),abort=()=>ac.abort();signal?.addEventListener('abort',abort,{once:true});
  const timer=setTimeout(abort,10000);
  try{const r=await fetch(path,{method:body===undefined?'GET':'POST',signal:ac.signal,redirect:'error',credentials:'same-origin',
    headers:{...(auth&&token?{Authorization:'Bearer '+token}:{}),...(body===undefined?{}:{'Content-Type':'application/json'})},
    body:body===undefined?undefined:JSON.stringify(body)});
    const p=await r.json().catch(()=>null);if(auth&&requestEpoch!==authEpoch)throw new Error('登录状态已变化，请重新检查');
    if(!r.ok||p?.ok!==true){if(r.status===401&&auth&&authenticated)showLogin(false);throw new Error(p?.error||'本机服务暂不可用');}return p.data;
  }finally{clearTimeout(timer);signal?.removeEventListener('abort',abort);}}
function dataForScene(){
  return {nodes:memoryItems.map(n=>({...n,id:'memory:'+n.id,documentId:n.id,category:'memory_'+n.kind,
    label:n.restricted?'受限记忆':n.preview,color:memoryColors[n.kind]||'#8dc7ff'})),edges:[]};
}
function choose(id){const documentId=String(id).startsWith('memory:')?String(id).slice(7):id;
  if(!memoryItems.some(n=>n.id===documentId))return;memorySelection=documentId;memoryFull=null;detailController?.abort();scene?.select('memory:'+documentId);renderSelected();}
function renderRegions(){
  const container=$('domain-list'),activeId=document.activeElement?.dataset?.node;
  const nodes=payload?.scene?.nodes||[];
  const items=nodes.map(n=>{const b=el('button','','region-button');b.type='button';b.dataset.node=n.id;
    const dot=el('i','','region-dot');dot.style.background=regionColors[n.id]||'#8dc7ff';
    b.append(dot,el('span',n.label),el('small',num(n.count)));b.addEventListener('click',()=>location.assign(panelLink(n)));return b;});
  container.replaceChildren(...items);if(!items.length)container.append(el('p','没有匹配的功能区。','muted'));
  if(activeId)container.querySelector(`[data-node="${activeId}"]`)?.focus({preventScroll:true});
}
function renderSelected(){
  const n=memoryItems.find(n=>n.id===memorySelection);$('memory-sources').replaceChildren();$('memory-expand').hidden=true;$('memory-expand').textContent='展开完整记忆';
  if(!n){
    text('node-title',memoryPage?($('memory-days').value==='all'?'全部时间的记忆':$('memory-days').selectedOptions[0].textContent+'的记忆'):'在皮层上选择一条记忆');
    text('node-count',memoryPage?num(memoryItems.length):'—');text('node-unit',memoryPage?'/ '+num(memoryPage.total)+' 条已加载':'');
    text('node-description',memoryPage&&!memoryItems.length?'这个时间范围暂无这类记忆，可切换时间或记忆类型。':'每个亮点对应一条实际记忆索引。悬停看内容片段，点击后展开详情与来源。');
    text('memory-meta','位置是稳定的显示布局，不表示真实脑区。');$('node-open').href='/panel?page=memories';return;
  }
  text('node-title',n.restricted?'受限记忆':n.source_kind==='imported_note'?'导入笔记':n.source_kind==='owner_note'?'手写笔记':kindNames[n.kind]||'记忆');text('node-count',n.pinned?'已置顶':'记忆');
  text('node-unit',n.recorded_at>0?date(n.recorded_at):'时间未标注');
  text('node-description',memoryFull?.id===n.id?memoryFull.text:n.preview);
  const origins={derived:'由原话派生的索引',original:'原始对话记录',imported_note:'导入笔记 · 原说话者与日期未核实',owner_note:'用户手写笔记'};
  text('memory-meta',(origins[n.source_kind]||'记忆索引')+' · 来源 '+num(n.source_count)+' 条'+(Number.isFinite(n.importance)?' · 重要性 '+n.importance.toFixed(2):''));
  $('node-open').href='/panel?page=memories&kind='+n.kind;
  $('memory-expand').hidden=n.restricted||memoryFull?.id===n.id;$('memory-expand').disabled=false;
  if(!n.restricted){for(const id of n.source_ids||[]){const b=el('button','查看来源 #'+id);b.type='button';b.addEventListener('click',()=>openMemorySource(id));$('memory-sources').append(b);}}
}
function renderMapStatus(){
  const q=options.query.trim().toLowerCase(),matching=memoryItems.filter(n=>(n.restricted?'受限记忆':n.preview).toLowerCase().includes(q)).length;
  let status=memoryBusy?(memoryItems.length?'正在加载更多记忆…':'正在读取真实记忆…'):memoryError;
  if(!status&&memoryPage)status='已加载 '+num(memoryItems.length)+' / '+num(memoryPage.total)+' 条'+(q?' · 当前匹配 '+num(matching)+' 条':'')+(memoryItems.length>=600&&memoryPage.has_more?' · 已达600点上限':'')+' · 按原记录时间';
  text('memory-map-status',status||'登录后读取真实记忆。');
  $('reload-memories').disabled=!authenticated||memoryBusy;
  $('load-more-memories').hidden=!memoryPage?.has_more||memoryItems.length>=600;
  $('load-more-memories').disabled=memoryBusy;
}
async function loadMemories(reset=false){
  if(!authenticated||disposed)return;
  if(reset){memoryGeneration++;memoryController?.abort();detailController?.abort();memoryItems=[];memoryPage=null;memorySelection=null;memoryFull=null;scene?.setData(dataForScene());}
  else if(memoryBusy||memoryItems.length>=600)return;
  const generation=memoryGeneration,epoch=authEpoch;memoryBusy=true;memoryError='';
  const ac=new AbortController();memoryController=ac;renderMapStatus();renderSelected();
  const params=new URLSearchParams({days:$('memory-days').value,kind:$('memory-kind').value,limit:String(Math.min(200,600-memoryItems.length)),offset:String(memoryPage?.next_offset??0)});
  if(memoryPage)params.set('until',String(memoryPage.until));
  try{const page=await api('/v1/memory-map?'+params.toString(),undefined,true,ac.signal);
    if(!authenticated||disposed||epoch!==authEpoch||generation!==memoryGeneration)return;
    if(page?.schema_version!==1||!Array.isArray(page.items))throw new Error('记忆视图接口不兼容');
    const byId=new Map(memoryItems.map(n=>[n.id,n]));for(const n of page.items)byId.set(n.id,n);
    memoryItems=[...byId.values()].slice(0,600);memoryPage=page;scene?.setData(dataForScene());scene?.select(memorySelection?'memory:'+memorySelection:null);renderSelected();
  }catch(e){if(generation===memoryGeneration&&authenticated&&e.name!=='AbortError')memoryError='记忆暂未读取，请点击刷新重试。';}
  finally{if(generation===memoryGeneration){memoryBusy=false;if(memoryController===ac)memoryController=null;renderMapStatus();}}
}
async function expandMemory(){
  const n=memoryItems.find(n=>n.id===memorySelection);if(!n||n.restricted)return;
  detailController?.abort();const ac=new AbortController(),generation=memoryGeneration,epoch=authEpoch;detailController=ac;$('memory-expand').disabled=true;
  try{const detail=await api('/v1/memory-map/'+encodeURIComponent(n.id),undefined,true,ac.signal);
    if(authenticated&&!ac.signal.aborted&&detailController===ac&&generation===memoryGeneration&&epoch===authEpoch&&memorySelection===n.id){if(detail.restricted){n.restricted=true;n.preview=detail.preview;scene?.setData(dataForScene());}
      else if(typeof detail.full_text==='string')memoryFull={id:n.id,text:detail.full_text};renderSelected();}}
  catch(e){if(e.name!=='AbortError'&&authenticated&&detailController===ac&&generation===memoryGeneration&&epoch===authEpoch&&memorySelection===n.id){$('memory-expand').textContent='读取失败，点击重试';}}
  finally{if(detailController===ac&&memorySelection===n.id)$('memory-expand').disabled=false;}
}
async function openMemorySource(id){
  const n=memoryItems.find(n=>n.id===memorySelection);if(!n||n.restricted)return;
  detailController?.abort();const ac=new AbortController(),generation=memoryGeneration,epoch=authEpoch;detailController=ac;
  const box=$('memory-source-content');box.replaceChildren(el('p','正在读取来源…'));$('memory-source-dialog').showModal();
  try{const detail=await api('/v1/memory-map/'+encodeURIComponent(n.id),undefined,true,ac.signal);
    if(!authenticated||ac.signal.aborted||detailController!==ac||generation!==memoryGeneration||epoch!==authEpoch||memorySelection!==n.id||!$('memory-source-dialog').open)return;
    const sources=detail.restricted?[]:(detail.sources||[]).filter(s=>s.message_id===id);
    box.replaceChildren(...sources.flatMap(raw=>{const kind=raw.source_kind,role=kind==='imported_memory'?'导入笔记 · 原说话者未核实':kind==='owner_note'?'用户手写笔记':raw.role==='user'?'用户':raw.role==='assistant'?'助手':'来源未核实';
      return [el('p',role+' · '+(raw.recorded_at>0?date(raw.recorded_at):'时间未标注')+' · 所引片段','memory-meta'),el('p',raw.text||'')];}));
    if(!sources.length)box.append(el('p','来源暂不可展示，请到控制面板核对。'));}
  catch(e){if(e.name!=='AbortError'&&authenticated&&detailController===ac&&generation===memoryGeneration&&epoch===authEpoch)box.replaceChildren(el('p','来源暂未读到，请稍后重试。'));}
}
function renderPipeline(){
  const p=payload.pipeline,j=p.jobs.by_status,settings=p.policy||{};
  const state=(v)=>v===true?'已启用':'已关闭';
  const model=(v)=>!v?.configured?'未配置':v.last_error?'已配置 · 最近异常':'已配置';
  $('pipeline-list').replaceChildren(
    row('原话接收',state(p.features.capture_enabled)),row('记忆检索',state(p.features.recall_enabled)),
    row('语义索引',model(p.embedding)),row('整理模型',model(p.derivation)),
    row('待处理 / 运行中',num(j.pending+j.retry)+' / '+num(j.running)),row('失败任务',num(j.failed)),
    row('向量模型',p.embedding.model||'未配置'),row('整理模型名',p.derivation.model||'未配置'));
  $('policy-summary').replaceChildren(row('证据预算',num(settings.token_budget)+' tokens'),
    row('整理批量',num(settings.derive_batch_messages)+' 条消息'),
    row('最长积累',typeof settings.derive_max_wait_seconds==='number'?num(settings.derive_max_wait_seconds/60)+' 分钟':'未报告'),
    row('版本',payload.service.version));
  if(p.derivation.metering_paused)$('policy-summary').append(el('p','整理计量异常，付费整理已暂停；原话仍保留。','warning-text'));
}
function signal(container,label,value,pad=false){
  const r=el('div','','signal-row'),head=el('div','','signal-heading');
  head.append(el('span',label),el('strong',typeof value==='number'?(pad?value.toFixed(2):percentage(value)):'未报告'));
  const meter=el('progress');meter.max=pad?2:1;meter.value=typeof value==='number'?(pad?value+1:value):0;
  meter.hidden=typeof value!=='number';meter.setAttribute('aria-label',label+'（规则信号）');r.append(head,meter);container.append(r);
}
function renderSignals(){
  const life=payload.life,pad=payload.pad;
  text('life-mode',life.error?'生活状态暂不可读':!life.enabled?'生活层已关闭':life.rhythm?.phase==='rest'?'休息时段 · 规则模拟':'清醒时段 · 规则模拟');
  const a=$('life-signals');a.replaceChildren();
  for(const [key,label]of Object.entries({energy:'精力',boredom:'无聊',social:'社交需求',curiosity:'好奇'}))signal(a,label,life.needs?.[key]);
  const b=$('pad-signals');b.replaceChildren(el('p',pad.enabled?'PAD 情绪信号（用户管理）':'PAD 情绪扩展已关闭','section-note'));
  for(const [key,label]of Object.entries({pleasure:'愉悦度',arousal:'唤醒度',dominance:'掌控感'}))signal(b,label,pad[key],true);
}
function renderUsage(){const u=payload.usage;
  text('usage-input',num(u.input_tokens));text('usage-output',num(u.output_tokens));
  text('usage-cache',percentage(u.cache_hit_ratio));text('usage-calls',num(u.attempts));
  text('usage-unknown',num(u.unknown_usage_attempts));
  text('usage-scope','仅后台摘要与事实整理，不含主聊天。未报告用量不按 0 计；缓存比率只统计已报告的后台输入。');
}
function render(){
  text('metric-messages',num(payload.storage.original_messages));
  text('metric-facts',num(payload.storage.by_kind.fact));text('metric-vectors',num(payload.storage.vectors));
  renderRegions();renderSelected();renderPipeline();renderSignals();renderUsage();
  text('last-sync','最近更新 '+date(payload.observed_at));
  scene?.setData(dataForScene());scene?.select(memorySelection?'memory:'+memorySelection:null);renderMapStatus();
  if(payload.activity){acceptActivity(payload.activity);renderActivity();}
  setConnection('本机大脑已连接','READ ONLY · '+payload.service.version);
}
function acceptActivity(next){if(Number.isFinite(next?.observed_at)&&(!activity||next.observed_at>=activity.observed_at))activity=next;}
function renderActivity(){
  const v=activityView(activity);options.workIntensity=v.intensity;
  $('work-activity').dataset.state=v.state;
  if(lastWorkState!==v.state){text('work-state',v.label);lastWorkState=v.state;}
  text('work-detail',v.detail);
  return v;
}
async function refreshActivity(){
  if(!authenticated||activityBusy||disposed||document.hidden)return;
  activityBusy=true;activityController=new AbortController();
  try{const next=await api('/v1/activity',undefined,true,activityController.signal);if(authenticated&&!disposed){acceptActivity(next);renderActivity();}}
  catch{if(authenticated){activity=null;renderActivity();}}
  finally{activityBusy=false;activityController=null;}
}
function animateWork(now){
  if(disposed)return;waveRaf=requestAnimationFrame(animateWork);
  if(document.hidden||now-lastWaveFrame<50)return;lastWaveFrame=now;
  const v=renderActivity();
  const path=wavePath(motionPreference.matches||!options.running?1:now/1000,v.intensity);
  if($('work-wave-path').getAttribute('d')!==path)$('work-wave-path').setAttribute('d',path);
}
function refresh(){
  if(!authenticated||disposed)return Promise.reject(new Error('请先从桌面入口打开或重新检查登录'));
  if(refreshTask)return refreshTask;
  busy=true;const activeController=new AbortController();controller=activeController;$('reload-data').disabled=true;
  refreshTask=(async()=>{try{const next=await api('/v1/observatory',undefined,true,activeController.signal);
    if(next?.schema_version!==1||!next.storage||!Array.isArray(next.scene?.nodes))throw new Error('接口版本暂不兼容');
    if(!authenticated||disposed)return;payload=next;render();
  }catch(e){if(e.name!=='AbortError'&&authenticated)setConnection(payload?'暂用上次数据':'暂未连接','点击刷新重试',true);throw e;
  }finally{busy=false;$('reload-data').disabled=false;if(controller===activeController)controller=null;refreshTask=null;}})();return refreshTask;
}
function modelStatus(s){
  rendererReady=s.state==='ready';$('brain-canvas').dataset.renderState=s.state;
  const box=$('model-loading');box.hidden=rendererReady;
  if(s.state==='error'){rendererFailed=true;$('brain-app').classList.add('static-mode');
    box.textContent='3D 暂不可用，已显示原版静态脑体；数据和控制面板仍可使用。';
    for(const id of ['motion-toggle','auto-rotate-toggle','rotate-view','reset-view','settings-toggle'])$(id).disabled=true;
    queueMicrotask(()=>{try{scene?.dispose();}catch{}scene=null;});}
  else if(s.message)box.textContent=s.message;
}
async function startRenderer(){
  if(new URLSearchParams(location.search).get('view')==='static'){
    modelStatus({state:'error'});return;
  }
  try{({HologramScene:modelClass}=await import('/static/brain-renderer.js'));
    scene=new modelClass($('brain-canvas'),()=>options,n=>choose(n.id),modelStatus);
    scene.setData(dataForScene());scene.select(memorySelection?'memory:'+memorySelection:null);
  }catch{modelStatus({state:'error'});}
}
function setMotion(){const b=$('motion-toggle');b.textContent=options.running?'暂停流动':'继续流动';b.setAttribute('aria-pressed',String(options.running));}
function setAutoRotate(){const b=$('auto-rotate-toggle');b.textContent=options.autoRotate?'暂停自转':'开启自转';b.setAttribute('aria-pressed',String(options.autoRotate));$('auto-rotate').checked=options.autoRotate;}
function setCinema(value){cinema=value;$('brain-app').classList.toggle('cinema',cinema);$('cinema-exit').hidden=!cinema;
  $('cinema-toggle').setAttribute('aria-pressed',String(cinema));requestAnimationFrame(()=>scene?.reset());}
function tab(name){if(!['state','life','usage'].includes(name))return;
  for(const b of document.querySelectorAll('[data-tab]')){const active=b.dataset.tab===name;
    b.setAttribute('aria-selected',String(active));b.classList.toggle('active',active);}
  for(const key of ['state','life','usage'])$('tab-'+key).hidden=key!==name;
}
$('node-search').addEventListener('input',e=>{options.query=e.target.value;renderMapStatus();});
$('memory-days').addEventListener('change',()=>{options.query='';$('node-search').value='';void loadMemories(true);});
$('memory-kind').addEventListener('change',()=>{options.query='';$('node-search').value='';void loadMemories(true);});
$('reload-memories').addEventListener('click',()=>{void loadMemories(true);});
$('load-more-memories').addEventListener('click',()=>{void loadMemories();});
$('memory-expand').addEventListener('click',()=>{void expandMemory();});
$('close-memory-source').addEventListener('click',()=>{detailController?.abort();$('memory-source-dialog').close();});
for(const b of document.querySelectorAll('[data-tab]'))b.addEventListener('click',()=>tab(b.dataset.tab));
$('motion-toggle').addEventListener('click',()=>{options.running=!options.running;setMotion();});
$('auto-rotate-toggle').addEventListener('click',()=>{options.autoRotate=!options.autoRotate;setAutoRotate();});
$('auto-rotate').addEventListener('change',e=>{options.autoRotate=e.target.checked;setAutoRotate();});
motionPreference.addEventListener('change',e=>{if(e.matches){options.autoRotate=false;options.running=false;setAutoRotate();setMotion();}});
$('rotation-direction').addEventListener('change',e=>{options.rotationDirection=e.target.value==='ccw'?'ccw':'cw';});
$('rotation-speed').addEventListener('input',e=>{options.rotationSpeed=.015*Number(e.target.value)+.015;text('rotation-period','约 '+Math.round(Math.PI*2/options.rotationSpeed)+' 秒一圈');});
$('rotate-view').addEventListener('click',()=>scene?.rotate());$('reset-view').addEventListener('click',()=>scene?.reset());
$('settings-toggle').addEventListener('click',()=>{$('visual-settings').hidden=!$('visual-settings').hidden;$('settings-toggle').setAttribute('aria-expanded',String(!$('visual-settings').hidden));});
$('glow-slider').addEventListener('input',e=>{options.glow=Number(e.target.value);});
$('density-slider').addEventListener('input',e=>{options.density=Number(e.target.value);});
$('art-toggle').addEventListener('change',e=>{options.art=e.target.checked;});
$('cinema-toggle').addEventListener('click',()=>setCinema(!cinema));$('cinema-exit').addEventListener('click',()=>setCinema(false));
$('reload-data').addEventListener('click',()=>{void Promise.allSettled([refresh(),loadMemories(true)]);});$('logout').addEventListener('click',()=>logout());
$('help-toggle').addEventListener('click',()=>$('help-dialog').showModal());$('close-help').addEventListener('click',()=>$('help-dialog').close());
window.addEventListener('keydown',e=>{if(e.key==='Escape'){setCinema(false);$('visual-settings').hidden=true;$('settings-toggle').setAttribute('aria-expanded','false');}
  if(e.target===$('brain-canvas')&&scene){if(e.key==='ArrowLeft'){e.preventDefault();scene.rotate();}
    if(e.key==='ArrowRight'){e.preventDefault();for(let i=0;i<9;i++)scene.rotate();}
    if(e.key==='Home'){e.preventDefault();scene.reset();}}});
$('login-form').addEventListener('submit',e=>{e.preventDefault();void initialize($('management-token').value.trim());});
$('login-retry').addEventListener('click',()=>{void initialize();});
function initialize(manualToken){
  const ticket=new URLSearchParams(location.hash.slice(1)).get('ticket');
  if(ticket)history.replaceState(null,'',location.pathname+location.search);
  if(authTask)return authTask;
  const legacy=manualToken===undefined?token:manualToken;
  authenticated=false;authEpoch++;controller?.abort();activityController?.abort();scrubPrivateView();text('login-error','正在检查本机登录…');
  for(const b of $('login-form').querySelectorAll('button'))b.disabled=true;
  authTask=(async()=>{try{
    if(refreshTask)await refreshTask.catch(()=>{});
    token='';
    let identity;
    if(ticket){await api('/v1/session/bootstrap',{ticket},false);identity=await api('/v1/session/current');}
    else{
      // Prefer an existing cookie so an obsolete tab token cannot mask a valid login.
      try{identity=await api('/v1/session/current');}catch(e){if(!legacy)throw e;}
      if(identity?.authenticated!==true&&legacy){token=legacy;await api('/v1/session/browser',{});token='';identity=await api('/v1/session/current');}
    }
    if(identity?.authenticated!==true)throw new Error('本机登录尚未确认');
    token='';sessionStorage.removeItem(TOKEN_KEY);authenticated=true;await refresh();await loadMemories(true);
    if(authenticated){$('management-token').value='';$('login-overlay').hidden=true;text('login-error','');}
  }catch(e){showLogin(false);text('login-error',e.name==='AbortError'?'本机服务响应超时，请稍后重新检查。':'暂未进入。请从桌面“忆核大脑”打开一次，再点击重新检查；也可展开高级选项手动登录。');}
  finally{for(const b of $('login-form').querySelectorAll('button'))b.disabled=false;authTask=null;}})();return authTask;
}
setMotion();setAutoRotate();tab('state');void initialize();void startRenderer();waveRaf=requestAnimationFrame(animateWork);
const polling=setInterval(()=>{if(authenticated&&!document.hidden)refresh().catch(()=>{});},15000);
const activityPolling=setInterval(()=>{void refreshActivity();},2000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&authenticated){refresh().catch(()=>{});void refreshActivity();}});
window.addEventListener('pagehide',()=>{disposed=true;clearInterval(polling);clearInterval(activityPolling);cancelAnimationFrame(waveRaf);controller?.abort();activityController?.abort();memoryController?.abort();detailController?.abort();scene?.dispose();},{once:true});
// bfcache restores must revalidate the session and recreate disposed WebGL state.
window.addEventListener('pageshow',e=>{if(e.persisted)location.reload();});
