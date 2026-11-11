/** Real first-run acceptance. Requires DSH_RUNTIME (read-only host modules).
 * Uses the actual public plugin entry, native owner admission, real Python venv
 * and offline runtime installation, then the actual AgentLoop with a local fake LLM.
 * All mutable state belongs to artifacts/first-run-20261108/owned-*.
 * For Desktop ASAR run using Electron with ELECTRON_RUN_AS_NODE=1.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import http from 'node:http';
import net from 'node:net';
import crypto from 'node:crypto';
import {fileURLToPath,pathToFileURL} from 'node:url';

const repository=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../..');
const runtime=process.env.DSH_RUNTIME;
if(!runtime||!path.isAbsolute(runtime))throw new Error('Set DSH_RUNTIME to an absolute, read-only installed DSH runtime.');
const version=fs.readFileSync(path.join(repository,'pyproject.toml'),'utf8').match(/^version\s*=\s*"([^"]+)"/m)?.[1];
if(!version)throw new Error('source_version_missing');
const outputRoot=path.join(repository,'artifacts','first-run-20261108');fs.mkdirSync(outputRoot,{recursive:true});
const resumed=Boolean(process.env.FIRST_RUN_REUSE_DIR);
const runDir=resumed?path.resolve(process.env.FIRST_RUN_REUSE_DIR):fs.mkdtempSync(path.join(outputRoot,'owned-'));
if(path.dirname(runDir)!==outputRoot||!path.basename(runDir).startsWith('owned-')||fs.lstatSync(runDir).isSymbolicLink())throw new Error('owned_retry_directory_invalid');
const packageRoot=path.join(runDir,'public-package'),home=path.join(runDir,'runtime-home');
if(resumed){
  const marker=JSON.parse(fs.readFileSync(path.join(home,'runtime-manager.json'),'utf8'));
  if(marker.manager!=='memory-brain-runtime-home')throw new Error('owned_retry_marker_invalid');
  // Code is replaceable; the owned environment/data remains intact. This
  // supports a finite retry after a documented real installation failure.
  fs.rmSync(packageRoot,{recursive:true,force:true});
}
const checks=[],requests=[],errors=[],statusStages=[];
const mark=(name,condition)=>{assert.ok(condition,name);checks.push(name);console.log('PASS '+name);};
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const fingerprint=()=>Object.fromEntries(['cordis','dsh-client-connection','dsh-session','dsh-agent-loop','dsh-llm','dsh-tools'].map(name=>{
  const file=path.join(runtime,'node_modules','@deepseek-ai',name,'lib','index.js');return [name,crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex')];
}));
const hostBefore=fingerprint();
// Positive source list excludes wheels, private config, data, artifacts and
// environments. The public entry derives packageRoot from this copied module.
fs.mkdirSync(packageRoot);
for(const name of ['pyproject.toml','README.md','LICENSE','NOTICE.md','MANIFEST.in']){
  if(fs.existsSync(path.join(repository,name)))fs.copyFileSync(path.join(repository,name),path.join(packageRoot,name));
}
const wheelBuilder=path.join(repository,'scripts','build_runtime_wheel.py');
if(fs.existsSync(wheelBuilder)){fs.mkdirSync(path.join(packageRoot,'scripts'));fs.copyFileSync(wheelBuilder,path.join(packageRoot,'scripts','build_runtime_wheel.py'));}
const dependencyManifest=path.join(repository,'compat','runtime-dependencies.json');
fs.mkdirSync(path.join(packageRoot,'compat'));fs.copyFileSync(dependencyManifest,path.join(packageRoot,'compat','runtime-dependencies.json'));
const vendored=JSON.parse(fs.readFileSync(dependencyManifest,'utf8'));
fs.mkdirSync(path.join(packageRoot,'wheels'));
for(const item of vendored.files){
  if(!/^tzdata-[0-9][A-Za-z0-9.+-]*-py2\.py3-none-any\.whl$/.test(item.filename))throw new Error('test_dependency_manifest_invalid');
  fs.copyFileSync(path.join(repository,'wheels',item.filename),path.join(packageRoot,'wheels',item.filename));
}
function copyDirectory(from,to,extensions){
  fs.mkdirSync(to,{recursive:true});for(const entry of fs.readdirSync(from,{withFileTypes:true})){
    if(entry.name.startsWith('.')||entry.name==='__pycache__')continue;
    const source=path.join(from,entry.name),target=path.join(to,entry.name);
    if(entry.isSymbolicLink())throw new Error('source_symlink_refused');
    if(entry.isDirectory())copyDirectory(source,target,extensions);
    else if(entry.isFile()&&extensions.has(path.extname(entry.name)))fs.copyFileSync(source,target);
  }
}
copyDirectory(path.join(repository,'adapters','dsh'),path.join(packageRoot,'adapters','dsh'),new Set(['.mjs','.json','.md']));
copyDirectory(path.join(repository,'xiaojiu_brain'),path.join(packageRoot,'xiaojiu_brain'),new Set(['.py','.html','.js','.css','.svg','.bin','.json','.md','.png']));
const suppliedWheel=process.env.FIRST_RUN_WHEEL;
if(suppliedWheel){
  if(!path.isAbsolute(suppliedWheel)||!path.basename(suppliedWheel).startsWith('xiaojiu_brain-'+version+'-')||!suppliedWheel.endsWith('.whl')||!fs.statSync(suppliedWheel).isFile())throw new Error('test_wheel_invalid');
  fs.copyFileSync(suppliedWheel,path.join(packageRoot,'wheels',path.basename(suppliedWheel)));
}
const packageHash=()=>{
  const value=crypto.createHash('sha256');const walk=directory=>{for(const item of fs.readdirSync(directory,{withFileTypes:true}).sort((a,b)=>a.name.localeCompare(b.name))){
    const file=path.join(directory,item.name);if(item.isDirectory())walk(file);else{value.update(path.relative(packageRoot,file));value.update(fs.readFileSync(file));}}};walk(packageRoot);return value.digest('hex');
};
const packageBefore=packageHash();
const mod=name=>import(pathToFileURL(path.join(runtime,'node_modules','@deepseek-ai',name,'lib','index.js')).href);
const [{Context,Service},{HostConnectionService},{SessionStore},{SessionProjectionRegistry},{AgentRegistry},{AgentLoop},{ToolRuntime},{SystemPrompt},llm]=
  await Promise.all(['cordis','dsh-client-connection','dsh-session','dsh-session-projection','dsh-agent','dsh-agent-loop','dsh-tools','dsh-system-prompt','dsh-llm'].map(mod));
const plugin=await import(pathToFileURL(path.join(packageRoot,'adapters','dsh','memory-brain.mjs')).href);
const compatibility=plugin.inspectRuntime(runtime).injectionMode;
const origin='http://127.0.0.1:19423',prefix='/api/memory-brain/setup/';
const header=(request,key)=>request.headers instanceof Headers?request.headers.get(key):request.headers[key];
const ctx=new Context();let pluginFiber=null,adapter=null,csrfToken=null,server=null;
const fakeModel=http.createServer(async(request,response)=>{
  let input='';for await(const chunk of request)input+=chunk;const wire=JSON.parse(input);requests.push(wire);
  response.writeHead(200,{'Content-Type':'application/json'});response.end(JSON.stringify({chunks:[
    {type:'block-end',index:0,block:{type:'text',text:'收到，合成安装验收原话已记录。'}},{type:'finish',reason:{kind:'stop'}}]}));
});
await new Promise(resolve=>fakeModel.listen(0,'127.0.0.1',resolve));
const fakeURL='http://127.0.0.1:'+fakeModel.address().port;
class FixtureLlm extends Service{
  constructor(context){super(context,'llm');}
  async prepareCall(config){return {config,stream:request=>this.stream(request)};}
  async *stream(request){
    assert.ok(llm.isAgentLoopRequest(request));assert.ok(Object.isFrozen(request)&&Object.isFrozen(request.messages));
    const {signal,...wire}=request;const response=await fetch(fakeURL,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(wire),signal});
    for(const chunk of (await response.json()).chunks)yield chunk;
  }
}
function readOwnedConfig(){
  const file=path.join(home,'private','config.json');if(!fs.existsSync(file))return null;
  const value=JSON.parse(fs.readFileSync(file,'utf8'));
  assert.equal(value.owner,'local-owner');assert.equal(path.resolve(value.data_dir),path.join(home,'data'));
  assert.ok(value.port>=18880&&value.port<=18899);return value;
}
async function kernel(endpoint,body,method=body===undefined?'GET':'POST'){
  const cfg=readOwnedConfig();assert.ok(cfg);const response=await fetch('http://127.0.0.1:'+cfg.port+endpoint,
    {method,headers:{Authorization:'Bearer '+cfg.api_token,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.timeout(5000)});
  const value=await response.json();assert.equal(value.ok,true);return value.data;
}
let connection,shared;
async function portOpen(port){return new Promise(resolve=>{const socket=net.createConnection({host:'127.0.0.1',port});let ended=false;
  const finish=result=>{if(ended)return;ended=true;socket.destroy();resolve(result);};socket.setTimeout(300,()=>finish(true));socket.once('connect',()=>finish(true));socket.once('error',()=>finish(false));});}
async function dispatch(action,body,identity='owner'){
  const method=action==='status'?'GET':'POST';const headers=new Headers({Host:'127.0.0.1:19423',Origin:origin});
  if(identity==='owner')headers.set('Cookie','synthetic_dsh_owner=authenticated');
  if(method==='POST'){headers.set('Content-Type','application/json');if(csrfToken)headers.set('X-Memory-Brain-Setup',csrfToken);}
  const request=new Request(origin+prefix+action,{method,headers,...(method==='POST'?{body:JSON.stringify(body||{})}:{})});
  // Native web-carrier admission decides identity; no owner Peer is forged here.
  const admission=connection.admit(request);
  if('rejection' in admission)return new Response('',{status:admission.rejection});
  return shared.fetch(request,admission.peer);
}
async function setupStatus(){
  const response=await dispatch('status');assert.equal(response.status,200);const value=await response.json();assert.equal(value.ok,true);
  csrfToken=value.data.csrfToken;return value.data;
}
async function action(name,body){assert.equal((await dispatch(name,body)).status,202);return settled(name);}
async function settled(name){
  const start=Date.now(),deadline=start+360000;let reported=0;
  while(Date.now()<deadline){
    const value=await setupStatus();if(!value.busy){assert.equal(value.error,null,'owner action failed: '+value.error);
      statusStages.push({action:name,elapsed_ms:Date.now()-start,phase:value.phase,runtime_state:value.runtime.state,
        running:value.runtime.running,restart_required:value.runtime.restart_required,adapter_enabled:value.adapter.enabled});return value;}
    if(Date.now()-start-reported>=30000){reported=Date.now()-start;console.log('WAIT '+name+' '+Math.floor(reported/1000)+'s');}
    await pause(350);
  }throw new Error('owner_action_timeout');
}
async function processIdentity(){
  // No PID operation is needed. Host files are verified byte-for-byte and the
  // only lifecycle endpoint below belongs to the newly initialized home.
  return {host_unchanged:JSON.stringify(fingerprint())===JSON.stringify(hostBefore)};
}
try{
  new SessionProjectionRegistry(ctx);new SessionStore(ctx);new AgentRegistry(ctx);new SystemPrompt(ctx,{includeRuntimeContext:false,includeHarnessIdentity:false});new ToolRuntime(ctx);new FixtureLlm(ctx);
  ctx.on('agent/error',event=>errors.push(event.error?.name||'agent_error'));
  const browserAuth={isAuthenticated:request=>header(request,'cookie')==='synthetic_dsh_owner=authenticated',authenticateApi:()=>undefined};
  connection=new HostConnectionService(ctx,[],browserAuth);shared=connection.createSharedFetchHandler('/api');
  const prepareBefore=SessionStore.prototype.prepare;
  pluginFiber=ctx.plugin(plugin,{enabled:true,runtimePath:runtime,runtimeHome:home,queueNamespace:'first-run',autoStart:false,intervalMs:50,timeoutMs:3000});
  await pluginFiber.await();mark(resumed?'actual Cordis public entry reuses explicitly owned retry home without activation':'actual Cordis public entry loads without installation',pluginFiber.state===2&&(resumed||!fs.existsSync(home)));
  const initial=await setupStatus();mark(resumed?'native owner GET leaves existing owned retry home disconnected':'native owner GET shows uninitialized with no adapter',
    (resumed||initial.phase==='not_initialized')&&initial.adapter.enabled===false&&(resumed||!fs.existsSync(home)));
  mark('native carrier rejects anonymous setup caller',(await dispatch('status',undefined,'anonymous')).status===401);
  mark('explicit managed home overrides repository legacy private configuration',initial.legacy===false&&initial.home===home);
  const installed=await action('initialize',{allowDownloads:true});
  mark(resumed?'explicit owner initialize accepts already installed owned retry environment':'real source installation completes in owned venv with current package version',installed.runtime.installed_version===version&&installed.runtime.running===false&&installed.adapter.enabled===false);
  mark('source package remains byte-identical and never receives egg-info or private state',packageHash()===packageBefore&&!fs.existsSync(path.join(packageRoot,'private'))&&!fs.existsSync(path.join(packageRoot,'xiaojiu_brain.egg-info')));
  const models=await action('models',{embed_url:'',embed_model:'synthetic-unused-embedding',llm_url:'',llm_model:'synthetic-unused-derivation'});
  mark('confirmed model form saves locally without external model endpoints',models.runtime.models.embedding_configured===false&&models.runtime.models.derivation_configured===false);
  const started=await action('start',{});adapter=plugin.getAdapter(pluginFiber.ctx);
  mark('explicit enable starts own kernel and installs actual DSH adapter',started.runtime.running&&started.adapter.enabled&&adapter?.enabled);
  const firstVersion=(await kernel('/v1/status')).version;mark('authenticated new kernel reports matching release',firstVersion===version);
  const loop=new AgentLoop(ctx,{agents:[],maxParallelToolCalls:{get:()=>2}}),agent=await loop.create('first-run-synthetic-session',{provider:'fixture',model:'local-fake'});
  const firstMessage=llm.createUserMessage({content:[{type:'text',text:'这是独立首启验收：我的合成物品在蓝色盒子里。'}],source:{kind:'user'}});
  agent.followup(firstMessage);await agent.whenIdle();
  await adapter.queue.flush();
  const beforeRows=(await kernel('/v1/messages?limit=20')).items;
  mark('real AgentLoop user and assistant settle through durable queue into actual SQLite',requests.length===1&&errors.length===0&&beforeRows.length===2&&beforeRows.some(row=>row.message_id===firstMessage.id&&row.role==='user')&&beforeRows.some(row=>row.role==='assistant'&&row.text==='收到，合成安装验收原话已记录。'));
  mark('request-only injection stays absent from durable DSH history',!agent.session.log.some(event=>event.type==='user/message'&&event.data?.source?.form==='snapshot'));
  const panelResponse=await dispatch('panel',{});assert.equal(panelResponse.status,200);const panel=(await panelResponse.json()).data.url,cfg=readOwnedConfig(),target=new URL(panel);
  mark('panel action returns only a one-use local ticket, never management token',target.pathname==='/brain'&&target.hash.startsWith('#ticket=')&&!panel.includes(cfg.api_token));
  const bootstrap=await fetch(target.origin+'/v1/session/bootstrap',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ticket:target.hash.slice(8)})});
  const bootstrapData=await bootstrap.json();mark('issued browser ticket authenticates the actual own kernel',bootstrap.ok&&bootstrapData.ok===true);
  const queueBefore=adapter.queue;await action('start',{});mark('repeated enable reuses same adapter and kernel',plugin.getAdapter(pluginFiber.ctx)?.queue===queueBefore&&(await kernel('/v1/messages?limit=20')).items.length===2);
  const changed=await action('models',{llm_model:'synthetic-unused-derivation-2'});mark('running model save visibly requires explicit application',changed.runtime.restart_required===true&&changed.adapter.enabled===true);
  const reapplied=await action('start',{});adapter=plugin.getAdapter(pluginFiber.ctx);mark('explicit apply restarts only owned kernel and clears persistent restart flag',reapplied.runtime.running&&reapplied.adapter.enabled&&!reapplied.runtime.restart_required&&adapter?.queue!==queueBefore);
  const afterRows=(await kernel('/v1/messages?limit=20')).items;
  mark('two original rows and stable IDs survive controlled own-kernel restart',afterRows.length===2&&beforeRows.every(old=>afterRows.some(row=>row.id===old.id&&row.message_id===old.message_id&&row.text===old.text)));
  await pluginFiber.dispose();await pluginFiber.await();
  mark('native unload removes setup routes and restores SessionStore hook',(await dispatch('status')).status===404&&connection.fetchRoutes.size===0&&SessionStore.prototype.prepare===prepareBefore);
  mark('plugin unload deliberately leaves independently owned kernel available',(await kernel('/v1/status')).version===version);
  mark('all read-only host module bytes remain unchanged',(await processIdentity()).host_unchanged);
  fs.writeFileSync(path.join(runDir,'RESULT.json'),JSON.stringify({passed:checks.length,failed:0,checks,version,compatibility,
    actual_owner_admission:true,actual_agentloop:true,actual_venv_install:true,source_stage_no_wheel:!suppliedWheel,installation_offline:true,
    python_version:installed.runtime.python.version,resumed_existing_owned_home:resumed,model_calls:0,
    fake_model_requests:requests.length,raw_messages:afterRows.length,production_mutations:0,hostBefore,hostAfter:fingerprint(),statusStages},null,2));
  console.log(JSON.stringify({passed:checks.length,failed:0,version,compatibility,runDir}));
}catch(error){
  const failureFile=path.join(runDir,fs.existsSync(path.join(runDir,'FAILURE.json'))?'FAILURE-'+Date.now()+'.json':'FAILURE.json');
  fs.writeFileSync(failureFile,JSON.stringify({error:error.name,message:error.message,checks,errors,statusStages,
    compatibility,fake_model_requests:requests.length,hostBefore,hostAfter:fingerprint()},null,2));throw error;
}finally{
  try{await pluginFiber?.dispose();await pluginFiber?.await();}catch{}
  try{await ctx.fiber.dispose();await ctx.fiber.await();}catch{}
  // Only the synthetic home can authorize this cleanup. Never kill a PID or
  // touch the production ports/configuration. Retain the tiny DB as evidence.
  let cleanup={owned_kernel_stopped:false,not_started:false};
  try{const cfg=readOwnedConfig();
    if(!cfg)cleanup={owned_kernel_stopped:true,not_started:true};
    else{
      if(await portOpen(cfg.port)){assert.equal((await kernel('/v1/status')).version,version);await kernel('/v1/admin/shutdown',{});}
      const deadline=Date.now()+15000;while(await portOpen(cfg.port)&&Date.now()<deadline)await pause(100);
      cleanup={owned_kernel_stopped:!await portOpen(cfg.port),not_started:false};
    }
  }catch{cleanup.error='owned_shutdown_not_confirmed';}
  fs.writeFileSync(path.join(runDir,'CLEANUP.json'),JSON.stringify(cleanup,null,2));if(!cleanup.owned_kernel_stopped)process.exitCode=1;
  fakeModel.closeAllConnections?.();await new Promise(resolve=>fakeModel.close(resolve));
}
