/** Actual reviewed DSH methods, local in-memory model, no kernel or API keys.
 * DSH_RUNTIME=<isolated reviewed runtime> node checks/test_request_bridge_admission.mjs
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {pathToFileURL} from 'node:url';
import {createRequestBridge,VERIFIED_LOOP_BUILDS} from '../adapters/dsh/request-bridge.mjs';

const runtime=process.env.DSH_RUNTIME;
if(!runtime||!path.isAbsolute(runtime))throw new Error('DSH_RUNTIME must point to an isolated reviewed runtime.');
const moduleFile=name=>path.join(runtime,'node_modules','@deepseek-ai',name,'lib','index.js');
const digest=bytes=>crypto.createHash('sha256').update(bytes).digest('hex');
const names=['cordis','dsh-session','dsh-session-projection','dsh-agent','dsh-agent-loop','dsh-tools','dsh-system-prompt','dsh-llm'];
const before=Object.fromEntries(names.map(name=>[name,digest(fs.readFileSync(moduleFile(name)))]));
const source=fs.readFileSync(moduleFile('dsh-agent-loop'),'utf8').replace(/^\uFEFF/,'').replace(/\r\n/g,'\n');
const sourceSha256=digest(source),build=VERIFIED_LOOP_BUILDS[sourceSha256];
assert.ok(build,'host complete-source fingerprint is unverified');
const mod=name=>import(pathToFileURL(moduleFile(name)).href);
const [{Context,Service},{SessionStore},{SessionProjectionRegistry},{AgentRegistry},{AgentLoop},{ToolRuntime},{SystemPrompt},llm]=await Promise.all(names.map(mod));
const requests=[],bridges=[],bindings=new WeakMap(),modelGates=new Map(),errors=[];
class FixtureLlm extends Service{
  constructor(ctx){super(ctx,'llm');}
  async prepareCall(config){return {config,stream:request=>this.stream(request)};}
  async *stream(request){
    assert.equal(llm.isAgentLoopRequest(request),true);assert(Object.isFrozen(request)&&Object.isFrozen(request.messages));
    requests.push(request);
    const gate=modelGates.get(request.sessionId);
    if(gate){
      gate.started();
      await new Promise((resolve,reject)=>{
        const abort=()=>reject(request.signal.reason||new Error('aborted'));
        request.signal.addEventListener('abort',abort,{once:true});
        gate.release=()=>{request.signal.removeEventListener('abort',abort);resolve();};
        if(request.signal.aborted)abort();
      });
    }
    yield {type:'block-end',index:0,block:{type:'text',text:'Synthetic local response.'}};
    yield {type:'finish',reason:{kind:'stop'}};
  }
}
const ctx=new Context(),checks=[];
const check=(name,fn)=>{fn();checks.push(name);console.log('PASS '+name);};
const snapshots=request=>request.messages.filter(message=>message.source?.kind==='plugin'&&message.source?.form==='snapshot');
const waitFor=async predicate=>{
  const deadline=Date.now()+5000;
  while(!predicate()){if(Date.now()>deadline)throw new Error('local test deadline exceeded');await new Promise(resolve=>setTimeout(resolve,5));}
};
const user=id=>llm.createUserMessage({id,source:{kind:'user'},content:[{type:'text',text:'Synthetic bridge check '+id}]});
let serial=0;
try{
  for(const [plugin,config] of [[SessionProjectionRegistry],[SessionStore],[AgentRegistry],
    [SystemPrompt,{includeRuntimeContext:false,includeHarnessIdentity:false}],[ToolRuntime],[FixtureLlm]]){
    const fiber=ctx.plugin(plugin,config);await fiber.await();assert.equal(fiber.state,2);
  }
  if(typeof ctx.tools.setExecutor==='function'){
    const fiber=ctx.plugin(await mod('dsh-tool-executor-native'));await fiber.await();assert.equal(fiber.state,2);
  }
  ctx.on('agent/error',payload=>errors.push(String(payload.error?.message||payload.error)));
  ctx.on('agent/pre-step',async(payload,next)=>{
    const decision=await next(),binding=bindings.get(payload.agent);if(!binding)return decision;
    const final={...decision,requestMessages:[binding.marker]};
    binding.lastStage=binding.bridge.stage(payload,final);
    if(binding.cancelAfterStage){binding.cancelAfterStage=false;payload.agent.cancel({kind:'user'});}
    return final;
  });
  const loopFiber=ctx.plugin(AgentLoop,{agents:[]});await loopFiber.await();assert.equal(loopFiber.state,2);
  async function agent(label,options={}){
    const value=await ctx.agentLoop.create('bridge-'+label+'-'+ ++serial,{provider:'fixture',model:'local-memory'});
    const bridge=createRequestBridge({llm,sourceSha256,...options}),binding={bridge,marker:'EVIDENCE_'+label};
    bridges.push(bridge);bindings.set(value,binding);return {value,bridge,binding};
  }
  async function turn(value,id){const start=requests.length;value.followup(user(id));await value.whenIdle();return requests.slice(start);}

  const normal=await agent('normal'),prototype=Object.getPrototypeOf(normal.value);
  const originalBuild=prototype.buildRequest,originalStep=prototype.step;
  const normalRequests=await turn(normal.value,'normal-first');
  check('reviewed actual methods admit exactly one branded request-only snapshot',()=>{
    assert.equal(normal.binding.lastStage.staged,true);assert.equal(normalRequests.length,1);
    assert.equal(snapshots(normalRequests[0]).length,1);assert.equal(snapshots(normalRequests[0])[0].content[0].text,normal.binding.marker);
    assert.equal(JSON.stringify(normal.value.session.log).includes(normal.binding.marker),false);
  });
  normal.bridge.dispose();const disabledRequests=await turn(normal.value,'after-disabled');
  check('disposal restores instance methods and the next real user turn contains no injected snapshot',()=>{
    assert.equal(Object.hasOwn(normal.value,'step'),false);assert.equal(Object.hasOwn(normal.value,'buildRequest'),false);
    assert.equal(disabledRequests.length,1);assert.equal(snapshots(disabledRequests[0]).length,0);
    assert.equal(normal.bridge.status().active,false);
  });

  for(const method of ['step','buildRequest']){
    const changed=await agent('modified-'+method),real=changed.value[method];
    const wrapper=method==='step'?async function(...args){return Reflect.apply(real,this,args);}:
      function(...args){return Reflect.apply(real,this,args);};
    changed.value[method]=wrapper;const observed=await turn(changed.value,'modified-'+method);
    check('an actual instance with an unreviewed '+method+' wrapper is refused without replacing it',()=>{
      assert.equal(changed.binding.lastStage.staged,false);assert.equal(changed.bridge.status().last_error,'unknown_'+method+'_method');
      assert.equal(changed.value[method],wrapper);assert.equal(observed.length,1);assert.equal(snapshots(observed[0]).length,0);
    });
    changed.bridge.dispose();delete changed.value[method];
  }

  const pending=await agent('cancel-pending');pending.binding.cancelAfterStage=true;
  const canceled=await turn(pending.value,'cancel-before-request');
  check('actual cancellation after staging discards pending evidence before any model request',()=>{
    assert.equal(pending.binding.lastStage.staged,true);assert.equal(canceled.length,0);
    assert.equal(pending.bridge.status().pending,0);assert.equal(pending.bridge.status().active_steps,0);
  });
  pending.bridge.dispose();

  const busy=await agent('unload-active');let started=false;
  const gate={started(){started=true;},release(){}};modelGates.set(busy.value.id,gate);
  busy.value.followup(user('busy-user'));
  await waitFor(()=>started&&busy.bridge.status().busy_calls===1);
  busy.bridge.dispose();
  check('unloading during an actual active model step immediately clears bindings and disables the bridge',()=>{
    assert.equal(busy.bridge.status().active,false);assert.equal(busy.bridge.status().pending,0);assert.equal(busy.bridge.status().active_steps,0);
  });
  busy.value.cancel({kind:'user'});gate.release();await busy.value.whenIdle();modelGates.delete(busy.value.id);
  check('cancellation settles the active call and delayed unload restores only owned wrappers',()=>{
    assert.equal(busy.bridge.status().busy_calls,0);assert.equal(Object.hasOwn(busy.value,'step'),false);assert.equal(Object.hasOwn(busy.value,'buildRequest'),false);
  });
  const afterBusy=await turn(busy.value,'after-active-unload');
  check('a new turn after active unload cannot replay evidence from the canceled turn',()=>{
    assert.equal(afterBusy.length,1);assert.equal(snapshots(afterBusy[0]).length,0);
    assert.equal(JSON.stringify(busy.value.session.log).includes(busy.binding.marker),false);
  });
  check('no host errors, prototype edits, or on-disk runtime changes occurred',()=>{
    assert.deepEqual(errors,[]);assert.equal(prototype.buildRequest,originalBuild);assert.equal(prototype.step,originalStep);
    for(const name of names)assert.equal(digest(fs.readFileSync(moduleFile(name))),before[name]);
  });
  console.log(JSON.stringify({passed:checks.length,failed:0,checks,build:build.name,source_sha256:sourceSha256,
    host_mutations:0,network_model_calls:0,model:'in-memory fixture'}));
}finally{
  for(const bridge of bridges)bridge.dispose();
  for(const gate of modelGates.values())gate.release();
  await ctx.fiber.dispose();await ctx.fiber.await();
}
