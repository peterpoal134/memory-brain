/** DSH_RUNTIME=<real host> node tests/test_chat_usage_loop.mjs; local stubs only. */
import fs from 'node:fs';import os from 'node:os';import path from 'node:path';
import http from 'node:http';import assert from 'node:assert/strict';import {pathToFileURL} from 'node:url';
const runtime=process.env.DSH_RUNTIME;if(!runtime)throw new Error('DSH_RUNTIME is required; no fake host substitute.');
const mod=name=>import(pathToFileURL(path.join(runtime,'node_modules/@deepseek-ai',name,'lib/index.js')).href);
const [{Context,Service},{SessionStore},{SessionProjectionRegistry},{AgentRegistry},{AgentLoop},{ToolRuntime},{SystemPrompt},llm]=
 await Promise.all(['cordis','dsh-session','dsh-session-projection','dsh-agent','dsh-agent-loop','dsh-tools','dsh-system-prompt','dsh-llm'].map(mod));
const plugin=await import('../adapters/dsh/xiaojiu-brain.mjs');
const directory=fs.mkdtempSync(path.join(os.tmpdir(),'chat-usage-loop-')),usage=new Map(),raw=[],warnings=[],errors=[];
let modelCalls=0,passed=0,adapter;
const check=(name,value)=>{assert(value,name);passed++;console.log('PASS '+name);};
const server=http.createServer(async(req,res)=>{
 let text='';for await(const part of req)text+=part;const body=text?JSON.parse(text):{};
 const send=(code,data)=>{res.writeHead(code,{'Content-Type':'application/json'});res.end(JSON.stringify(data));};
 if(req.url==='/v1/chat/usage'){
  const key=body.conversation_id+'|'+body.request_id,duplicate=usage.has(key);if(!duplicate)usage.set(key,body);
  return send(200,{ok:true,data:{accepted:!duplicate,duplicate,request_id:body.request_id,source_event_seq:body.source_event_seq}});
 }
 if(req.url==='/v1/ingest'){raw.push(body);return send(200,{ok:true,data:{accepted:true,id:raw.length}});}
 return send(404,{ok:false,error:'fixture_route_unavailable'});
});await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
class Model extends Service{
 constructor(ctx){super(ctx,'llm');}
 async prepareCall(config){return {config,stream:request=>this.stream(request)};}
 async *stream(request){
  assert(llm.isAgentLoopRequest(request));modelCalls++;const n=modelCalls;
  if(n===1)yield {type:'block-end',index:0,block:{type:'tool-call',id:'synthetic-call',name:'usage_noop',arguments:'{}'}};
  else if(n!==3){yield {type:'block-end',index:0,block:{type:'reasoning',text:'SYNTHETIC_REASONING_PRIVATE'}};
   yield {type:'block-end',index:1,block:{type:'text',text:'SYNTHETIC_FINAL_RECEIPT'}};}
  yield {type:'usage',usage:{inputTokens:100*n,cacheReadTokens:20*n,cacheWriteTokens:5*n,outputTokens:10*n,totalTokens:135*n}};
  yield {type:'finish',reason:n===3?{kind:'error',failure:{code:'SYNTHETIC_RETRY',message:'Synthetic retry only'}}:{kind:n===1?'tool-calls':'stop'}};
 }
}
const ctx=new Context();
try{
 for(const [service,config]of[[SessionProjectionRegistry],[SessionStore],[AgentRegistry],[SystemPrompt,{includeRuntimeContext:false,includeHarnessIdentity:false}],[ToolRuntime],[Model]]){
  const fiber=ctx.plugin(service,config);await fiber.await();assert.equal(fiber.state,2);
 }
 if(typeof ctx.tools.setExecutor==='function'){const fiber=ctx.plugin(await mod('dsh-tool-executor-native'));await fiber.await();}
 ctx.on('agent/request-error',async()=>modelCalls===3?{kind:'retry'}:undefined);
 ctx.on('agent/error',event=>errors.push(event.error?.name||'agent_error'));
 const fiber=ctx.plugin(plugin,{enabled:true,sessionModule:path.join(runtime,'node_modules/@deepseek-ai/dsh-session/lib/index.js'),
  toolsModule:path.join(runtime,'node_modules/@deepseek-ai/dsh-tools/lib/index.js'),queueDir:path.join(directory,'queue'),
  baseUrl:'http://127.0.0.1:'+server.address().port,token:'local-synthetic-token',recallEnabled:false,importantToolEnabled:false,
  intervalMs:60000,chatUsageIntervalMs:60000,timeoutMs:500,chatUsageTimeoutMs:500});
 await fiber.await();adapter=plugin.getAdapter(fiber.ctx);check('real Cordis adapter activates metadata collector',fiber.state===2&&adapter?.enabled&&adapter.chatUsage);
 ctx.tools.register({name:'usage_noop',description:'Synthetic no-op',parameters:{type:'object',properties:{},required:[],additionalProperties:false},
  output:{schema:{type:'string'},render:(_,value)=>[{type:'text',text:value}]},execute:async()=> 'SYNTHETIC_TOOL_PRIVATE'});
 const loop=ctx.plugin(AgentLoop,{agents:[],maxParallelToolCalls:2});await loop.await();
 const agent=await ctx.agentLoop.create('synthetic-usage-loop',{provider:'synthetic-route',model:'synthetic-model'});
 for(const id of ['one','two']){agent.followup(llm.createUserMessage({id,source:{kind:'user'},content:[{type:'text',text:'SYNTHETIC_USER_PRIVATE_'+id}]}));await agent.whenIdle();}
 for(let count=0;count<10&&usage.size<4;count++){await adapter.chatUsage.tick();await adapter.chatUsage.flush({budgetMs:5000});}
 await adapter.queue.flush({budgetMs:5000});
 const records=[...usage.values()];
 check('two real turns include tool continuation and failed retry',modelCalls===4&&errors.length===0);
 check('all four actual host requests are recorded exactly once',usage.size===4&&new Set(records.map(row=>row.request_id)).size===4);
 check('one failed attempt and three assistant messages have distinct durable IDs',records.filter(row=>row.outcome==='error').length===1&&records.filter(row=>row.outcome==='success').length===3&&records.every(row=>row.request_id==='dsh-event:'+row.source_event_seq));
 check('usage totals include the tool request and failed retry, without duplicate usage copies',records.reduce((sum,row)=>sum+row.total_tokens,0)===1350&&records.reduce((sum,row)=>sum+row.input_tokens,0)===1250);
 check('two turn groups contain all request records instead of only finals',new Set(records.map(row=>row.turn_id)).size===2&&[...new Set(records.map(row=>row.turn_id))].every(turn=>records.filter(row=>row.turn_id===turn).length===2));
 check('provider model and live start timestamps are captured from actual host events',records.every(row=>row.provider==='synthetic-route'&&row.model==='synthetic-model'&&row.capture_mode==='live'&&typeof row.started_at==='number'));
 check('usage ledger contains no prompts reasoning tool arguments or output text',!JSON.stringify(records).includes('PRIVATE')&&!JSON.stringify(records).includes('SYNTHETIC_FINAL'));
 check('original message queue remains separate from all-request usage accounting',raw.length===4&&raw.filter(row=>row.role==='user').length===2&&raw.filter(row=>row.role==='assistant').length===2);
 adapter.chatUsage.scan(agent.session);await adapter.chatUsage.flush({budgetMs:5000});check('session replay does not add request accounting rows',usage.size===4);
 console.log(JSON.stringify({passed,failed:0,real_model_calls:0,local_stub_calls:modelCalls,metadata_requests:usage.size}));
}finally{
 await adapter?.stop();await ctx.fiber.dispose();await ctx.fiber.await();await new Promise(resolve=>server.close(resolve));
 fs.rmSync(directory,{recursive:true,force:true});
}
