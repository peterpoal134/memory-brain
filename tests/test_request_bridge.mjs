/** Restricted bridge: real stock method bodies + same-host LLM branding helpers.
 * DSH_STOCK_LOOP_FILE=<verified stock index.js> DSH_LLM_MODULE=<host llm index.js>
 * node tests/test_request_bridge.mjs
 * The root integration separately runs the complete unpatched DSH AgentLoop.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import crypto from 'node:crypto';
import path from 'node:path';
import vm from 'node:vm';
import { pathToFileURL } from 'node:url';
import { createRequestBridge,STOCK_LOOP_SHA256,STOCK_METHOD_SHA256 } from '../adapters/dsh/request-bridge.mjs';

const stockFile=process.env.DSH_STOCK_LOOP_FILE,llmFile=process.env.DSH_LLM_MODULE;
if(!stockFile||!llmFile)throw new Error('Set DSH_STOCK_LOOP_FILE and DSH_LLM_MODULE to read-only, matching DSH modules.');
const raw=fs.readFileSync(stockFile),sha=bytes=>crypto.createHash('sha256').update(bytes).digest('hex');
assert.equal(sha(raw),STOCK_LOOP_SHA256,'unsupported original loop source');
const source=raw.toString('utf8');
const method={
  buildRequest:source.slice(source.indexOf('\tbuildRequest(config, preparedCall, tools, position, startsRequestSeries, signal) {')+1,
    source.indexOf('\n\t}\n};\n//#endregion\n//#region lib/types/constants.js')+'\n\t}'.length),
  step:source.slice(source.indexOf('\tasync step(decision) {')+1,
    source.indexOf('\n\t/** Resolve request config and bind its adapter before admitting model-visible input. */')),
};
const llm=await import(llmFile.startsWith('file:')?llmFile:pathToFileURL(path.resolve(llmFile)).href);
for(const key of ['createUserMessage','markAgentLoopRequest','isAgentLoopRequest'])assert.equal(typeof llm[key],'function');
const deepFreeze=value=>{
  if(value&&typeof value==='object'&&!Object.isFrozen(value)){
    for(const item of Object.values(value))deepFreeze(item);Object.freeze(value);
  }return value;
};
class FixtureLive{
  constructor(){this.ended=false;this.stream={};this.frame=null;}
  start(){}
  push(chunk){this.frame=chunk;}
  get finish(){return this.frame?.finish||{kind:'stop'};}
  blocks(){return this.frame?.blocks||[];}
  settle(_type,operation){this.ended=true;return operation();}
  abandon(){this.ended=true;}
  interruptedBlocks(){return this.blocks();}
}
class FixtureLlmError extends Error{constructor(message,code,failure){super(message);this.code=code;this.failure=failure;}}
const globals={canonicalHeader:input=>({config:input.config,...(input.tools?{tools:input.tools}:{})}),
  headerEquals:(a,b)=>JSON.stringify(a)===JSON.stringify(b),createDeveloperMessage:llm.createDeveloperMessage,
  deepFreeze,markAgentLoopRequest:llm.markAgentLoopRequest,renderPrompt:()=>'',renderContextSections:()=>[],
  assembleContextFor:()=>({}),joinContextSections:()=>'',AssistantStreamAttempt:FixtureLive,LlmError:FixtureLlmError,
  createAssistantMessage:llm.createAssistantMessage,executeToolCalls:async()=>({concluded:false})};
const native=Object.fromEntries(Object.entries(method).map(([name,text])=>{
  const fn=vm.runInNewContext('({'+text+'})',globals)[name];
  assert.equal(sha(Function.prototype.toString.call(fn)),STOCK_METHOD_SHA256[name]);return [name,fn];
}));
let passed=0,failed=0,serial=0;
const check=async(name,fn)=>{try{await fn();passed++;console.log('PASS '+name);}catch(error){failed++;console.error('FAIL '+name+': '+error.stack);}};

function fixture(label='fixture'){
  const events=[],history=[],requests=[],controller=new AbortController();let header,context;
  const agent=Object.create(native);
  Object.assign(agent,{id:label+'-'+(++serial),phase:{kind:'running',turn:1,step:1,abort:controller},
    frozenMessages:new WeakSet(),requestHeaderLogged:false,requestSurfaceGeneration:0,
    assistantAttemptCounter:0,assistantStreamRevision:0,toolsChanged:()=>false,
    loopCtx:{systemPrompt:{refreshContext:()=>({})},llm:{}},runtimeContext:{project:()=>undefined},
    systemPrompt:{project:()=>[]},dispatch:{emit:()=>{},waterfall:async(_event,_payload)=>({kind:'retry'})}});
  agent.session={id:agent.id,surface:{contentGeneration:1},requestHeader:()=>header,requestContext:()=>context,
    toolHistory:()=>[],deriveMessages:()=>[...history],append(type,data){
      events.push({type,data});if(type==='request/header')header=data.header;if(type==='request/context')context=data;
      if(type==='user/message')history.push(data);if(type==='assistant/message')history.push(data.message);
      return {seq:events.length};}};
  agent.prepareRequest=async()=>({config:{provider:'synthetic',model:'synthetic',temperature:.2},preparedCall:{stream(request){
    requests.push(request);
    return (async function*(){if(agent.gate)await agent.gate;if(agent.error)throw agent.error;
      yield {finish:agent.frames?.shift()||{kind:'stop'},blocks:[{type:'text',text:'Synthetic visible final.'}]};})();
  }}});
  const payload=()=>({agent,turn:agent.phase.turn,step:agent.phase.step,signal:agent.phase.abort.signal});
  const decision=blocks=>({kind:'enter',messages:[],assembly:{tools:[]},requestMessages:blocks});
  const build=(position={turn:agent.phase.turn,step:agent.phase.step},signal=agent.phase.abort.signal)=>
    agent.buildRequest({provider:'synthetic',model:'synthetic',temperature:.2},undefined,[],position,false,signal);
  return {agent,events,history,requests,controller,payload,decision,build};
}
const blocks=request=>request.messages.filter(message=>message.source?.kind==='plugin'&&message.source?.form==='snapshot')
  .map(message=>message.content[0].text);

await check('actual stock fingerprints are exact and installation changes only two instance descriptors',async()=>{
  const f=fixture(),bridge=createRequestBridge({llm});
  assert.equal(bridge.stage(f.payload(),f.decision(['evidence'])).staged,true);
  assert.equal(Object.getOwnPropertyDescriptor(f.agent,'step').enumerable,false);
  assert.notEqual(f.agent.step,native.step);assert.notEqual(f.agent.buildRequest,native.buildRequest);
  assert.equal(Object.getPrototypeOf(f.agent).step,native.step);assert.equal(Object.getPrototypeOf(f.agent).buildRequest,native.buildRequest);
  bridge.dispose();assert.equal(Object.hasOwn(f.agent,'step'),false);assert.equal(Object.hasOwn(f.agent,'buildRequest'),false);
});
await check('step consumes the final outer decision, retaining inner blocks but not stale early blocks',async()=>{
  const f=fixture(),bridge=createRequestBridge({llm});
  bridge.stage(f.payload(),f.decision(['EARLY']));
  await f.agent.step(f.decision(['INNER_EXISTING','OUTER_FINAL','OUTER_FINAL','',12]));
  assert.deepEqual(blocks(f.requests[0]),['INNER_EXISTING','OUTER_FINAL','OUTER_FINAL']);
  assert(!JSON.stringify(f.events).includes('INNER_EXISTING'));assert(!JSON.stringify(f.events).includes('OUTER_FINAL'));
  bridge.dispose();
});
await check('an empty early handler can still carry blocks added by the final outer middleware',async()=>{
  const f=fixture(),bridge=createRequestBridge({llm});bridge.stage(f.payload(),f.decision([]));
  await f.agent.step(f.decision(['LATE_ADDED']));assert.deepEqual(blocks(f.requests[0]),['LATE_ADDED']);bridge.dispose();
});
await check('outer clearing and rejection never replay pending evidence',async()=>{
  const f=fixture(),bridge=createRequestBridge({llm});bridge.stage(f.payload(),f.decision(['EARLY']));
  await f.agent.step(f.decision([]));assert.deepEqual(blocks(f.requests[0]),[]);
  bridge.stage(f.payload(),f.decision(['REJECTED']));
  // A final rejected decision makes native turn() skip step() entirely.
  assert.deepEqual(blocks(f.build()),[]);
  bridge.clear(f.agent);assert.equal(bridge.status().pending,0);
  bridge.dispose();
});
await check('an earlier reject is not a gate when final outer decision admits the step',async()=>{
  const f=fixture(),bridge=createRequestBridge({llm});
  assert.equal(bridge.stage(f.payload(),{kind:'reject'}).staged,true);
  await f.agent.step(f.decision(['FINAL_ENTER']));assert.deepEqual(blocks(f.requests[0]),['FINAL_ENTER']);bridge.dispose();
});
await check('same-step prepared-call retry receives evidence each time without accumulation',async()=>{
  const f=fixture(),bridge=createRequestBridge({llm});
  f.agent.frames=[{kind:'error',failure:{message:'Synthetic retry',code:'fixture'}},{kind:'stop'}];
  bridge.stage(f.payload(),f.decision(['RETRY_EVIDENCE']));await f.agent.step(f.decision(['RETRY_EVIDENCE']));
  assert.equal(f.requests.length,2);for(const request of f.requests)assert.deepEqual(blocks(request),['RETRY_EVIDENCE']);
  assert.notEqual(f.requests[0].messages.at(-1),f.requests[1].messages.at(-1));
  assert.equal(bridge.status().active_steps,0);assert.deepEqual(blocks(f.build()),[]);bridge.dispose();
});
await check('requests stay frozen and host branded while config tools history and signal remain native',async()=>{
  const f=fixture(),bridge=createRequestBridge({llm}),tools=[{name:'fixture_tool',parameters:{type:'object'}}];
  const d=f.decision(['SNAPSHOT']);d.assembly.tools=tools;
  bridge.stage(f.payload(),d);await f.agent.step(d);const request=f.requests[0];
  assert.equal(llm.isAgentLoopRequest(request),true);assert(Object.isFrozen(request));assert(Object.isFrozen(request.messages));
  assert.equal(request.tools,tools);assert.equal(request.signal,f.controller.signal);assert.equal(request.temperature,.2);
  assert(Object.isFrozen(request.messages.at(-1)));assert.equal(request.messages.at(-1).role,'user');
  assert.equal(f.history.some(message=>JSON.stringify(message).includes('SNAPSHOT')),false);bridge.dispose();
});
await check('new tool step uses its own final blocks and an empty next step cannot replay them',async()=>{
  const f=fixture(),bridge=createRequestBridge({llm});bridge.stage(f.payload(),f.decision(['STEP_ONE']));await f.agent.step(f.decision(['STEP_ONE']));
  f.agent.phase.step=2;bridge.stage(f.payload(),f.decision(['STEP_TWO']));await f.agent.step(f.decision(['STEP_TWO']));
  f.agent.phase.step=3;bridge.stage(f.payload(),f.decision([]));await f.agent.step(f.decision([]));
  assert.deepEqual(f.requests.map(blocks),[['STEP_ONE'],['STEP_TWO'],[]]);bridge.dispose();
});
await check('concurrent agents are isolated even when they have identical turn and step numbers',async()=>{
  const a=fixture('A'),b=fixture('B'),bridge=createRequestBridge({llm});let release;
  const gate=new Promise(resolve=>{release=resolve;});a.agent.gate=gate;b.agent.gate=gate;
  bridge.stage(a.payload(),a.decision(['A_PRIVATE']));bridge.stage(b.payload(),b.decision(['B_PRIVATE']));
  const p=a.agent.step(a.decision(['A_PRIVATE'])),q=b.agent.step(b.decision(['B_PRIVATE']));
  await new Promise(resolve=>setTimeout(resolve,0));assert.equal(bridge.status().active_steps,2);release();await Promise.all([p,q]);
  assert.deepEqual(blocks(a.requests[0]),['A_PRIVATE']);assert.deepEqual(blocks(b.requests[0]),['B_PRIVATE']);
  assert(!JSON.stringify(bridge.status()).includes('PRIVATE'));bridge.dispose();
});
await check('equal evidence text in separate sessions does not share mutable requests or message IDs',async()=>{
  const a=fixture(),b=fixture(),bridge=createRequestBridge({llm});
  for(const f of [a,b])bridge.stage(f.payload(),f.decision(['SAME_TEXT']));
  await Promise.all([a.agent.step(a.decision(['SAME_TEXT'])),b.agent.step(b.decision(['SAME_TEXT']))]);
  assert.notEqual(a.requests[0],b.requests[0]);assert.notEqual(a.requests[0].messages.at(-1).id,b.requests[0].messages.at(-1).id);
  bridge.dispose();
});
await check('stage during a real awaiting native step permanently disables this instance bridge',async()=>{
  const f=fixture(),bridge=createRequestBridge({llm});let release;
  f.agent.gate=new Promise(resolve=>{release=resolve;});
  bridge.stage(f.payload(),f.decision(['FIRST_ALREADY_SENT']));
  const first=f.agent.step(f.decision(['FIRST_ALREADY_SENT']));
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.deepEqual(blocks(f.requests[0]),['FIRST_ALREADY_SENT']);
  assert.equal(bridge.stage(f.payload(),f.decision(['REENTRANT_MUST_NOT_LEAK'])).staged,false);
  const second=f.agent.step(f.decision(['REENTRANT_MUST_NOT_LEAK']));
  await new Promise(resolve=>setTimeout(resolve,0));assert.deepEqual(blocks(f.requests[1]),[]);
  release();await Promise.all([first,second]);
  assert.equal(bridge.status().active_steps,0);assert.equal(bridge.status().busy_calls,0);
  assert.equal(bridge.stage(f.payload(),f.decision(['STILL_DISABLED'])).staged,false);
  f.agent.gate=null;await f.agent.step(f.decision(['STILL_DISABLED']));assert.deepEqual(blocks(f.requests[2]),[]);bridge.dispose();
});
await check('direct same-instance step reentry cannot borrow another invocation binding',async()=>{
  const f=fixture(),bridge=createRequestBridge({llm});let releaseFirst,releaseSecond;
  f.agent.gate=new Promise(resolve=>{releaseFirst=resolve;});bridge.stage(f.payload(),f.decision(['FIRST']));
  const first=f.agent.step(f.decision(['FIRST']));await new Promise(resolve=>setTimeout(resolve,0));
  f.agent.gate=new Promise(resolve=>{releaseSecond=resolve;});
  const second=f.agent.step(f.decision(['SECOND_NO_STAGE']));await new Promise(resolve=>setTimeout(resolve,0));
  assert.deepEqual(blocks(f.requests[1]),[]);releaseFirst();await first;
  assert.equal(bridge.status().busy_calls,1);assert.equal(bridge.status().active_steps,0);
  releaseSecond();await second;
  assert.equal(bridge.status().active_steps,0);assert(bridge.status().reentrant_calls>0);bridge.dispose();
});
await check('abort clears pending binding and preserves the native abort error',async()=>{
  const f=fixture(),bridge=createRequestBridge({llm}),reason=new Error('Synthetic abort');
  bridge.stage(f.payload(),f.decision(['CANCELED']));f.controller.abort(reason);
  await assert.rejects(()=>f.agent.step(f.decision(['CANCELED'])),error=>error===reason);
  assert.equal(f.requests.length,0);assert.equal(bridge.status().pending,0);assert.equal(bridge.status().active_steps,0);bridge.dispose();
});
await check('different run position or signal clears binding rather than replaying it later',async()=>{
  for(const mismatch of ['position','signal']){
    const f=fixture(),bridge=createRequestBridge({llm}),prepare=f.agent.prepareRequest;
    f.agent.prepareRequest=async()=>{
      const wrong=mismatch==='position'?{turn:1,step:2}:{turn:1,step:1};
      const signal=mismatch==='signal'?new AbortController().signal:f.controller.signal;
      assert.deepEqual(blocks(f.build(wrong,signal)),[]);return prepare();};
    bridge.stage(f.payload(),f.decision(['MUST_NOT_REPLAY']));await f.agent.step(f.decision(['MUST_NOT_REPLAY']));
    assert.deepEqual(blocks(f.requests[0]),[]);bridge.dispose();
  }
});
await check('native stream exceptions are unchanged and helper failures fail open without stale replay',async()=>{
  const f=fixture(),bridge=createRequestBridge({llm}),error=new Error('Synthetic native error');f.agent.error=error;
  bridge.stage(f.payload(),f.decision(['ERROR_BLOCK']));await assert.rejects(()=>f.agent.step(f.decision(['ERROR_BLOCK'])),value=>value===error);
  assert.equal(bridge.status().active_steps,0);bridge.dispose();
  const g=fixture(),broken=createRequestBridge({llm:{...llm,createUserMessage(){throw new Error('Synthetic helper failure');}}});
  broken.stage(g.payload(),g.decision(['FAIL_OPEN']));await g.agent.step(g.decision(['FAIL_OPEN']));
  assert.deepEqual(blocks(g.requests[0]),[]);assert.equal(llm.isAgentLoopRequest(g.requests[0]),true);broken.dispose();
});
await check('unknown wrappers, native hook, frozen agents and missing helpers are rejected safely',async()=>{
  const bridge=createRequestBridge({llm}),a=fixture();a.agent.step=async function unknown(){};
  assert.equal(bridge.stage(a.payload(),a.decision(['x'])).staged,false);assert.equal(Object.hasOwn(a.agent,'buildRequest'),false);
  const b=fixture();b.agent.withRequestMessages=()=>{};assert.equal(bridge.stage(b.payload(),b.decision(['x'])).staged,false);
  const c=fixture();Object.preventExtensions(c.agent);assert.equal(bridge.stage(c.payload(),c.decision(['x'])).staged,false);
  const missing=createRequestBridge();assert.equal(missing.status().active,false);assert.equal(missing.stage(b.payload(),b.decision(['x'])).staged,false);
  bridge.dispose();missing.dispose();
});
await check('clear and dispose restore only owned descriptors including existing own flags',async()=>{
  const f=fixture(),bridge=createRequestBridge({llm});
  Object.defineProperty(f.agent,'buildRequest',{value:native.buildRequest,configurable:false,writable:true,enumerable:true});
  const before=Object.getOwnPropertyDescriptor(f.agent,'buildRequest');bridge.stage(f.payload(),f.decision(['x']));
  assert.equal(bridge.clear(f.agent),true);assert.deepEqual(Object.getOwnPropertyDescriptor(f.agent,'buildRequest'),before);
  assert.equal(Object.hasOwn(f.agent,'step'),false);bridge.dispose();bridge.dispose();assert.equal(bridge.status().active,false);
});
await check('other plugins later wrappers survive unload and captured bridge wrappers become transparent',async()=>{
  const f=fixture(),bridge=createRequestBridge({llm});bridge.stage(f.payload(),f.decision(['STALE']));
  const ourStep=f.agent.step,ourBuild=f.agent.buildRequest;
  const outerStep=async function(...args){return Reflect.apply(ourStep,this,args);};
  const outerBuild=function(...args){return Reflect.apply(ourBuild,this,args);};
  f.agent.step=outerStep;f.agent.buildRequest=outerBuild;bridge.dispose();
  assert.equal(f.agent.step,outerStep);assert.equal(f.agent.buildRequest,outerBuild);
  await f.agent.step(f.decision(['STALE']));assert.deepEqual(blocks(f.requests[0]),[]);
});
await check('real source bytes and native prototype remain unchanged',async()=>{
  assert.equal(sha(fs.readFileSync(stockFile)),STOCK_LOOP_SHA256);
  for(const name of ['step','buildRequest'])assert.equal(sha(Function.prototype.toString.call(native[name])),STOCK_METHOD_SHA256[name]);
});
console.log(JSON.stringify({passed,failed,production_mutations:0,real_model_calls:0,host_method_bodies:'verified_stock_source'}));
if(failed)process.exitCode=1;
