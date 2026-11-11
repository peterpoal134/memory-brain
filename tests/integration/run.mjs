/** DSH_RUNTIME=<installation> node tests/integration/run.mjs */
import fs from 'node:fs';
import path from 'node:path';
import http from 'node:http';
import crypto from 'node:crypto';
import { spawn } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';
import assert from 'node:assert/strict';
const brainPlugin = await import(process.env.MEMORY_BRAIN_ENTRY ? pathToFileURL(path.resolve(process.env.MEMORY_BRAIN_ENTRY)).href : '../../adapters/dsh/xiaojiu-brain.mjs');
const here = path.dirname(fileURLToPath(import.meta.url));
const runtime = process.env.DSH_RUNTIME;
if (!runtime) throw new Error('Set DSH_RUNTIME to an installed DSH runtime (read-only).');
const runtimeFiles=['dsh-agent-loop','dsh-session','dsh-llm','dsh-tools'].map(n=>path.join(runtime,'node_modules','@deepseek-ai',n,'lib','index.js'));
const fingerprint=()=>Object.fromEntries(runtimeFiles.map(f=>[path.relative(runtime,f),crypto.createHash('sha256').update(fs.readFileSync(f)).digest('hex')]));
const runtimeBefore=fingerprint();
const mod = name => import(pathToFileURL(path.join(runtime, 'node_modules', '@deepseek-ai', name, 'lib', 'index.js')).href);
const [{ Context, Service }, { SessionStore }, { SessionProjectionRegistry }, { AgentRegistry }, { AgentLoop }, { ToolRuntime }, { SystemPrompt }, llm] = await Promise.all(['cordis','dsh-session','dsh-session-projection','dsh-agent','dsh-agent-loop','dsh-tools','dsh-system-prompt','dsh-llm'].map(mod));
const artifactRoot=path.resolve(here,'../../artifacts/agentloop-acceptance');
fs.mkdirSync(artifactRoot,{recursive:true});
const runDir = fs.mkdtempSync(path.join(artifactRoot, 'run-'));
const token = crypto.randomBytes(32).toString('hex'), checks = [], requests = [], errors = [], warnings = [];
const check = (name, predicate) => { assert(predicate, name); checks.push(name); console.log('PASS ' + name); };
const python = spawn(process.env.PYTHON || 'python', ['-I',path.join(here, 'brain_fixture.py'), path.join(runDir, 'data')], { cwd: path.resolve(here, '../..'), env: { ...process.env, LOOP_TEST_TOKEN: token, PYTHONUTF8:'1' }, windowsHide:true, stdio:['ignore','pipe','pipe'] });
let pythonError = ''; python.stderr.on('data', b => { pythonError += b; });
const port = await new Promise((resolve, reject) => { let buffer=''; const t=setTimeout(()=>reject(new Error('Brain startup: '+pythonError)),10000); python.stdout.on('data', b=> { buffer+=b; if(buffer.includes('\n')){clearTimeout(t);resolve(JSON.parse(buffer.split('\n')[0]).port);} }); python.on('exit',code=>reject(new Error('Brain exited '+code+': '+pythonError))); });
const baseUrl='http://127.0.0.1:'+port;
const queueDir=process.env.MEMORY_BRAIN_ENTRY ? path.join(runDir,'data','dsh-queue','integration') : path.join(runDir,'queue');
const localConfig=path.join(runDir,'synthetic-config.json');
fs.writeFileSync(localConfig,JSON.stringify({host:'127.0.0.1',port,api_token:token,data_dir:'./data'}));
const call = async (endpoint, body, method=body?'POST':'GET') => { const r=await fetch(baseUrl+endpoint,{method,headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});const j=await r.json();assert.equal(j.ok,true,JSON.stringify(j));return j.data; };
let modelCall=0;
const fakeModel=http.createServer(async(req,res)=>{let text='';for await(const b of req)text+=b;const wire=JSON.parse(text);requests.push(wire);fs.writeFileSync(path.join(runDir,'request-'+requests.length+'.json'),JSON.stringify(wire,null,2));modelCall++;
  const toolStep=modelCall===2||modelCall===4||modelCall===6;
  const blocks=toolStep?[{type:'tool-call',id:'fixture-tool-'+modelCall,name:'fixture_ping',arguments:'{}'}]:[{type:'reasoning',text:'合成推理不可入库'},{type:'text',text:modelCall===1?'我答应保留这个约定。':'家长会在周日。'}];
  res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify({chunks:[...blocks.map((block,index)=>({type:'block-end',index,block})),{type:'finish',reason:{kind:toolStep?'tool-calls':'stop'}}]}));});
await new Promise(r=>fakeModel.listen(0,'127.0.0.1',r));
const modelUrl='http://127.0.0.1:'+fakeModel.address().port;
class FixtureLlm extends Service { constructor(ctx){super(ctx,'llm');} async prepareCall(config){return {config,stream:request=>this.stream(request)};} async *stream(request){assert(llm.isAgentLoopRequest(request),'actual request retains the host private request brand');assert(Object.isFrozen(request)&&Object.isFrozen(request.messages),'actual request snapshot remains immutable');const {signal,...wire}=request;const response=await fetch(modelUrl,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(wire),signal});for(const chunk of (await response.json()).chunks)yield chunk;} }
const ctx=new Context();
let adapter, pluginFiber;
try {
  // Service classes may own asynchronous native initialization. Constructing
  // them directly bypasses that lifecycle on newer hosts.
  for(const [service,config] of [[SessionProjectionRegistry],[SessionStore],[AgentRegistry],
    [SystemPrompt,{includeRuntimeContext:false,includeHarnessIdentity:false}],[ToolRuntime],[FixtureLlm]]){
    const serviceFiber=ctx.plugin(service,config);await serviceFiber.await();assert.equal(serviceFiber.state,2,'native service class did not initialize');
  }
  // Newer ToolRuntime exposes explicit executor selection; use the host's real
  // native executor. Older runtimes retain their own built-in executor.
  if(typeof ctx.tools.setExecutor==='function'){
    const executorFiber=ctx.plugin(await mod('dsh-tool-executor-native'));await executorFiber.await();assert.equal(executorFiber.state,2,'real native executor did not initialize');
  }
  ctx.on('agent/error',e=>errors.push(String(e.error?.stack||e.error||e)));
  // Registered outside/before the memory middleware: its final decision must
  // win even when it changes requestMessages after the memory handler returns.
  ctx.on('agent/pre-step',async(payload,next)=>{
    const decision=await next();
    const text=(payload.messages||[]).flatMap(m=>m.content||[]).filter(b=>b.type==='text').map(b=>b.text).join('\n');
    if(text==='SYNTHETIC_OUTER_OVERRIDE')return {...decision,requestMessages:['OUTER_FINAL_SNAPSHOT']};
    if(text==='SYNTHETIC_OUTER_CLEAR')return {...decision,requestMessages:[]};
    return decision;
  });
  const prepareBefore=SessionStore.prototype.prepare;
  const pluginOptions=process.env.MEMORY_BRAIN_ENTRY ? {enabled:true,runtimePath:runtime,brainConfigFile:localConfig,queueNamespace:'integration',autoStart:false,intervalMs:50,timeoutMs:3000} :
    {enabled:true,sessionModule:path.join(runtime,'node_modules','@deepseek-ai','dsh-session','lib','index.js'),baseUrl,token,queueDir,intervalMs:50,timeoutMs:3000};
  pluginFiber=ctx.plugin(brainPlugin,pluginOptions);
  await pluginFiber.await();
  adapter=brainPlugin.getAdapter(pluginFiber.ctx);
  check('real Cordis module loading reaches active state without invalid effect',pluginFiber.state===2&&adapter?.enabled===true);
  ctx.tools.register({name:'fixture_ping',description:'Synthetic no-op tool',parameters:{type:'object',properties:{},required:[],additionalProperties:false},output:{schema:{type:'string'},render:(_args,value)=>[{type:'text',text:value}]},execute:async()=> '合成工具输出不可入库'});
  const loopFiber=ctx.plugin(AgentLoop,{agents:[],maxParallelToolCalls:2});await loopFiber.await();assert.equal(loopFiber.state,2,'native AgentLoop did not initialize');
  const loop=ctx.agentLoop;
  const agent=await loop.create('isolated-loop-session',{provider:'fixture',model:'local-fake'});
  const fact=llm.createUserMessage({id:'fact-1',content:[{type:'text',text:'家长会时间确定为周日，请记住。'}],source:{kind:'user'}});
  agent.followup(fact);await agent.whenIdle();
  check('real loop completed historical turn',requests.length===1&&errors.length===0);
  const question=llm.createUserMessage({id:'question-1',content:[{type:'text',text:'家长会是什么时间？'}],source:{kind:'user'}});
  agent.followup(question);await agent.whenIdle();
  check('real loop performed model tool round and final',requests.length===3&&agent.session.log.some(e=>e.type==='tool/result')&&errors.length===0);
  const incomingBlocks=requests.slice(1).map(r=>r.messages.filter(m=>m.source?.kind==='plugin'&&m.source?.form==='snapshot'));
  check('actual first question request contains memory evidence',incomingBlocks[0].length===1&&JSON.stringify(incomingBlocks[0]).includes('周日'));
  check('actual injected evidence excludes the current question',!JSON.stringify(incomingBlocks[0]).includes('家长会是什么时间？'));
  check('tool followup retains exactly one equal evidence block',incomingBlocks[1].length===1&&incomingBlocks[1][0].content[0].text===incomingBlocks[0][0].content[0].text);
  const block=incomingBlocks[0][0].content[0].text;
  const log=agent.session.log;
  check('request-only evidence never enters durable session log',!JSON.stringify(log).includes(block));
  await adapter.queue.flush();
  const recalled=await call('/v1/recall',{conversation_id:agent.id,query:'家长会是什么时间？',current_message_id:question.id,token_budget:1200});
  const all=await call('/v1/messages?limit=100');const questionRow=all.items.find(m=>m.message_id===question.id);
  check('current question is stored once from persistent inbox then visible event',all.items.filter(m=>m.message_id===question.id).length===1&&Boolean(questionRow));
  check('current question is excluded from retrieval evidence',!recalled.evidence.some(e=>e.source_refs.some(r=>r.message_id===questionRow.id)));
  check('raw history retains real assistant promise',all.items.some(m=>m.role==='assistant'&&m.text==='我答应保留这个约定。'));
  check('reasoning, tool output and injection do not enter raw storage',!all.items.some(m=>m.text.includes('合成推理')||m.text.includes('合成工具输出')||m.text===block));
  await adapter.queue.flush();
  const before=all.items.length;const first=all.items.find(m=>m.message_id===fact.id);
  const replay=await call('/v1/ingest',{conversation_id:first.conversation_id,message_id:first.message_id,role:first.role,text:first.text,recorded_at:first.recorded_at,metadata:first.metadata});
  check('replay reaches explicit duplicate without extra raw row',replay.duplicate===true&&(await call('/v1/messages?limit=100')).items.length===before);
  check('captured user order is historical fact before current question',all.items.filter(m=>m.role==='user').sort((a,b)=>a.id-b.id).map(m=>m.message_id).join(',')===[fact.id,question.id].join(','));
  const canceled=llm.createUserMessage({content:[{type:'text',text:'尚未认领，随后取消的合成输入'}],source:{kind:'user'}});
  agent.send(canceled,'next-turn',false);
  await new Promise(r=>setTimeout(r,100));
  check('durable but unclaimed pending input is not archived',!(await call('/v1/messages?limit=100')).items.some(m=>m.message_id===canceled.id));
  agent.cancel({kind:'user'});await adapter.queue.flush();
  check('canceled inbox removal cannot masquerade as a claimed user message',!(await call('/v1/messages?limit=100')).items.some(m=>m.message_id===canceled.id));
  // Life must reach an actual DSH model request even while a concurrent real
  // backend recall outlives the adapter's three-second deadline.
  await call('/v1/settings',{life_enabled:true,life_context_enabled:true},'PATCH');
  const timingPath=path.join(runDir,'fixture-timing.jsonl');
  const timingBefore=fs.readFileSync(timingPath,'utf8').trim().split('\n').filter(Boolean).length;
  fs.writeFileSync(path.join(runDir,'recall-delay.json'),JSON.stringify({seconds:4}));
  const lifeQuestion=llm.createUserMessage({id:'life-timeout-question',content:[{type:'text',text:'这是一条普通合成聊天，请正常继续。'}],source:{kind:'user'}});
  const lifeStarted=Date.now();agent.followup(lifeQuestion);await agent.whenIdle();
  fs.unlinkSync(path.join(runDir,'recall-delay.json'));
  const lifeRequests=requests.slice(3),lifeBlocks=lifeRequests.map(r=>r.messages.filter(m=>m.source?.kind==='plugin'&&m.source?.form==='snapshot'));
  check('real DSH life turn completes despite recall timeout',lifeRequests.length===2&&errors.length===0&&adapter.warnings.some(w=>w.code==='recall_timeout')&&Date.now()-lifeStarted<6000);
  check('actual model request contains four bounded rule signals once',lifeBlocks.every(blocks=>blocks.length===1&&['精力','无聊','社交','好奇'].every(label=>blocks[0].content[0].text.includes(label))));
  const lifeBlock=lifeBlocks[0][0].content[0].text;
  check('actual tool followup reuses the equal life state without duplication',lifeBlocks[1][0].content[0].text===lifeBlock);
  check('actual request life state never becomes a durable session message',!JSON.stringify(agent.session.log).includes(lifeBlock));
  const timed=fs.readFileSync(timingPath,'utf8').trim().split('\n').filter(Boolean).slice(timingBefore).map(line=>JSON.parse(line));
  const lifeEnd=timed.find(e=>e.stage==='life_context_finished'),slowStart=timed.find(e=>e.stage==='slow_recall_started'),slowEnd=timed.find(e=>e.stage==='slow_recall_finished');
  check('real fast life endpoint completes before blocked retrieval and within its own budget',Boolean(lifeEnd&&slowStart)&&lifeEnd.at*1000-lifeStarted<800&&(!slowEnd||lifeEnd.at<slowEnd.at));
  check('same real user tool series fetches the life endpoint only once',timed.filter(e=>e.stage==='life_context_finished').length===1);
  await call('/v1/settings',{life_enabled:false,recall_enabled:false,self_notes_enabled:true},'PATCH');
  await call('/v1/self',{style_preferences:'SYNTHETIC_USE_BRIEF_STYLE'},'PATCH');
  await call('/v1/standards',{enabled:true,add:{id:'own-rule',text:'SYNTHETIC_EXPLAIN_UNCERTAINTY',scope:'conversation',conversation_id:agent.id,priority:80}},'PATCH');
  await call('/v1/standards',{add:{id:'other-rule',text:'OTHER_CONVERSATION_RULE_MUST_NOT_APPEAR',scope:'conversation',conversation_id:'other'}},'PATCH');
  const beforeProfileRequests=requests.length;
  const beforeProfileTiming=fs.readFileSync(timingPath,'utf8').trim().split('\n').filter(Boolean).length;
  agent.followup(llm.createUserMessage({id:'profile-independent-question',content:[{type:'text',text:'普通合成对话，验证角色通道。'}],source:{kind:'user'}}));await agent.whenIdle();
  const profileRequests=requests.slice(beforeProfileRequests),profileBlocks=profileRequests.map(r=>r.messages.filter(m=>m.source?.kind==='plugin'&&m.source?.form==='snapshot'));
  check('real DSH receives owner profile even when factual retrieval is disabled',profileRequests.length===2&&profileBlocks.every(bs=>bs.length===1&&JSON.stringify(bs).includes('SYNTHETIC_USE_BRIEF_STYLE')&&JSON.stringify(bs).includes('SYNTHETIC_EXPLAIN_UNCERTAINTY')));
  check('other-conversation rule is absent from actual model requests',!JSON.stringify(profileBlocks).includes('OTHER_CONVERSATION_RULE_MUST_NOT_APPEAR'));
  check('profile snapshot is equal across real tool followup and never persisted',profileBlocks[0][0].content[0].text===profileBlocks[1][0].content[0].text&&!JSON.stringify(agent.session.log).includes(profileBlocks[0][0].content[0].text));
  const profileTimed=fs.readFileSync(timingPath,'utf8').trim().split('\n').filter(Boolean).slice(beforeProfileTiming).map(line=>JSON.parse(line));
  check('one real profile turn fetches only one independent context snapshot',profileTimed.filter(e=>e.stage==='profile_context_finished').length===1&&adapter.status().profile_context.included===true);
  const beforeOverride=requests.length;
  agent.followup(llm.createUserMessage({id:'outer-override',content:[{type:'text',text:'SYNTHETIC_OUTER_OVERRIDE'}],source:{kind:'user'}}));await agent.whenIdle();
  const overridden=requests.slice(beforeOverride).flatMap(r=>r.messages.filter(m=>m.source?.kind==='plugin'&&m.source?.form==='snapshot'));
  check('outer middleware final override wins over earlier memory decision',overridden.length===1&&overridden[0].content[0].text==='OUTER_FINAL_SNAPSHOT');
  const beforeClear=requests.length;
  agent.followup(llm.createUserMessage({id:'outer-clear',content:[{type:'text',text:'SYNTHETIC_OUTER_CLEAR'}],source:{kind:'user'}}));await agent.whenIdle();
  check('outer middleware final empty decision removes all earlier snapshots',requests.length===beforeClear+1&&!requests.at(-1).messages.some(m=>m.source?.kind==='plugin'&&m.source?.form==='snapshot'));
  const otherAgent=await loop.create('isolated-concurrent-B',{provider:'fixture',model:'local-fake'});
  await call('/v1/standards',{add:{id:'bridge-only-a',text:'ONLY_SYNTHETIC_AGENT_A',scope:'conversation',conversation_id:agent.id,priority:100}},'PATCH');
  await call('/v1/standards',{add:{id:'bridge-only-b',text:'ONLY_SYNTHETIC_AGENT_B',scope:'conversation',conversation_id:otherAgent.id,priority:100}},'PATCH');
  const beforeConcurrent=requests.length;
  agent.followup(llm.createUserMessage({id:'concurrent-a',content:[{type:'text',text:'合成并发A'}],source:{kind:'user'}}));
  otherAgent.followup(llm.createUserMessage({id:'concurrent-b',content:[{type:'text',text:'合成并发B'}],source:{kind:'user'}}));
  await Promise.all([agent.whenIdle(),otherAgent.whenIdle()]);
  const concurrent=requests.slice(beforeConcurrent),aRequest=concurrent.find(r=>r.sessionId===agent.id),bRequest=concurrent.find(r=>r.sessionId===otherAgent.id);
  const snapshots=r=>JSON.stringify(r?.messages.filter(m=>m.source?.kind==='plugin'&&m.source?.form==='snapshot'));
  check('concurrent real agents never borrow each other scoped rule',concurrent.length===2&&snapshots(aRequest).includes('ONLY_SYNTHETIC_AGENT_A')&&!snapshots(aRequest).includes('ONLY_SYNTHETIC_AGENT_B')&&snapshots(bRequest).includes('ONLY_SYNTHETIC_AGENT_B')&&!snapshots(bRequest).includes('ONLY_SYNTHETIC_AGENT_A'));
  check('override, concurrent, and profile snapshots remain absent from all durable histories',![agent,otherAgent].some(a=>['OUTER_FINAL_SNAPSHOT','ONLY_SYNTHETIC_AGENT_A','ONLY_SYNTHETIC_AGENT_B'].some(t=>JSON.stringify(a.session.log).includes(t))));
  check('all real requests completed without host errors',errors.length===0);
  check('existing DSH runtime module bytes are unchanged',JSON.stringify(runtimeBefore)===JSON.stringify(fingerprint()));
  const finalAdapterStatus=adapter.status();
  await pluginFiber.dispose();await pluginFiber.await();
  check('real Cordis unload awaits queue cleanup and restores SessionStore hook',adapter.enabled===false&&SessionStore.prototype.prepare===prepareBefore&&!fs.existsSync(path.join(queueDir,'writer.lock')));
  check('unload leaves no owned per-agent request or step wrappers',![agent,otherAgent].some(a=>Object.hasOwn(a,'buildRequest')||Object.hasOwn(a,'step')));
  fs.writeFileSync(path.join(runDir,'session-events.json'),JSON.stringify(log,null,2));
  fs.writeFileSync(path.join(runDir,'result.json'),JSON.stringify({passed:checks.length,failed:0,checks,requests:requests.length,raw_messages:all.items.length,errors,warnings:adapter.warnings,adapter_status:finalAdapterStatus,runtimeBefore,runtimeAfter:fingerprint(),runDir,production_mutations:0,real_model_calls:0},null,2));
  console.log(JSON.stringify({passed:checks.length,failed:0,runDir}));
} catch(e){fs.writeFileSync(path.join(runDir,'failure.json'),JSON.stringify({error:e.stack,errors,requests:requests.length,pythonError},null,2));throw e;}
finally {await adapter?.stop();try{await ctx.fiber.dispose();await ctx.fiber.await();}catch{}fakeModel.closeAllConnections();await new Promise(r=>fakeModel.close(r));python.kill();}
