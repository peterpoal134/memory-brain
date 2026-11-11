/** Public DSH entry: load local-only configuration, check the host, start the
 * independent kernel if requested, then install the existing evidence adapter.
 * No host patches, personal presets or credentials are shipped in this module.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createRequire } from 'node:module';
import { createAdapter, getAdapter } from './xiaojiu-brain.mjs';
import { createRequestBridge, createNativeRequestBudgetBridge } from './request-bridge.mjs';
import { createRequestBudget } from './request-budget.mjs';
import { createContextSettings, CONTEXT_DEFAULTS } from './context-settings.mjs';
import { createCompactionSupport } from './compaction-support.mjs';
import { probeRuntime } from './host-compat.mjs';
import { discoverHostRuntime } from './runtime-discovery.mjs';
import { createRuntimeManager, resolveRuntimeHome } from './runtime-home.mjs';
import { createOnboarding, mountOnboarding } from './onboarding.mjs';

export const name = 'memory-brain';
export const inject = [];
export { getAdapter };
const execute = promisify(execFile);
const packageRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const onboarding = new WeakMap();
const CONTEXT_FIELDS=Object.keys(CONTEXT_DEFAULTS);
function contextSettings(configFile,config){
  return createContextSettings({file:path.join(path.dirname(configFile),'context-management.json'),
    defaults:Object.fromEntries(CONTEXT_FIELDS.filter(key=>config[key]!==undefined).map(key=>[key,config[key]]))});
}
export const getOnboarding = ctx => onboarding.get(ctx);

export function discoverRuntime({allowUnverified=false}={}) {
  return discoverHostRuntime({allowUnverified,probeRuntime,inspectRuntime,pluginUrl:import.meta.url});
}

export function inspectRuntime(runtimePath) {
  const result=probeRuntime(runtimePath);
  if(!result.supported)throw new Error('当前DSH构建尚未完成核验（dsh_request_messages_hook_required）；请打开插件配置复制环境诊断。本会话采集和召回未接入。');
  return result;
}

export function resolveConfiguration(config = {}) {
  if (config.enabled !== true) return { enabled: false };
  if (config.requestBridge !== undefined) throw new Error('request_bridge_is_owned_by_the_verified_public_entry');
  if (config.requestBudget !== undefined) throw new Error('request_budget_is_owned_by_the_verified_public_entry');
  if (config.compactionSupport !== undefined) throw new Error('compaction_support_is_owned_by_the_verified_public_entry');
  if (Number(process.versions.node.split('.')[0]) < 22) throw new Error('node_22_or_later_required');
  if (config.token !== undefined || config.baseUrl !== undefined) throw new Error('use_local_brain_config_instead_of_inline_token_or_url');
  const configFile = path.resolve(config.brainConfigFile || path.join(packageRoot, 'private', 'config.json'));
  const local = JSON.parse(fs.readFileSync(configFile, 'utf8').replace(/^\uFEFF/, ''));
  if (!['127.0.0.1', 'localhost', '::1'].includes(local.host) || !Number.isInteger(local.port) || local.port < 1024 || local.port > 65535) throw new Error('local_brain_address_invalid');
  if (typeof local.api_token !== 'string' || local.api_token.length < 24) throw new Error('local_brain_token_invalid');
  const runtime = config.runtimePath ? inspectRuntime(config.runtimePath) : discoverRuntime();
  const data = path.resolve(path.dirname(configFile), local.data_dir || './data');
  const namespace = config.queueNamespace || 'default';
  if (!/^[a-zA-Z0-9_-]{1,80}$/.test(namespace)) throw new Error('queue_namespace_invalid');
  const contextPolicy=contextSettings(configFile,config),policy=contextPolicy.read();
  const options = { ...config, ...policy, ...runtime, configFile, packageRoot, contextPolicySignature:contextPolicy.signature(policy),
    baseUrl: `http://${local.host === '::1' ? '[::1]' : local.host}:${local.port}`, token: local.api_token,
    queueDir: path.join(data, 'dsh-queue', namespace) };
  delete options.runtime;
  return options;
}

async function ready(options) {
  try {
    const configured = Number(options.readinessTimeoutMs ?? 1500);
    const readinessTimeoutMs = Number.isFinite(configured) ? Math.max(500, Math.min(30000, Math.floor(configured))) : 1500;
    const response = await fetch(options.baseUrl + '/v1/status', { redirect: 'error', signal: AbortSignal.timeout(readinessTimeoutMs),
      headers: { Authorization: `Bearer ${options.token}` } });
    const value = await response.json();
    return response.ok && value.ok === true && typeof value.data?.version === 'string' && typeof value.data?.build_id === 'string';
  } catch { return false; }
}

async function attach(ctx,options,ensureReady,isDisposed) {
  // The legacy bridge is per-agent, version/fingerprint gated and process local.
  // It never edits the host bundle, prototype or durable message history.
  const budget=createRequestBudget({enabled:options.contextEnabled!==false,maxInputTokens:options.contextMaxInputTokens,
    safetyTokens:options.contextSafetyTokens,onWarning:code=>ctx.logger?.warn?.('memory-brain: '+String(code).replace(/[^a-zA-Z0-9_:.-]/g,''))});
  options.requestBudget=budget;
  const llm=await import(options.llmModule);
  const bridge = options.injectionMode === 'scoped_agent_bridge' ?
    createRequestBridge({llm,sourceSha256:options.compatibility.loop_canonical_sha256,requestBudget:budget}) :
    options.injectionMode==='native_snapshot'&&options.contextEnabled!==false?
      createNativeRequestBudgetBridge({llm,sourceSha256:options.compatibility.loop_sha256,requestBudget:budget}):null;
  if (bridge) options.requestBridge = bridge;
  let support;
  try {
  if (!await ready(options)) await ensureReady();
  if (isDisposed()) throw new Error('setup_closed');
  if (!await ready(options)) throw new Error('memory_brain_readiness_not_confirmed');
  support = createCompactionSupport({ ctx, enabled: options.contextEnabled !== false,
    summaryEnabled: options.contextSummaryEnabled, summaryMaxChars: options.contextSummaryMaxChars,
    summaryMaxTokens: options.contextSummaryMaxTokens, pressureCooldownMs: options.contextPressureCooldownMs,
    pressureThresholdRatio: options.contextPressureThresholdRatio, pressureRetainRatio: options.contextPressureRetainRatio,
    pressureHeadroomTokens: options.contextPressureHeadroomTokens,
    toolPruneEnabled: options.contextToolPruneEnabled, toolPruneThresholdChars: options.contextToolPruneThresholdChars,
    toolPruneHeadChars: options.contextToolPruneHeadChars, toolPruneTailChars: options.contextToolPruneTailChars,
    basicCompactionModule: options.basicCompactionModule, llmModule: options.llmModule,
    cordisModule: options.cordisModule, toolResultPrunerModule: options.toolResultPrunerModule, tokenMeterModule: options.tokenMeterModule,
    onWarning: code => ctx.logger?.warn?.('memory-brain: ' + String(code).replace(/[^a-zA-Z0-9_:.-]/g,'')) });
  options.compactionSupport = support;
  await support.ready;
  if (isDisposed()) throw new Error('setup_closed');
  const controller = await createAdapter(ctx, options);
  controller.contextPolicySignature=options.contextPolicySignature;
  if (isDisposed()) { await controller.stop(); throw new Error('setup_closed'); }
  if (!controller.enabled) throw new Error('memory_brain_adapter_not_enabled_see_dsh_log');
  // Unloading removes the adapter only. The kernel may serve another host or
  // an open panel, so it is never killed or purged by a DSH plugin disposer.
  return controller;
  } catch (error) { await support?.stop(); bridge?.dispose(); budget.stop(); throw error; }
}

function profileIdentity(ctx,config){
  const context=ctx.get?.('profileContext');
  if(typeof context?.dir==='string'&&path.isAbsolute(context.dir)){
    const normalized=path.resolve(context.dir).replace(/\\/g,'/');
    return 'profile:'+(process.platform==='win32'?normalized.toLowerCase():normalized);
  }
  const flag=process.argv.indexOf('--profile');
  const named=typeof context?.name==='string'?context.name:flag>=0?process.argv[flag+1]:null;
  return 'profile:'+(named||config.queueNamespace||'default');
}
function runningAgent(ctx){
  const registry=ctx.get?.('agents');
  if(!registry||typeof registry.list!=='function')throw new Error('agent_inventory_unavailable');
  try{const agents=registry.list();if(!Array.isArray(agents))throw new Error();return agents.some(agent=>agent?.status==='running');}
  catch{throw new Error('agent_inventory_unavailable');}
}
function mountSetup(ctx,setup){
  onboarding.set(ctx,setup);
  ctx.inject?.(['connection'],scoped=>{
    const connection=scoped.get?.('connection')||scoped.connection;
    if(typeof connection?.fetch?.register!=='function'){
      setup.setError?.({code:'dsh_setup_connection_routes_required'});
      console.warn('memory-brain: dsh_setup_connection_routes_required; no anonymous setup endpoint was created');
      return;
    }
    return mountOnboarding(scoped,setup);
  });
}
function existingManager(options){
  return {
    async status(){
      const local=JSON.parse(fs.readFileSync(options.configFile,'utf8').replace(/^\uFEFF/,''));
      const up=await ready(options);
      return {state:'existing_local',installed_version:'existing',python:{available:true},running:up,
        models:{embedding_configured:Boolean(local.embed_url),derivation_configured:Boolean(local.llm_url&&local.llm_model)}};
    },
    async initialize(){throw new Error('existing_local_installation_preserved');},
    async saveModels(){throw new Error('configure_existing_installation_in_its_original_directory');},
    async panelUrl(){
      const response=await fetch(options.baseUrl+'/v1/session/ticket',{method:'POST',headers:{Authorization:'Bearer '+options.token,'Content-Type':'application/json'},
        body:'{}',redirect:'error',signal:AbortSignal.timeout(2000)});
      const value=await response.json();
      if(!response.ok||value.ok!==true||typeof value.data?.ticket!=='string')throw new Error('panel_ticket_unavailable');
      return options.baseUrl+'/brain#ticket='+value.data.ticket;
    },
    async dispose(){}
  };
}

export async function apply(ctx,config={}){
  if(config.enabled!==true)return;
  if(Number(process.versions.node.split('.')[0])<22)throw new Error('node_22_or_later_required');
  if(config.requestBridge!==undefined)throw new Error('request_bridge_is_owned_by_the_verified_public_entry');
  if(config.requestBudget!==undefined)throw new Error('request_budget_is_owned_by_the_verified_public_entry');
  if(config.compactionSupport!==undefined)throw new Error('compaction_support_is_owned_by_the_verified_public_entry');
  if(config.token!==undefined||config.baseUrl!==undefined)throw new Error('use_local_brain_config_instead_of_inline_token_or_url');
  if(config.queueNamespace!==undefined&&!/^[a-zA-Z0-9_-]{1,80}$/.test(config.queueNamespace))throw new Error('queue_namespace_invalid');
  const runtime=config.runtimePath?probeRuntime(config.runtimePath):discoverRuntime({allowUnverified:true});
  let disposed=false,controller=null,activation=null,setup;
  const isDisposed=()=>disposed;
  const cleanup=async()=>{if(disposed)return;disposed=true;await setup?.dispose();};
  ctx.effect?.(()=>cleanup);
  if(!runtime.supported){
    // Unknown packaged/mobile builds remain readable for diagnostics. No
    // configuration, Python installation, memory store or adapter is opened.
    const manager={async status(){return {state:'compatibility_required',models:{}};},async dispose(){}};
    setup=createOnboarding({manager,compatibility:runtime.compatibility,getAdapterStatus:()=>null});
    mountSetup(ctx,setup);
    console.warn('memory-brain: compatibility_required '+JSON.stringify(runtime.compatibility));
    return;
  }
  const legacy=config.brainConfigFile!==undefined||config.runtimeHome===undefined&&fs.existsSync(path.join(packageRoot,'private','config.json'));
  if(legacy){
    // An existing explicit/link install keeps its exact data, credentials and
    // startup contract. Never copy it into a new, empty managed installation.
    const options=resolveConfiguration(config),manager=existingManager(options),policy=contextSettings(options.configFile,config);
    const start=()=>{
      if(activation)return activation;
      activation=(async()=>{
      const latest=resolveConfiguration(config);
      if(controller?.enabled&&controller.contextPolicySignature===latest.contextPolicySignature)return controller;
      if(controller?.enabled){
        if(runningAgent(ctx))throw new Error('finish_current_chat_before_applying_context');
        await controller.stop();controller=null;
      }
      return attach(ctx,latest,async()=>{
        if(config.autoStart===false)throw new Error('memory_brain_not_ready_start_the_local_launcher');
        if(process.platform!=='win32')throw new Error('automatic_start_windows_only_start_kernel_manually');
        if(path.resolve(options.configFile).toLowerCase()!==path.join(packageRoot,'private','config.json').toLowerCase())
          throw new Error('custom_config_requires_manual_kernel_start');
        try{await execute('powershell.exe',['-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden',
          '-File',path.join(packageRoot,'scripts','start-local.ps1'),'-NoOpen'],{cwd:packageRoot,windowsHide:true,timeout:30000,maxBuffer:65536});}
        catch{throw new Error('memory_brain_start_failed_see_local_service_log');}
      },isDisposed);
      })().then(value=>controller=value).finally(()=>activation=null);
      return activation;
    };
    setup=createOnboarding({manager,onStart:start,home:path.dirname(path.dirname(options.configFile)),
      compatibility:runtime.compatibility,legacy:true,getAdapterStatus:()=>controller,
      getContextSettings:()=>({settings:policy.read(),restart_required:Boolean(controller?.enabled&&controller.contextPolicySignature!==policy.signature())}),
      saveContextSettings:body=>policy.save(body)});
    mountSetup(ctx,setup);
    // Keep this installation's setup scope alive after a temporary backend
    // outage. It owns neither the external kernel nor its startup contract.
    try { await start(); }
    catch (error) {
      if (disposed) return;
      setup.setError?.(error);
      const suppliedInterval = Number(config.reconnectIntervalMs ?? 5000);
      const retryMs = Number.isFinite(suppliedInterval) ? Math.max(1000, Math.min(60000, Math.floor(suppliedInterval))) : 5000;
      const suppliedAttempts = Number(config.reconnectAttempts ?? 12);
      const maxAttempts = Number.isFinite(suppliedAttempts) ? Math.max(0, Math.min(60, Math.floor(suppliedAttempts))) : 12;
      let attempts = 0;
      if (maxAttempts > 0) {
        const timer = setInterval(() => {
          if (disposed || controller?.enabled || attempts >= maxAttempts) { clearInterval(timer); return; }
          if (activation) return;
          attempts++;
          start().then(() => { setup.setError?.(null); clearInterval(timer); })
            .catch(failure => { if (!disposed) setup.setError?.(failure); });
        }, retryMs);
        timer.unref?.();
        ctx.effect?.(() => () => clearInterval(timer));
      }
    }
    return;
  }
  // GitHub/npm installs keep code replaceable and all mutable state in one
  // stable per-user/profile directory. Loading never calls initialize().
  const home=await resolveRuntimeHome({packageRoot,runtimeHome:config.runtimeHome,profile:profileIdentity(ctx,config)});
  const manager=createRuntimeManager({packageRoot,home});
  const policy=contextSettings(path.join(home,'private','config.json'),config);
  const start=()=>{
    if(activation)return activation;
    activation=(async()=>{
      const state=await manager.status();
      if(disposed)throw new Error('setup_closed');
      const policyChanged=Boolean(controller?.enabled&&controller.contextPolicySignature!==policy.signature());
      if(controller?.enabled&&state.running&&!state.restart_required&&!policyChanged)return controller;
      if(policyChanged){
        if(runningAgent(ctx))throw new Error('finish_current_chat_before_applying_context');
        await controller.stop();controller=null;
      }
      if(state.restart_required&&state.running){
        if(runningAgent(ctx))throw new Error('finish_current_chat_before_applying_models');
        await controller?.stop();controller=null;
        if(runningAgent(ctx))throw new Error('finish_current_chat_before_applying_models');
        await manager.stop();
      }else if(controller?.enabled&&!state.running){await controller.stop();controller=null;}
      await manager.start();
      if(disposed)throw new Error('setup_closed');
      const connection=await manager.connectionOptions();
      const options=resolveConfiguration({...config,runtimePath:runtime.runtime,brainConfigFile:connection.configFile});
      return attach(ctx,options,()=>manager.start(),isDisposed);
    })().then(value=>controller=value).finally(()=>activation=null);
    return activation;
  };
  setup=createOnboarding({manager,onStart:start,home,compatibility:runtime.compatibility,getAdapterStatus:()=>controller,
    getContextSettings:()=>({settings:policy.read(),restart_required:Boolean(controller?.enabled&&controller.contextPolicySignature!==policy.signature())}),
    saveContextSettings:body=>{if(!fs.existsSync(path.join(home,'private','config.json')))throw new Error('runtime_not_initialized');return policy.save(body);}});
  mountSetup(ctx,setup);
  const state=await manager.status();
  if(state.installed_version===state.expected_version&&state.state!=='error'&&config.autoStart!==false){
    // No install/upgrade occurs here. Failed auto-start stays visible in the
    // setup card and can be retried explicitly, rather than losing the card.
    try{await start();}catch(error){setup.setError?.(error);}
  }
}
