/** Restricted in-process compatibility for exact verified DSH source builds.
 *
 * This is not an official requestMessages hook. The public entry must first
 * verify the selected complete source hash; both methods are fingerprinted
 * here. No prototype, host file, ASAR, durable session or LLM route is patched.
 */
import crypto from 'node:crypto';
import { ContextBudgetError, CONTEXT_BUDGET_ERROR_CODE } from './request-budget.mjs';

export const STOCK_LOOP_SHA256='cf57e3ecd487ac5c2553455aaa5cef2c6fb41e6bd61be5aa455675d1d7193033';
export const STOCK_METHOD_SHA256=Object.freeze({
  buildRequest:'7833829fbcb6fbcfef27c2d771e3adbdcdecf89a3ef7837f71185fad08128f42',
  step:'1a17029cf820c00e61f9c29b6ae5170e34044e4d4b877137e1e5b3d9cbac6dfa',
});
// Complete source hashes select a separately verified pair of method bodies.
// A version label never grants compatibility, and unknown hashes stay closed.
export const VERIFIED_LOOP_BUILDS=Object.freeze({
  [STOCK_LOOP_SHA256]:Object.freeze({name:'official_021_stock',methods:STOCK_METHOD_SHA256}),
  a6fa8e0c7c90191caf5f4a3bb5c624a86e9845cf3c79d020b71420d0e20ae4c1:Object.freeze({
    name:'official_022_rc1',methods:Object.freeze({
      buildRequest:'7833829fbcb6fbcfef27c2d771e3adbdcdecf89a3ef7837f71185fad08128f42',
      step:'3897f0b4f560d26ba21f640e29d1d549cf7094877d9c411178146b33d2e242b7',
    }),
  }),
  a8b456b07ad59a77b65be20702cb07fb202e646f7783c5f0bbd3f8d3d48d4937:Object.freeze({
    name:'dsha_build190_reconstructed',methods:Object.freeze({
      buildRequest:'7833829fbcb6fbcfef27c2d771e3adbdcdecf89a3ef7837f71185fad08128f42',
      step:'499c2f197a8cc77ebf8a11255f54e2cdd214c2a9e5f0305db9ddee1fa6a45b0f',
    }),
  }),
});
// Known requestMessages patch from compat/dsh-request-messages.json. The LF
// form is derived from those exact declared replacements, not another host.
export const NATIVE_LOOP_SHA256='05da9a39de1e0109861423eb3617a9cdcfb7fb8cc822feb455337c1f333098f8';
export const NATIVE_CANONICAL_LOOP_SHA256='5d5f9bfe53bc4551887f242d05e0dcf79fdb3d459ef6bffde347eab650974834';
export const NATIVE_METHOD_SHA256=Object.freeze({
  buildRequest:'7833829fbcb6fbcfef27c2d771e3adbdcdecf89a3ef7837f71185fad08128f42',
  step:'ebfb323a213c385823bbb457ae2c186aaea65f8c4df53dd6c4311265793c42dc',
  withRequestMessages:'958fcced2e853dc12d46c76d725e83cc695cd9087595b575da20658a16957f24',
});
const NATIVE_BUILD=Object.freeze({name:'verified_021_native_request_messages',methods:NATIVE_METHOD_SHA256});
const VERIFIED_NATIVE_BUILDS=Object.freeze({[NATIVE_LOOP_SHA256]:NATIVE_BUILD,[NATIVE_CANONICAL_LOOP_SHA256]:NATIVE_BUILD});
// Only line-ending representation is normalized. Spaces, comments and every
// other method byte remain part of the exact verified-build fingerprint.
const hash=fn=>crypto.createHash('sha256').update(Function.prototype.toString.call(fn).replace(/\r\n/g,'\n')).digest('hex');
const object=value=>value!==null&&(typeof value==='object'||typeof value==='function');
const position=value=>value&&Number.isInteger(value.turn)&&value.turn>=0&&Number.isInteger(value.step)&&value.step>=0;
function methodDescriptor(agent,name){
  for(let current=agent;current;current=Object.getPrototypeOf(current)){
    const descriptor=Object.getOwnPropertyDescriptor(current,name);
    if(descriptor)return descriptor;
  }
  return null;
}
function ownsDescriptor(agent,name,installed){
  const current=Object.getOwnPropertyDescriptor(agent,name);
  return current&&'value'in current&&current.value===installed.value&&
    current.configurable===installed.configurable&&current.enumerable===installed.enumerable&&current.writable===installed.writable;
}

export function createRequestBridge(options={}){
  return createBoundRequestBridge({...options,nativeBudget:false});
}
export function createNativeRequestBudgetBridge(options={}){
  return createBoundRequestBridge({...options,nativeBudget:true});
}
function createBoundRequestBridge({llm,sourceSha256=STOCK_LOOP_SHA256,requestBudget=null,nativeBudget=false}={}){
  if(requestBudget!==null&&typeof requestBudget?.check!=='function')throw new TypeError('request_budget_helper_invalid');
  const builds=nativeBudget?VERIFIED_NATIVE_BUILDS:VERIFIED_LOOP_BUILDS;
  const build=Object.hasOwn(builds,sourceSha256)?builds[sourceSha256]:null;
  const records=new WeakMap(),agents=new Set();
  const create=llm?.createUserMessage,mark=llm?.markAgentLoopRequest,isBranded=llm?.isAgentLoopRequest;
  const hasBudget=Boolean(requestBudget)&&typeof requestBudget.check==='function';
  const helpers=Boolean(build)&&typeof mark==='function'&&(nativeBudget?hasBudget:typeof create==='function');
  const methodNames=nativeBudget?['step','buildRequest','withRequestMessages']:['step','buildRequest'];
  let disposed=false,lastError=!build?'host_loop_build_unverified':helpers?null:'host_llm_helpers_missing';
  const counters={stages:0,installed:0,injections:0,refused:0,cleared:0,restored:0,
    external_wrappers_preserved:0,aborted:0,fail_open:0,original_errors:0,reentrant_calls:0,
    budget_checks:0,budget_blocked:0,native_requests:0};
  const report=code=>{
    lastError=code;
    if(hasBudget&&code!=='request_budget_exceeded'){
      try{requestBudget.unavailable?.(code);}catch{/* Diagnostic consumers never change bridge admission. */}
    }
  };
  if(!helpers&&hasBudget)report(lastError);
  const release=binding=>{if(binding?.cleanup){binding.cleanup();binding.cleanup=null;}};
  const clearBindings=record=>{
    const pending=record.pending,active=record.active;
    record.pending=null;record.active=null;
    release(pending);if(active?.ticket!==pending)release(active?.ticket);
  };
  const disableReentrant=record=>{
    record.enabled=false;clearBindings(record);counters.reentrant_calls++;report('reentrant_instance_call');
  };
  function phaseMatches(agent,binding){
    const phase=agent.phase;
    return phase?.kind==='running'&&phase.turn===binding.turn&&phase.step===binding.step&&
      phase.abort?.signal===binding.signal&&binding.signal?.aborted===false;
  }
  function restore(agent,record){
    clearBindings(record);record.enabled=false;
    for(const name of methodNames){
      if(ownsDescriptor(agent,name,record.installed[name])){
        try{
          const old=record.originalOwn[name];
          if(old)Object.defineProperty(agent,name,old);else delete agent[name];
          counters.restored++;
        }catch{report('instance_method_restore_failed');}
      }else counters.external_wrappers_preserved++;
    }
    records.delete(agent);agents.delete(record.ref);
  }
  function ensure(agent){
    let record=records.get(agent);if(record)return record;
    if(!object(agent)){report('agent_missing');return null;}
    const native=methodDescriptor(agent,'withRequestMessages');
    if(!nativeBudget&&native&&typeof native.value==='function'){report('native_snapshot_present');return null;}
    if(nativeBudget&&(!native||typeof native.value!=='function')){report('native_snapshot_missing');return null;}
    const original={},originalOwn={},installed={};
    for(const name of methodNames){
      const descriptor=methodDescriptor(agent,name),own=Object.getOwnPropertyDescriptor(agent,name);
      if(!descriptor||!('value'in descriptor)||typeof descriptor.value!=='function'||hash(descriptor.value)!==build.methods[name]){
        report('unknown_'+name+'_method');return null;
      }
      if(own&&(!('value'in own)||(!own.configurable&&!own.writable))||!own&&!Object.isExtensible(agent)){
        report('instance_method_not_writable');return null;
      }
      original[name]=descriptor.value;originalOwn[name]=own;
    }
    record={ref:new WeakRef(agent),original,originalOwn,installed,pending:null,active:null,enabled:true,
      busy:0,generation:0,restoreAfterBusy:false};
    const buildWrapper=function(...args){
      let request;
      try{request=Reflect.apply(original.buildRequest,this,args);}
      catch(error){clearBindings(record);counters.original_errors++;throw error;}
      const active=record.active;
      if(disposed||!record.enabled)return request;
      if(!active){if(hasBudget)report('request_budget_binding_unavailable');return request;}
      try{
        if(record.ref.deref()!==this||!position(args[3])||args[3].turn!==active.turn||args[3].step!==active.step||
          args[5]!==active.signal||!phaseMatches(this,active)){
          clearBindings(record);report('request_binding_mismatch');return request;
        }
        if(!Object.isFrozen(request)||!Array.isArray(request.messages)||!Object.isFrozen(request.messages)||
          request.signal!==active.signal||(typeof isBranded==='function'&&!isBranded(request))){
          clearBindings(record);report('host_request_shape_or_brand_mismatch');return request;
        }
        if(nativeBudget){
          active.preparedCall=args[1];active.builtRequest=request;
          return request;
        }
        const transient=active.blocks.map(text=>create({content:[{type:'text',text}],source:{kind:'plugin',form:'snapshot'}}));
        const next=transient.length?mark(Object.freeze({...request,messages:Object.freeze([...request.messages,...transient])})):request;
        if(next===null||typeof next!=='object'||!Object.isFrozen(next)||
          (typeof isBranded==='function'&&!isBranded(next)))throw new Error('invalid host brand');
        if(hasBudget){
          counters.budget_checks++;
          if(requestBudget.check(next,{preparedCall:args[1],agent:this})!==next)throw new Error('invalid request-budget result');
        }
        if(transient.length)counters.injections++;
        return next;
      }catch(error){
        if(error instanceof ContextBudgetError||error?.code===CONTEXT_BUDGET_ERROR_CODE){
          clearBindings(record);counters.budget_blocked++;report('request_budget_exceeded');throw error;
        }
        clearBindings(record);counters.fail_open++;report('request_snapshot_unavailable');return request;
      }
    };
    const nativeMessagesWrapper=function(...args){
      let next;
      // Preserve the real native helper's arguments, result and exceptions.
      // In particular, this bridge does not inject a second snapshot.
      try{next=Reflect.apply(original.withRequestMessages,this,args);}
      catch(error){clearBindings(record);counters.original_errors++;throw error;}
      const active=record.active;
      if(disposed||!record.enabled)return next;
      if(!active){report('request_budget_binding_unavailable');return next;}
      try{
        if(record.ref.deref()!==this||!phaseMatches(this,active)||args[0]!==active.builtRequest||
          !Object.isFrozen(next)||!Array.isArray(next.messages)||!Object.isFrozen(next.messages)||
          next.signal!==active.signal||(typeof isBranded==='function'&&!isBranded(next))){
          clearBindings(record);report('native_request_binding_mismatch');return next;
        }
        counters.budget_checks++;
        if(requestBudget.check(next,{preparedCall:active.preparedCall,agent:this})!==next)throw new Error('invalid request-budget result');
        counters.native_requests++;return next;
      }catch(error){
        if(error instanceof ContextBudgetError||error?.code===CONTEXT_BUDGET_ERROR_CODE){
          clearBindings(record);counters.budget_blocked++;report('request_budget_exceeded');throw error;
        }
        clearBindings(record);counters.fail_open++;report('request_snapshot_unavailable');return next;
      }
    };
    const stepWrapper=async function(decision,...rest){
      const generation=++record.generation;
      if(record.busy>0)disableReentrant(record);
      record.busy++;
      const ticket=record.pending;record.pending=null;
      let active=null;
      if(!disposed&&record.enabled&&ticket){
        try{
          if(record.ref.deref()===this&&decision?.kind==='enter'&&phaseMatches(this,ticket)){
            // Read the actual final waterfall decision here, never the earlier
            // handler's requestMessages. Outer removal/replacement is respected.
            const blocks=Array.isArray(decision.requestMessages)?decision.requestMessages.filter(value=>typeof value==='string'&&value.trim()):[];
            if(blocks.length||hasBudget)active={turn:ticket.turn,step:ticket.step,signal:ticket.signal,blocks,ticket,generation};
          }
        }catch{counters.fail_open++;report('final_decision_unavailable');}
      }
      // Reentrant calls are transparent and must never replace an outer call's
      // binding. disableReentrant has already discarded every staged fragment.
      if(record.enabled)record.active=active;
      try{return await Reflect.apply(original.step,this,[decision,...rest]);}
      catch(error){counters.original_errors++;throw error;}
      finally{
        if(record.active?.generation===generation)record.active=null;
        release(ticket);
        record.busy--;
        if(record.busy===0&&record.restoreAfterBusy){
          const owner=record.ref.deref();if(owner)restore(owner,record);
        }
      }
    };
    try{
      const wrappers=[['buildRequest',buildWrapper],['step',stepWrapper],...(nativeBudget?[['withRequestMessages',nativeMessagesWrapper]]:[])];
      for(const [name,value] of wrappers){
        installed[name]=originalOwn[name]?{...originalOwn[name],value}:{value,writable:true,configurable:true,enumerable:false};
        Object.defineProperty(agent,name,installed[name]);
      }
    }catch{
      for(const name of methodNames)if(installed[name]&&ownsDescriptor(agent,name,installed[name])){
        try{if(originalOwn[name])Object.defineProperty(agent,name,originalOwn[name]);else delete agent[name];}catch{}
      }
      record.enabled=false;report('instance_method_install_failed');return null;
    }
    records.set(agent,record);agents.add(record.ref);counters.installed++;return record;
  }
  function stage(payload,decision){
    if(disposed||!helpers)return {staged:false,reason:disposed?'disposed':lastError};
    const agent=payload?.agent;
    if(!object(agent)){counters.refused++;report('agent_missing');return {staged:false,reason:lastError};}
    const previous=records.get(agent);
    if(previous?.busy>0){disableReentrant(previous);counters.refused++;return {staged:false,reason:'reentrant_instance_call'};}
    if(previous){clearBindings(previous);if(!previous.enabled){counters.refused++;return {staged:false,reason:'instance_bridge_disabled'};}}
    // An outer waterfall handler can turn an earlier reject into enter. Only
    // the eventual step(decision) is authoritative about admission or blocks.
    if(!position(payload)||payload.signal?.aborted!==false||
      typeof payload.signal.addEventListener!=='function'||typeof payload.signal.removeEventListener!=='function'){
      counters.cleared++;return {staged:false,reason:'step_not_admitted'};
    }
    let record;
    try{record=ensure(agent);}catch{report('instance_inspection_failed');}
    if(!record){counters.refused++;return {staged:false,reason:lastError};}
    const binding={turn:payload.turn,step:payload.step,signal:payload.signal,cleanup:null};
    const abort=()=>{
      if(record.pending===binding)record.pending=null;
      if(record.active?.ticket===binding)record.active=null;
      counters.aborted++;release(binding);
    };
    binding.cleanup=()=>binding.signal.removeEventListener('abort',abort);
    try{binding.signal.addEventListener('abort',abort,{once:true});}
    catch{clearBindings(record);counters.refused++;report('abort_binding_unavailable');return {staged:false,reason:lastError};}
    record.pending=binding;counters.stages++;
    if(binding.signal.aborted)abort();
    return {staged:record.pending===binding,reason:record.pending===binding?'bound_to_final_step':'aborted'};
  }
  function clear(agent){
    const record=object(agent)?records.get(agent):null;
    if(!record)return false;
    if(record.busy>0){record.enabled=false;clearBindings(record);record.restoreAfterBusy=true;}
    else restore(agent,record);
    counters.cleared++;return true;
  }
  function dispose(){
    if(disposed)return;disposed=true;
    for(const ref of [...agents]){
      const agent=ref.deref();if(agent){const record=records.get(agent);if(record){
        if(record.busy>0){record.enabled=false;clearBindings(record);record.restoreAfterBusy=true;}
        else restore(agent,record);
      }}else agents.delete(ref);
    }
  }
  function status(){
    let instances=0,pending=0,activeSteps=0,busyCalls=0;
    for(const ref of [...agents]){
      const agent=ref.deref();if(!agent){agents.delete(ref);continue;}
      const record=records.get(agent);if(!record)continue;
      instances++;if(record.pending)pending++;if(record.active)activeSteps++;busyCalls+=record.busy;
    }
    return {mode:nativeBudget?(helpers?'verified_native_budget':'native_budget_unavailable'):'scoped_agent_bridge',
      active:!disposed&&helpers,instances,pending,active_steps:activeSteps,busy_calls:busyCalls,
      ...counters,last_error:lastError,
      ...(hasBudget&&typeof requestBudget.status==='function'?{request_budget:requestBudget.status()}:{} )};
  }
  return Object.freeze({stage,clear,dispose,status});
}
