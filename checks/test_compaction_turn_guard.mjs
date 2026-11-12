/** Synthetic public-plugin regression; real host pruner + released-V4 validator.
 * DSH_RUNTIME must name an existing compatible runtime; no download or host edit.
 */
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import {fileURLToPath,pathToFileURL} from 'node:url';
const here=path.dirname(fileURLToPath(import.meta.url)),root=path.resolve(here,'..');
const runtime=process.env.DSH_RUNTIME;
if(!runtime||!path.isAbsolute(runtime))throw Error('DSH_RUNTIME_absolute_existing_path_required');
const host=name=>pathToFileURL(path.join(runtime,'node_modules/@deepseek-ai',name,'lib/index.js')).href;
const [{Context,Service},{Session,KNOWN_SESSION_EVENT_TYPES},llm,format,{serializeSessionLog},basic,pruner]=await Promise.all([
 import(host('cordis')),import(host('dsh-session')),import(host('dsh-llm')),import(host('dsh-session-format-v3-to-v4')),
 import(host('dsh-session-log-export')),import(host('dsh-compaction-basic')),import(host('dsh-compaction-tool-result-pruner'))]);
const currentFile=path.join(root,'adapters/dsh/compaction-support.mjs');
const {createCompactionSupport}=await import(pathToFileURL(currentFile));
const {createContextManager}=await import(pathToFileURL(path.join(root,'adapters/dsh/context-manager.mjs')));
const baselineFile=process.env.MEMORY_BRAIN_BASELINE_SUPPORT||path.join(root,'original/adapters/dsh/compaction-support.mjs');
const baseline=fs.existsSync(baselineFile)?await import(pathToFileURL(baselineFile)):null;
const checks=[],scenarios=[],budget={thresholdChars:4096,headChars:2048,tailChars:512};
let modelCalls=0,ctx,support;
const check=(name,fn)=>{fn();checks.push({name,passed:true});};
class SyntheticLlm extends Service{constructor(ctx){super(ctx,'llm');}stream(){modelCalls++;throw Error('unexpected_model_call');}}
class SyntheticSessions extends Service{constructor(ctx){super(ctx,'sessions');}}
class SyntheticProjections extends Service{constructor(ctx){super(ctx,'sessionProjections');}}
class SyntheticAgents extends Service{constructor(ctx){super(ctx,'agents');this.items=[];}list(){return this.items;}}
class SyntheticMeter extends Service{constructor(ctx){super(ctx,'tokenMeter');this.calls=0;}estimateMessage(m){this.calls++;return Math.ceil(m.content.reduce((n,b)=>n+(b.text?.length||0),0)/4);}}
async function setup({hostPruner=false}={}){
 const context=new Context();
 for(const [Class,options]of[[SyntheticLlm],[SyntheticSessions],[SyntheticProjections],[SyntheticAgents],[SyntheticMeter],
  [basic.BasicCompactionEngine,{auto:false}],...(hostPruner?[[pruner.ToolResultPruner,budget]]:[])]){
  const fiber=context.plugin(Class,options);await fiber.await();assert.equal(fiber.state,2);
 }
 return context;
}
function services(factory,context){return factory({ctx:context,basicCompactionModule:host('dsh-compaction-basic'),
 llmModule:host('dsh-llm'),cordisModule:host('cordis'),toolResultPrunerModule:host('dsh-compaction-tool-result-pruner'),
 tokenMeterModule:host('dsh-token-meter')});}
function fixture(id){const s=Session.create('synthetic-public-'+id);s.append('turn/start',{turn:1});
 s.append('request/header',{header:{config:{provider:'synthetic',model:'no-model-called'}},reason:'initial'});
 s.append('step/start',{turn:1,step:1});
 const call={type:'tool-call',id:'synthetic-call',name:'synthetic_tool',arguments:'{}'};
 s.append('assistant/message',{turn:1,step:1,stream:[],message:llm.createAssistantMessage({content:[call],source:{provider:'synthetic',model:'no-model-called'}})},{surfaceOp:'append'});
 const started=s.append('tool/call',{turn:1,step:1,callId:call.id,name:call.name,arguments:call.arguments});
 s.append('tool/result',{turn:1,step:1,message:llm.createToolResultMessage({callId:call.id,content:[{type:'text',text:'SYNTHETIC_TOOL_OUTPUT_'.repeat(400)}],isError:false})},{surfaceOp:'append',sourceEventSeqs:[started.seq]});
 s.append('step/end',{turn:1,step:1});return s;}
const close=(s,turn=1)=>s.append('turn/end',{turn,reason:{kind:'completed'}});
const serialize=s=>serializeSessionLog(s.header,s.snapshotEvents());
function released(s,label){
 const text=serialize(s),rows=text.trimEnd().split('\n').map(JSON.parse),header=rows.shift();delete header.type;
 const artifact={header,events:rows,inheritedEventCount:0};
 fs.writeFileSync(path.join(root,'synthetic-'+label+'.jsonl'),text);
 format.restoreReleasedV4Artifact(artifact,KNOWN_SESSION_EVENT_TYPES);
 format.assertReleasedV4Relationships(artifact,KNOWN_SESSION_EVENT_TYPES);
 return artifact.events.length;
}
async function stop(){if(support)await support.stop();support=null;if(ctx){await ctx.fiber.dispose();await ctx.fiber.await();ctx=null;}}
const result={baseline_plugin_version:'0.2.3',candidate_version_target:'0.2.4',checks,scenarios,model_calls:0,network_calls:0,
 production_changed:false,host_changed:false,budgets_changed:false,
 candidate_sha256:crypto.createHash('sha256').update(fs.readFileSync(currentFile)).digest('hex'),
 host_pruner_sha256:crypto.createHash('sha256').update(fs.readFileSync(fileURLToPath(host('dsh-compaction-tool-result-pruner')))).digest('hex')};
try{
 if(baseline){
  ctx=await setup();const session=fixture('baseline-idle');close(session);const agent={ctx,session,status:'idle'};
  ctx.agents.items.push(agent);
  check('baseline_ended_log_valid_before_plugin_load',()=>released(session,'baseline-before'));
  support=services(baseline.createCompactionSupport,ctx);await support.ready;
  check('public023_idle_startup_reproduces_native_prune',()=>assert.equal(support.status().pruned_results,1));
  check('public023_idle_startup_log_rejected_by_released_reader',()=>assert.throws(()=>released(session,'baseline-invalid'),/tool\/result is outside/));
  scenarios.push({name:'public023_ready_existing_idle',native_pruned:1,released_v4_valid:false});await stop();
 }
 ctx=await setup();const ended=fixture('candidate-idle');close(ended);const agent={ctx,session:ended,status:'idle'};
 ctx.agents.items.push(agent);const before=serialize(ended);
 support=services(createCompactionSupport,ctx);await support.ready;
 check('owned_native_pruner_can_prepare_without_idle_mutation',()=>{assert.equal(support.status().pruner_state,'owned_native');assert.equal(support.status().pruned_results,0);assert.equal(serialize(ended),before);released(ended,'candidate-idle');});
 for(let i=0;i<10;i++)await support.prepareAgent(agent);
 check('repeated_idle_preparation_keeps_log_and_metrics',()=>{assert.equal(serialize(ended),before);assert.equal(ctx.tokenMeter.calls,0);assert.equal(support.status().failures,0);});
 ended.append('turn/start',{turn:2});agent.status='running';
 const contextManager=createContextManager({enabled:true,compactionModule:host('dsh-compaction'),
  prepareAgent:(current,signal)=>support.prepareAgent(current,signal)});
 const controller=new AbortController();const decision={kind:'enter'};
 const next=await contextManager.beforeStep({agent,turn:2,step:1,signal:controller.signal},decision);
 check('actual_context_manager_prestep_still_prunes_in_new_turn',()=>{assert.equal(next,decision);assert.equal(support.status().pruned_results,1);assert(support.status().pruned_chars>0);close(ended,2);released(ended,'candidate-next-turn');});
 contextManager.stop();scenarios.push({name:'candidate_idle_then_context_manager_prestep',idle_mutated:false,native_pruned_next_turn:1,released_v4_valid:true});
 const continuation=fixture('continuation');agent.session=continuation;
 const continuationManager=createContextManager({enabled:true,compactionModule:host('dsh-compaction'),prepareAgent:(current,signal)=>support.prepareAgent(current,signal)});
 await continuationManager.beforeStep({agent,turn:1,step:2,signal:controller.signal},decision);
 check('tool_continuation_prepare_still_prunes',()=>{assert.equal(support.status().pruned_results,2);close(continuation);released(continuation,'candidate-continuation');});
 continuationManager.stop();await stop();
 ctx=await setup({hostPruner:true});const hostEnded=fixture('existing-host');close(hostEnded);const hostBefore=serialize(hostEnded);
 const hostAgent={ctx,session:hostEnded,status:'idle'};ctx.agents.items.push(hostAgent);
 const nativeInstance=ctx.toolResultPruner[Symbol.for('cordis.original')]||ctx.toolResultPruner;
 const nativeMethod=nativeInstance.pruneSession;
 support=services(createCompactionSupport,ctx);await support.ready;
 check('existing_host_pruner_is_not_patched_or_called_when_idle',()=>{assert.equal(support.status().pruner_state,'host_existing');assert.equal(nativeInstance.pruneSession,nativeMethod);assert.equal(serialize(hostEnded),hostBefore);released(hostEnded,'host-existing-idle');});
 hostEnded.append('turn/start',{turn:2});hostAgent.status='running';
 await support.prepareAgent(hostAgent,controller.signal);
 check('existing_host_pruner_active_call_remains_replay_safe',()=>{assert.equal(support.status().pruned_results,1);close(hostEnded,2);released(hostEnded,'host-existing-next');});
 const empty=Session.create('synthetic-public-no-turn');hostAgent.session=empty;hostAgent.status='idle';const emptyBefore=serialize(empty);
 await support.prepareAgent(hostAgent);
 check('brand_new_agent_without_turn_has_no_events',()=>{assert.equal(serialize(empty),emptyBefore);released(empty,'empty');});
 const restoreSource=fixture('restored-closed-source');close(restoreSource);
 const restored=Session.create(restoreSource.id,restoreSource.snapshotEvents(),restoreSource.header);
 hostAgent.session=restored;const restoreBefore=serialize(restored);
 await support.prepareAgent(hostAgent);
 check('restored_closed_session_seed_marker_does_not_authorize_pruning',()=>{assert.equal(serialize(restored),restoreBefore);released(restored,'restored-closed');});
 const active=fixture('running-no-signal');hostAgent.session=active;hostAgent.status='running';const activeBefore=serialize(active);
 await support.prepareAgent(hostAgent);
 check('running_agent_without_admitted_signal_is_still_not_touched',()=>{assert.equal(serialize(active),activeBefore);assert.equal(support.status().state,'waiting_idle');});
 close(active);await support.prepareAgent(hostAgent,controller.signal);
 check('ended_turn_with_signal_is_also_guarded',()=>{assert.equal(support.status().pruned_results,1);released(active,'ended-with-signal');});
 const aborted=fixture('aborted');hostAgent.session=aborted;const aborter=new AbortController();aborter.abort();const abortBefore=serialize(aborted);
 await support.prepareAgent(hostAgent,aborter.signal);
 check('aborted_preparation_does_not_prune',()=>assert.equal(serialize(aborted),abortBefore));close(aborted);released(aborted,'aborted');
 const repeated=fixture('cache');close(repeated);hostAgent.session=repeated;hostAgent.status='idle';let reads=0;
 repeated.log=new Proxy(repeated.log,{get(target,key,receiver){if(typeof key==='string'&&/^\d+$/.test(key))reads++;return Reflect.get(target,key,receiver);}});
 await support.prepareAgent(hostAgent);reads=0;
 for(let i=0;i<20;i++)await support.prepareAgent(hostAgent);
 check('closed_unchanged_log_uses_incremental_checkpoint_only',()=>assert(reads<=22));
 result.cache_indexed_reads_for20_idle_calls=reads;
 check('no_model_calls_any_path',()=>assert.equal(modelCalls,0));
}catch(error){result.error={name:error.name,message:error.message};process.exitCode=1;}
finally{
 await stop();result.passed=checks.length;result.failed=result.error?1:0;result.model_calls=modelCalls;
 result.boundaries=['Actual public compaction-support/context-manager functions, real Cordis Context+BasicCompactionEngine+ToolResultPruner, real detached Session, actual installed released-V4 relationship validator.',
  'Prerequisite LLM/session registry services are synthetic and no model is called. This is not a complete AgentLoop or actual persistence-service run.'];
 fs.writeFileSync(path.join(root,'TEST-RESULT.json'),JSON.stringify(result,null,2)+'\n');
 console.log(JSON.stringify({passed:result.passed,failed:result.failed,model_calls:result.model_calls,candidate_sha256:result.candidate_sha256,error:result.error}));
}
