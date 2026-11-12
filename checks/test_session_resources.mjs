/** Synthetic lifecycle/durable-overflow tests; no live services or model calls. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { ChatUsageCollector, usageFromEvent } from '../adapters/dsh/chat-usage.mjs';

const entry = process.env.RESOURCE_ADAPTER_ENTRY || path.resolve('adapters/dsh/xiaojiu-brain.mjs');
const { createAdapter } = await import(pathToFileURL(entry).href);
const root = fs.mkdtempSync(path.join(os.tmpdir(), 'brain-session-resources-'));
const fixture = path.join(root, 'sessions.mjs');
fs.writeFileSync(fixture, `export class SessionStore { prepare(id) { return {id,log:[],append(type,data){const event={type,data,seq:this.log.length,time:1700000000000+this.log.length};this.log.push(event);return event;},deriveMessages(){return this.log.filter(e=>e.type==='user/message').map(e=>e.data);}}; } }`);
const { SessionStore } = await import(pathToFileURL(fixture).href);
let passed = 0, serial = 0;
const checks = [], instances = [];
const check = (name, fn) => { fn(); passed++; checks.push(name); console.log('PASS ' + name); };
const message = id => ({id,role:'user',source:{kind:'user'},content:[{type:'text',text:'Synthetic utterance '+id}]});
const usageEvent = seq => ({type:'assistant/message',seq,time:1700000000000+seq,data:{turn:1,step:1,
 message:{id:'a'+seq,role:'assistant',source:{kind:'model',model:'synthetic'},content:[{type:'text',text:'not part of usage metadata'}]},
 usage:{inputTokens:9,outputTokens:1,totalTokens:10},stream:[]}});
const response = (code, data) => ({ok:code>=200&&code<300,status:code,json:async()=>data});
class Events {
 constructor(){this.hooks=new Map();this.effects=[];this.logger={warn(){},info(){}};}
 on(name, fn){let set=this.hooks.get(name);if(!set)this.hooks.set(name,set=new Set());set.add(fn);return ()=>{set.delete(fn);if(!set.size)this.hooks.delete(name);};}
 emit(name, ...args){for(const fn of [...(this.hooks.get(name)||[])])fn(...args);}
 get(){return {list:()=>[]};}
 effect(fn){this.effects.push(fn());}
 count(){return [...this.hooks.values()].reduce((n,set)=>n+set.size,0);}
}
async function adapter({dir=path.join(root,'queue-'+ ++serial),ctx=new Events(),maxPending=2,fetchImpl,usage=true}={}){
 const deliveries=[];
 const fetch = fetchImpl || (async (url, init)=>{
  const payload=JSON.parse(init.body);
  if(url.endsWith('/v1/ingest')){deliveries.push(payload);return response(200,{ok:true,data:{accepted:true}});}
  if(url.endsWith('/v1/chat/usage'))return response(200,{ok:true,data:{accepted:true,request_id:payload.request_id,source_event_seq:payload.source_event_seq,conversation_id:payload.conversation_id}});
  return response(404,{ok:false});
 });
 const value=await createAdapter(ctx,{enabled:true,baseUrl:'http://127.0.0.1:1',token:'synthetic-resource-token-not-live',
  sessionModule:pathToFileURL(fixture).href,queueDir:dir,maxPending,intervalMs:60000,timeoutMs:100,
  importantToolEnabled:false,historyEnabled:false,chatUsageEnabled:usage,chatUsageIntervalMs:60000,fetchImpl:fetch});
 assert(value.enabled);instances.push(value);return {value,ctx,dir,deliveries};
}
const recovery = dir => fs.readdirSync(path.join(dir,'session-recovery')).filter(n=>n.endsWith('.json'));
const append = (session, id) => session.append('user/message',message(id));
const restore = (store,id,events) => {const s=store.prepare(id);s.log.push(...events);return s;};
try {
 const a=await adapter(), store=new SessionStore(), first=store.prepare('one');
 const wrapped=first.append;append(first,'a');
 a.ctx.emit('session/disposed',first);
 check('disposed releases exact session, restores append and clears usage registration',()=>{
  assert.notEqual(first.append,wrapped);assert.equal(a.value.status().capture.attached_sessions,0);assert.equal(a.value.chatUsage.sessions.size,0);
 });
 const second=store.prepare('one'), secondWrapped=second.append;
 a.ctx.emit('session/disposed',first);
 check('a late duplicate disposal cannot restore a same-ID new instance',()=>{assert.equal(second.append,secondWrapped);assert.equal(a.value.status().capture.attached_sessions,1);});
 await a.value.stop();
 check('stop restores remaining sessions, clears listeners and is idempotent',()=>{
  assert.notEqual(second.append,secondWrapped);assert.equal(a.ctx.count(),0);assert.equal(a.value.status().capture.attached_sessions,0);assert.equal(a.value.stop(),a.value.stop());
 });
 const count=a.deliveries.length;append(first,'after-stop');append(second,'after-stop-new');
 check('append after stop has no capture callback',()=>assert.equal(a.deliveries.length,count));

 const b=await adapter({maxPending:1}), s=store.prepare('overflow');
 for(let i=0;i<6;i++)append(s,'old-'+i);
 s.append('tool/result',{content:'SYSTEM_TOOL_MUST_NOT_APPEAR'});
 s.append('user/message',{...message('injected'),source:{kind:'runtime-context'}});
 s.append('assistant/message',{interrupted:true,message:{id:'partial',role:'assistant',source:{kind:'model'},content:[{type:'text',text:'PARTIAL_MUST_NOT_APPEAR'}]}});
 const events=[...s.log];b.ctx.emit('session/disposed',s);
 check('capacity-full disposal durably spools remaining eligible messages before advancing cursor',()=>{
  assert.equal(b.value.queue.stats().pending,1);assert.equal(recovery(b.dir).length,5);assert.equal(b.value.queue.cursor(s.id).index,events.length-1);
  const text=recovery(b.dir).map(n=>fs.readFileSync(path.join(b.dir,'session-recovery',n),'utf8')).join('');
  assert(!text.includes('SYSTEM_TOOL'));assert(!text.includes('injected'));assert(!text.includes('PARTIAL'));
 });
 await b.value.stop();
 const c=await adapter({dir:b.dir,maxPending:1}), s2=restore(store,'overflow',events);append(s2,'new-last');
 check('recovery receipt remains until ACK, not merely an enqueue',()=>{assert.equal(recovery(c.dir).length,5);assert.equal(c.value.queue.stats().pending,1);});
 for(let i=0;i<10;i++){await c.value.queue.flush();c.ctx.emit('session/event',s2);}
 await c.value.queue.flush();c.ctx.emit('session/event',s2);
 check('restart drains oldest overflow before fresh messages without duplicate delivery',()=>{
  assert.deepEqual(c.deliveries.map(row=>row.message_id),['old-0','old-1','old-2','old-3','old-4','old-5','new-last']);
  assert.equal(recovery(c.dir).length,0);
 });
 await c.value.stop();

 const d=await adapter({maxPending:1}), lost=store.prepare('disk-error');append(lost,'safe');append(lost,'retry');
 const cursor=d.value.queue.cursor(lost.id), originalOpen=fs.openSync;
 fs.openSync=function(file,...args){if(String(file).includes('session-recovery')){const error=new Error('synthetic disk unavailable');error.code='EIO';throw error;}return originalOpen.call(this,file,...args);};
 try{d.ctx.emit('session/disposed',lost);}finally{fs.openSync=originalOpen;}
 check('dual storage failure is visible and cannot advance past the missing record',()=>{
  assert.deepEqual(d.value.queue.cursor(lost.id),cursor);assert(d.value.warnings.some(x=>x.code==='session_release_capture_not_durable_host_replay_required'));
 });
 await d.value.queue.flush();const retry=restore(store,lost.id,lost.log);d.ctx.emit('session/event',retry);await d.value.queue.flush();
 check('host log replay recovers a disk-error gap after storage is restored',()=>assert.deepEqual(d.deliveries.map(row=>row.message_id),['safe','retry']));
 await d.value.stop();

 const e=await adapter({maxPending:100}), active=store.prepare('active');
 const all=[];for(let i=0;i<35;i++){const item=store.prepare('released-'+i);append(item,'message-'+i);all.push(item);e.ctx.emit('session/disposed',item);}
 append(active,'active-still-captured');
 check('many disposed sessions do not accumulate while the live session remains captured',()=>{
  assert.equal(e.value.status().capture.attached_sessions,1);assert(e.value.queue.has({conversation_id:active.id,message_id:'active-still-captured',role:'user'}));
 });
 const baseline=e.ctx.count();await e.value.stop();
 for(let i=0;i<3;i++){const again=await adapter({ctx:e.ctx});assert.equal(e.ctx.count(),baseline);await again.value.stop();assert.equal(e.ctx.count(),0);}
 check('repeated stop/reconnect on one context does not accumulate event callbacks',()=>assert.equal(e.ctx.count(),0));

 const chunked=await adapter({maxPending:1,usage:false}), many=store.prepare('chunked');
 for(let i=0;i<58;i++)append(many,'batch-'+i);
 chunked.ctx.emit('session/disposed',many);await chunked.value.queue.flush();chunked.value.queue.maxPending=100;
 const newer=restore(store,many.id,many.log);append(newer,'after-58');
 check('a bounded recovery pass cannot let fresh input skip the unprocessed fifty-record boundary',()=>{
  const ids=chunked.value.queue.pendingFiles().map(name=>JSON.parse(fs.readFileSync(path.join(chunked.dir,'pending',name),'utf8')).payload.message_id);
  assert(!ids.includes('after-58'));assert.equal(ids.length,50);
 });
 for(let i=0;i<4;i++){await chunked.value.queue.flush();chunked.ctx.emit('session/event',newer);}
 check('bounded recovery eventually preserves complete ordering across its pass boundary',()=>assert.deepEqual(chunked.deliveries.map(row=>row.message_id),[...Array.from({length:58},(_,i)=>'batch-'+i),'after-58']));
 await chunked.value.stop();

 const usageDir=path.join(root,'usage-resources');let delivered=0;
 const usageFetch=async(_url,init)=>{const p=JSON.parse(init.body);delivered++;return response(200,{ok:true,data:{accepted:true,request_id:p.request_id,source_event_seq:p.source_event_seq,conversation_id:p.conversation_id}});};
 const makeUsage=()=>new ChatUsageCollector({dir:usageDir,baseUrl:'http://127.0.0.1:1',token:'synthetic-usage',fetchImpl:usageFetch,intervalMs:60000,indexCacheEntries:4,indexCacheBytes:1024,maxPending:1}).init();
 let u=makeUsage(), history={id:'usage',log:Array.from({length:12},(_,i)=>usageEvent(i+1))};
 u.attach(history);u.scan(history);u.detach(history);
 check('usage disposal releases weak registration and durably stores capacity overflow',()=>{assert.equal(u.sessions.size,0);assert.equal(u.recoveryFiles().length,11);});
 for(let i=0;i<20;i++){await u.flush();await u.tick();}
 check('usage receipts are acknowledged once and RAM fingerprints stay bounded',()=>{
  assert.equal(delivered,12);assert.equal(u.status().pending,0);assert.equal(u.recoveryFiles().length,0);assert(u.seen.cacheStats().entries<=4);assert(u.seen.cacheStats().bytes<=1024);
 });
 const ackHash=crypto.createHash('sha256').update(fs.readFileSync(path.join(usageDir,'acked.jsonl'))).digest('hex');await u.stop();u=makeUsage();
 for(const event of history.log)assert(u.enqueue(usageFromEvent(event,{conversationId:history.id})));
 check('streamed legacy ACK replay retains deduplication and leaves old journal unchanged',()=>{
  assert.equal(u.status().pending,0);assert(u.seen.cacheStats().entries<=4);assert.equal(crypto.createHash('sha256').update(fs.readFileSync(path.join(usageDir,'acked.jsonl'))).digest('hex'),ackHash);
 });
 const agent={session:{id:'timing',log:[]}};u.observeFrame(agent,{type:'start',attemptId:'in-flight'});
 u.observeFrame(agent,{type:'end',attemptId:'in-flight',outcome:{kind:'committed',seq:99}});
 u.observeFrame(agent,{type:'start',attemptId:'abandoned-on-dispose'});u.detach(agent.session);
 check('dispose removes unfinished timing and agent weak registrations',()=>{assert(!u.live.has(agent));assert(!u.timings.size);assert(!u.sessionAgents.has(agent.session));});
 const finalPayload=usageFromEvent(usageEvent(999),{conversationId:'index-write-recovery'}), originalSet=u.seen.set;
 u.seen.set=()=>{throw new Error('synthetic transient index write');};
 assert.throws(()=>u.enqueue(finalPayload),/transient index write/);u.seen.set=originalSet;
 assert(u.enqueue(finalPayload));await u.flush();
 check('a durable usage payload survives a transient identity-index write and retries without restart',()=>assert.equal(u.status().pending,0));
 await u.stop();

 if(process.env.DSH_RUNTIME){
  const mod=name=>import(pathToFileURL(path.join(process.env.DSH_RUNTIME,'node_modules','@deepseek-ai',name,'lib','index.js')).href);
  const [{Context},{SessionStore:NativeStore}]=await Promise.all([mod('cordis'),mod('dsh-session')]);
  const ctx=new Context(), fiber=ctx.plugin(NativeStore);await fiber.await();
  const native=await createAdapter(ctx,{enabled:true,baseUrl:'http://127.0.0.1:1',token:'synthetic-native-token',
   sessionModule:path.join(process.env.DSH_RUNTIME,'node_modules','@deepseek-ai','dsh-session','lib','index.js'),queueDir:path.join(root,'native'),
   fetchImpl:async()=>response(503,{ok:false}),maxPending:1,intervalMs:60000,importantToolEnabled:false,historyEnabled:false});
  instances.push(native);
  const real=ctx.sessions.prepare('native-disposal'), wrapped=real.append, detach=ctx.sessions.enter(real);ctx.sessions.announce(real);
  real.append('user/message',message('native-first'),{surfaceOp:'append'});real.append('user/message',message('native-backlog'),{surfaceOp:'append'});detach();
  check('real SessionStore emits disposed and releases the exact live instance',()=>{
   assert.equal(ctx.sessions.get(real.id),undefined);assert.equal(native.status().capture.attached_sessions,0);assert.notEqual(real.append,wrapped);
   assert.equal(recovery(native.queue.dir).length,1);
  });
  await native.stop();await ctx.fiber.dispose();await ctx.fiber.await();
 }else console.log('SKIP native SessionStore proof: set DSH_RUNTIME.');
 console.log(JSON.stringify({passed,failed:0,checks,entry:path.basename(entry),production_mutations:0,network_model_calls:0}));
}finally{
 for(const value of instances)await value.stop();
 fs.rmSync(root,{recursive:true,force:true});
}
