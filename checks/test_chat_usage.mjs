/** Metadata-only HTTP/outbox tests. No model provider or production data. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import {ChatUsageCollector,normalizeUsage,usageFromEvent} from '../adapters/dsh/chat-usage.mjs';
const temporary=fs.mkdtempSync(path.join(os.tmpdir(),'chat-usage-'));
const records=new Map(),received=[],warnings=[];let mode='accept',passed=0;
const check=(name,fn)=>{fn();passed++;console.log('PASS '+name);};
const makeEvent=(seq,{turn=1,step=1,usage={inputTokens:10,outputTokens:3,totalTokens:22,cacheReadTokens:7,cacheWriteTokens:2},type='assistant/message',finish='stop'}={})=>({
 type,seq,time:1700000000000+seq,data:{turn,step,...(type==='assistant/message'?{message:{source:{kind:'model',provider:'route-alias',model:'fixture-model'},content:[{type:'text',text:'PRIVATE_BODY_MUST_NOT_BE_STORED'}]},usage}:{}),
 stream:[{type:'text-chunks',time0:1700000000000,dt:[],texts:['PRIVATE_REASONING_MUST_NOT_BE_STORED']},
  ...(usage?[{type:'chunk',time:1700000000001,chunk:{type:'usage',usage}}]:[]),
  {type:'chunk',time:1700000000002,chunk:{type:'finish',reason:{kind:finish}}}]}});
const header={type:'request/header',seq:0,time:1700000000000,data:{header:{config:{provider:'route-alias',model:'fixture-model',api_key:'PRIVATE_KEY_MUST_NOT_BE_STORED',baseUrl:'https://proxy.invalid/private'}}}};
const server=http.createServer(async(req,res)=>{
 let raw='';for await(const chunk of req)raw+=chunk;const body=JSON.parse(raw);received.push(body);
 const reply=(status,data)=>{res.writeHead(status,{'Content-Type':'application/json'});res.end(JSON.stringify(data));};
 if(mode==='outage')return reply(503,{ok:false});
 if(mode==='conflict')return reply(409,{ok:false,error:'conflict'});
 if(mode==='unknown')return reply(200,{ok:true,data:{accepted:true}});
 if(mode==='wrong')return reply(200,{ok:true,data:{accepted:true,request_id:body.request_id,source_event_seq:body.source_event_seq+1}});
 const key=body.conversation_id+'|'+body.request_id,duplicate=records.has(key);if(!duplicate)records.set(key,body);
 return reply(200,{ok:true,data:{accepted:!duplicate,duplicate,request_id:body.request_id,source_event_seq:body.source_event_seq,conversation_id:body.conversation_id}});
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const baseUrl='http://127.0.0.1:'+server.address().port,instances=[];
const collector=name=>{const value=new ChatUsageCollector({dir:path.join(temporary,name),baseUrl,token:'fixture-token',timeoutMs:500,intervalMs:60000,onWarning:code=>warnings.push(code)}).init();instances.push(value);return value;};
try{
 check('disjoint source input normalized from full total without double count',()=>assert.deepEqual(normalizeUsage({inputTokens:10,outputTokens:3,totalTokens:22,cacheReadTokens:7,cacheWriteTokens:2}),{input_tokens:19,output_tokens:3,cache_hit_tokens:7,cache_miss_tokens:12,total_tokens:22,normalization_basis:'reported_total_minus_output'}));
 check('missing cache fields stay null instead of becoming zero',()=>{const row=normalizeUsage({inputTokens:10,outputTokens:3,totalTokens:13});assert.equal(row.input_tokens,10);assert.equal(row.cache_hit_tokens,null);assert.equal(row.cache_miss_tokens,null);});
 check('complete disjoint sum is allowed when total is absent',()=>{const row=normalizeUsage({inputTokens:10,outputTokens:3,cacheReadTokens:7,cacheWriteTokens:2});assert.equal(row.input_tokens,19);assert.equal(row.total_tokens,22);assert.equal(row.normalization_basis,'disjoint_sum');});
 check('partial invalid counts are never guessed into total input',()=>{const row=normalizeUsage({inputTokens:10,outputTokens:3,cacheReadTokens:true,cacheWriteTokens:NaN});assert.equal(row.input_tokens,null);assert.equal(row.total_tokens,null);assert.equal(row.cache_hit_tokens,null);});
 check('one settlement uses last usage once, not top plus stream',()=>{const event=makeEvent(1);event.data.stream.push({type:'chunk',time:3,chunk:{type:'usage',usage:{inputTokens:20,outputTokens:4,totalTokens:30,cacheReadTokens:6,cacheWriteTokens:0}}});assert.equal(usageFromEvent(event,{conversationId:'c'}).input_tokens,26);});
 check('failed request compact stream usage survives without a top-level usage',()=>{const row=usageFromEvent(makeEvent(2,{type:'assistant/attempt',finish:'error'}),{conversationId:'c',header:{model:'safe-model',provider:'safe-route'}});assert.equal(row.outcome,'error');assert.equal(row.model,'safe-model');assert.equal(row.input_tokens,19);});
 check('no usage is unknown, not a zero-token free call',()=>{const row=usageFromEvent(makeEvent(3,{usage:null}),{conversationId:'c'});assert.equal(row.input_tokens,null);assert.equal(row.output_tokens,null);assert.equal(row.total_tokens,null);assert.equal(row.started_at,null);});
 check('provider name alone cannot identify an official model origin',()=>{const row=usageFromEvent(makeEvent(4),{conversationId:'c'});assert.equal(row.provider,'route-alias');const event=makeEvent(4);event.data.message.source.baseUrl='https://api.deepseek.com/v1';const official=usageFromEvent(event,{conversationId:'c'});assert.equal(official.provider,'deepseek_official');assert(!JSON.stringify(official).includes('https://'));});
 const one=collector('strict'),event=makeEvent(5),payload=usageFromEvent(event,{conversationId:'strict'});one.enqueue(payload);
 mode='unknown';await one.flush();check('unmatched or incomplete200ACK stays durable',()=>assert.equal(one.status().pending,1));
 mode='wrong';one.retryAt=0;await one.flush();check('ACK must match exact durable event sequence',()=>assert.equal(one.status().pending,1));
 mode='outage';one.retryAt=0;await one.flush();check('503 leaves metadata pending',()=>assert.equal(one.status().pending,1));
 mode='accept';one.retryAt=0;await one.flush();check('accepted matching key ACK clears pending only after durable acknowledgement',()=>assert.equal(one.status().pending,0));
 await one.stop();const restored=collector('strict');
 check('live versus history replay deduplicates after restart',()=>{assert(restored.enqueue({...payload,started_at:123,capture_mode:'live'}));assert.equal(restored.status().pending,0);});
 check('same request with changed actual counts is visibly rejected, never overwritten',()=>{restored.enqueue({...payload,output_tokens:99});assert.equal(restored.status().conflicts,1);assert.equal(restored.status().failed,1);});
 await restored.stop();const restoredAgain=collector('strict');check('failed alternate body cannot poison previously acknowledged identity',()=>{restoredAgain.enqueue(payload);assert.equal(restoredAgain.status().conflicts,0);assert.equal(restoredAgain.status().pending,0);});
 const conflict=collector('conflict');conflict.enqueue(usageFromEvent(makeEvent(6),{conversationId:'conflict'}));mode='conflict';await conflict.flush();check('server409is retained as visible failed metadata',()=>{assert.equal(conflict.status().pending,0);assert.equal(conflict.status().failed,1);});mode='accept';
 const bounded=collector('bounded'),events=[header,...Array.from({length:502},(_,n)=>makeEvent(n+1,{turn:Math.floor(n/4)+1,step:n%4+1}))],session={id:'history502',log:events};
 const first=bounded.scan(session);check('one catch-up batch is capped at50requests and200events',()=>{assert(first.requests<=50&&first.events<=200);assert.equal(first.requests,50);assert(!first.complete);});
 await bounded.stop();const continued=collector('bounded');await continued.importHistory(session.id,session.log);
 for(let index=0;index<12&&continued.status().pending;index++)await continued.flush({budgetMs:10000});
 check('cursor restores and imports all502request settlements exactly once',()=>{assert.equal([...records.values()].filter(row=>row.conversation_id==='history502').length,502);assert.equal(continued.status().pending,0);});
 const privacy=JSON.stringify([...records.values()])+fs.readFileSync(path.join(continued.dir,'cursors',fs.readdirSync(path.join(continued.dir,'cursors'))[0]),'utf8');
 check('only metadata survives, not text, reasoning, credentials or URLs',()=>{for(const forbidden of ['PRIVATE_BODY','PRIVATE_REASONING','PRIVATE_KEY','https://proxy.invalid'])assert(!privacy.includes(forbidden));});
 const gap=collector('capacity');gap.maxPending=0;gap.scan({id:'gap',log:[makeEvent(7)]});check('disk/capacity gap never advances past a missing record',()=>assert.equal(fs.readdirSync(path.join(gap.dir,'cursors')).length,0));
 gap.maxPending=10;gap.scan({id:'gap',log:[makeEvent(7)]});await gap.flush();check('space recovery resumes the same request',()=>assert.equal(gap.status().pending,0));
 const live=collector('live'),agent={session:{id:'live',log:[makeEvent(8)]}};
 live.observeFrame(agent,{type:'start',attemptId:'session:1'});live.observeFrame(agent,{type:'end',attemptId:'session:1',outcome:{kind:'committed',seq:8}});
 live.scan(agent.session);const pending=JSON.parse(fs.readFileSync(path.join(live.dir,'pending',live.pending()[0]),'utf8')).payload;
 check('live host timing is captured while request ID remains durable seq',()=>{assert.equal(pending.capture_mode,'live');assert.equal(pending.request_id,'dsh-event:8');assert.equal(typeof pending.started_at,'number');});
 check('a second writer cannot steal or clear the active metadata outbox lock',()=>assert.throws(()=>collector('live'),/writer_in_use/));
 console.log(JSON.stringify({passed,failed:0,synthetic_http_records:records.size}));
}finally{
 for(const value of instances)await value.stop();
 await new Promise(resolve=>server.close(resolve));
 fs.rmSync(temporary,{recursive:true,force:true});
}
