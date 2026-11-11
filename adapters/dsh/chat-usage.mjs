/** Metadata-only host request ledger. Never wraps a model stream or records text. */
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

const hash=value=>crypto.createHash('sha256').update(value).digest('hex');
const integer=value=>Number.isSafeInteger(value)&&value>=0?value:null;
const label=value=>typeof value==='string'&&value.trim()&&value.length<=256&&!/[\r\n\x00-\x1f]/.test(value)&&!/^\w+:\/\//.test(value)?value:'unknown';
const same=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
const safeRead=file=>JSON.parse(fs.readFileSync(file,'utf8'));
function persist(file,value,exclusive=false){
 fs.mkdirSync(path.dirname(file),{recursive:true,mode:0o700});
 const temporary=file+'.'+process.pid+'-'+crypto.randomBytes(8).toString('hex')+'.tmp';
 const fd=fs.openSync(temporary,'wx',0o600);
 try{fs.writeFileSync(fd,JSON.stringify(value));fs.fsyncSync(fd);}finally{fs.closeSync(fd);}
 try{if(exclusive){fs.linkSync(temporary,file);fs.unlinkSync(temporary);}else fs.renameSync(temporary,file);}
 finally{try{fs.unlinkSync(temporary);}catch{}}
}
function append(file,value){const fd=fs.openSync(file,'a',0o600);try{fs.writeSync(fd,JSON.stringify(value)+'\n');fs.fsyncSync(fd);}finally{fs.closeSync(fd);}}

export function normalizeUsage(usage){
 const raw=usage&&typeof usage==='object'?usage:{};
 const uncached=integer(raw.inputTokens),output=integer(raw.outputTokens),read=integer(raw.cacheReadTokens),write=integer(raw.cacheWriteTokens);
 let total=integer(raw.totalTokens),input=null,basis='partial_unknown';
 if(total!==null&&output!==null&&total>=output){input=total-output;basis='reported_total_minus_output';}
 else if(uncached!==null&&read!==null&&write!==null&&Number.isSafeInteger(uncached+read+write)){
  input=uncached+read+write;basis='disjoint_sum';
  if(total===null&&output!==null&&Number.isSafeInteger(input+output))total=input+output;
 }
 // Do not turn missing optional cache fields into zero. A contradictory count
 // is unknown, rather than clamped into a plausible but invented cache ratio.
 const hit=read!==null&&(input===null||read<=input)?read:null;
 return {input_tokens:input,output_tokens:output,cache_hit_tokens:hit,
  cache_miss_tokens:input!==null&&hit!==null?input-hit:null,total_tokens:total,normalization_basis:basis};
}

function streamMetadata(event){
 const records=Array.isArray(event?.data?.stream)?event.data.stream:[];
 let usage=null,finish=null;
 for(const record of records){
  if(record?.type!=='chunk'||!record.chunk||typeof record.chunk!=='object')continue;
  if(record.chunk.type==='usage'&&record.chunk.usage&&typeof record.chunk.usage==='object')usage=record.chunk.usage;
  if(record.chunk.type==='finish')finish=record.chunk.reason?.kind;
 }
 return {usage:usage||event?.data?.usage||null,finish};
}
function provider(source,header){
 if(header?.official_origin===true)return 'deepseek_official';
 for(const value of [source,header])for(const key of ['baseUrl','baseURL']){
  if(typeof value?.[key]!=='string')continue;
  try{if(new URL(value[key]).origin==='https://api.deepseek.com')return 'deepseek_official';}catch{}
 }
 return label(source?.provider||header?.provider);
}
export function usageFromEvent(event,{conversationId,header={},startedAt=null,captureMode='history'}={}){
 if(!['assistant/message','assistant/attempt'].includes(event?.type)||integer(event.seq)===null)return null;
 const data=event.data||{};
 if(integer(data.turn)===null||integer(data.step)===null||typeof conversationId!=='string'||!conversationId)return null;
 const source=data.message?.source||{};
 if(event.type==='assistant/message'&&source.kind!=='model')return null;
 const stream=streamMetadata(event);
 const outcome=data.interrupted===true||stream.finish==='aborted'?'cancelled':stream.finish==='error'?'error':event.type==='assistant/message'?'success':'unknown';
 return {conversation_id:conversationId,request_id:'dsh-event:'+event.seq,turn_id:String(data.turn),
  model:label(source.model||header.model),provider:provider(source,header),
  started_at:typeof startedAt==='number'&&Number.isFinite(startedAt)?startedAt:null,
  ended_at:typeof event.time==='number'&&Number.isFinite(event.time)?event.time/1000:null,
  outcome,...normalizeUsage(stream.usage),source:'dsh_session_event',source_event_seq:event.seq,capture_mode:captureMode};
}
function requestHeader(event,previous){
 if(event?.type!=='request/header')return previous;
 const input=event.data?.header?.config||{};
 // URL is classified while in memory. It is never put in cursors or payloads.
 const chosen=provider(input,{});
 let official=false;
 for(const key of ['baseUrl','baseURL'])try{if(typeof input[key]==='string'&&new URL(input[key]).origin==='https://api.deepseek.com')official=true;}catch{}
 return {provider:chosen,model:label(input.model),official_origin:official};
}
function identity(payload){return JSON.stringify([payload.conversation_id,payload.request_id]);}
function fingerprint(payload){
 const {started_at,capture_mode,captured_at,...immutable}=payload;
 return hash(JSON.stringify(immutable));
}

export class ChatUsageCollector{
 constructor({dir,baseUrl,token,fetchImpl=globalThis.fetch,timeoutMs=3000,onWarning=()=>{},intervalMs=1000,maxPending=20000}={}){
  this.dir=path.resolve(dir);this.baseUrl=String(baseUrl).replace(/\/+$/,'');this.token=token;this.fetch=fetchImpl;
  this.timeoutMs=Math.max(100,Math.min(15000,Number(timeoutMs)||3000));this.warn=onWarning;this.intervalMs=Math.max(50,Number(intervalMs)||1000);
  this.maxPending=maxPending;this.sessions=new Map();this.position=0;this.seen=new Map();this.live=new WeakMap();this.timings=new Map();
  this.closed=false;this.busy=false;this.retryAt=0;this.failures=0;this.nonce=crypto.randomBytes(16).toString('hex');
  this.counters={enqueued:0,delivered:0,duplicates:0,conflicts:0,abandoned:0,last_error:null};
 }
 init(){
  fs.mkdirSync(this.dir,{recursive:true,mode:0o700});this.lock=path.join(this.dir,'writer.lock');
  if(fs.existsSync(this.lock)){
   const previous=safeRead(this.lock);let alive=true;
   try{process.kill(previous.pid,0);}catch(error){if(error.code==='ESRCH')alive=false;}
   if(alive)throw new Error('chat_usage_writer_in_use');fs.unlinkSync(this.lock);
  }
  persist(this.lock,{pid:process.pid,nonce:this.nonce},true);
  for(const directory of ['pending','failed','cursors'])fs.mkdirSync(path.join(this.dir,directory),{recursive:true,mode:0o700});
  const destination=path.join(this.dir,'backend.json');
  if(fs.existsSync(destination)&&safeRead(destination).backend_sha256!==hash(this.baseUrl)){this.close();throw new Error('chat_usage_backend_changed');}
  if(!fs.existsSync(destination))persist(destination,{backend_sha256:hash(this.baseUrl)});
  const acknowledged=path.join(this.dir,'acked.jsonl');
  if(fs.existsSync(acknowledged))for(const line of fs.readFileSync(acknowledged,'utf8').split('\n')){
   try{const row=JSON.parse(line);if(typeof row.key==='string'&&typeof row.fingerprint==='string')this.seen.set(row.key,row.fingerprint);}catch{}
  }
  for(const directory of ['pending','failed'])for(const name of fs.readdirSync(path.join(this.dir,directory))){
   if(!name.endsWith('.json'))continue;
   try{const row=safeRead(path.join(this.dir,directory,name));if(row.payload&&row.fingerprint===fingerprint(row.payload)&&!this.seen.has(identity(row.payload)))this.seen.set(identity(row.payload),row.fingerprint);}catch{}
  }
  this.timer=setInterval(()=>this.tick().catch(()=>this.alert('chat_usage_tick_failed')),this.intervalMs);this.timer.unref?.();return this;
 }
 alert(code){this.counters.last_error=code;try{this.warn(code);}catch{}}
 pending(){return fs.readdirSync(path.join(this.dir,'pending')).filter(name=>/^[a-f0-9]{64}\.json$/.test(name)).sort();}
 status(){
  try{return {enabled:!this.closed,...this.counters,pending:this.pending().length,failed:fs.readdirSync(path.join(this.dir,'failed')).filter(name=>name.endsWith('.json')).length};}
  catch{return {enabled:!this.closed,...this.counters,pending:null,failed:null,last_error:'chat_usage_storage_unavailable'};}
 }
 attach(session){if(!this.closed&&typeof session?.id==='string'&&Array.isArray(session.log))this.sessions.set(session.id,new WeakRef(session));}
 observeFrame(agent,frame){
  if(this.closed||!agent?.session||!frame)return;
  if(frame.type==='start'){
   let attempts=this.live.get(agent);if(!attempts)this.live.set(agent,attempts=new Map());
   attempts.set(frame.attemptId,{startedAt:Date.now()/1000});
  }else if(frame.type==='end'){
   const attempts=this.live.get(agent),attempt=attempts?.get(frame.attemptId);attempts?.delete(frame.attemptId);
   if(frame.outcome?.kind==='committed'&&integer(frame.outcome.seq)!==null){
    this.timings.set(agent.session.id+'|'+frame.outcome.seq,{startedAt:attempt?.startedAt??null,captureMode:'live'});
    while(this.timings.size>2000)this.timings.delete(this.timings.keys().next().value);
   }else if(frame.outcome?.kind==='abandoned'){this.counters.abandoned++;this.alert('chat_usage_attempt_not_durable');}
   this.attach(agent.session);
  }
 }
 enqueue(payload){
  if(this.closed)return false;
  const key=identity(payload),digest=fingerprint(payload),old=this.seen.get(key);
  if(old){
   if(old===digest){this.counters.duplicates++;return true;}
   persist(path.join(this.dir,'failed',hash(key)+'-conflict-'+crypto.randomBytes(6).toString('hex')+'.json'),
    {key,fingerprint:digest,payload,error:'chat_usage_identity_conflict'},true);
   this.counters.conflicts++;this.alert('chat_usage_identity_conflict');return true;
  }
  if(this.pending().length>=this.maxPending){this.alert('chat_usage_capacity');return false;}
  persist(path.join(this.dir,'pending',hash(key)+'.json'),{key,fingerprint:digest,payload},true);
  this.seen.set(key,digest);this.counters.enqueued++;return true;
 }
 scan(session,{maxEvents=200,maxRequests=50}={}){
  if(this.closed||!Array.isArray(session?.log))return {events:0,requests:0,complete:true};
  const conversation=String(session.id),file=path.join(this.dir,'cursors',hash(conversation)+'.json');
  let cursor;try{cursor=safeRead(file);}catch{cursor={index:-1,seq:null,type:null,header:{}};}
  let index=0,header={};
  if(cursor.index>=0&&session.log[cursor.index]?.seq===cursor.seq&&session.log[cursor.index]?.type===cursor.type){index=cursor.index+1;header=cursor.header||{};}
  let events=0,requests=0;
  for(;index<session.log.length&&events<Math.min(200,maxEvents)&&requests<Math.min(50,maxRequests);index++){
   const event=session.log[index];header=requestHeader(event,header);
   const live=this.timings.get(conversation+'|'+event.seq);
   const payload=usageFromEvent(event,{conversationId:conversation,header,startedAt:live?.startedAt??null,captureMode:live?.captureMode||'history'});
   try{
    if(payload){if(!this.enqueue(payload))break;requests++;this.timings.delete(conversation+'|'+event.seq);}
    persist(file,{index,seq:event.seq,type:event.type,header});
   }catch{this.alert('chat_usage_outbox_write_failed');break;}
   events++;
  }
  return {events,requests,complete:index>=session.log.length};
 }
 async flush(options={}){
  if(this.flushing)return;this.flushing=true;
  try{return await this.flushRecords(options);}finally{this.flushing=false;}
 }
 async flushRecords({maxRequests=50,budgetMs=this.timeoutMs}={}){
  if(this.closed||Date.now()<this.retryAt)return;
  const deadline=Date.now()+budgetMs;
  for(const name of this.pending().slice(0,Math.min(50,maxRequests))){
   if(this.closed||deadline-Date.now()<Math.min(100,this.timeoutMs))break;
   const file=path.join(this.dir,'pending',name);let row;
   try{row=safeRead(file);if(!row.payload||row.key!==identity(row.payload)||row.fingerprint!==fingerprint(row.payload))throw new Error();}
   catch{fs.renameSync(file,path.join(this.dir,'failed',name));this.alert('chat_usage_record_invalid');continue;}
   try{
    const response=await this.fetch(this.baseUrl+'/v1/chat/usage',{method:'POST',headers:{Authorization:'Bearer '+this.token,'Content-Type':'application/json'},
     body:JSON.stringify(row.payload),signal:AbortSignal.timeout(Math.max(1,Math.min(this.timeoutMs,deadline-Date.now()))),redirect:'error'});
    let reply;try{reply=await response.json();}catch{}
    if(this.closed)return;
    const ack=reply?.data;
    if(response.ok&&reply?.ok===true&&(ack?.accepted===true||ack?.duplicate===true)&&ack.request_id===row.payload.request_id
       &&ack.source_event_seq===row.payload.source_event_seq&&(ack.conversation_id===undefined||ack.conversation_id===row.payload.conversation_id)){
     append(path.join(this.dir,'acked.jsonl'),{key:row.key,fingerprint:row.fingerprint});fs.unlinkSync(file);
     this.counters.delivered++;this.failures=0;this.retryAt=0;this.counters.last_error=null;
    }else if([400,401,403,409,422].includes(response.status)){
     const category=response.status===409?'chat_usage_server_conflict':'chat_usage_rejected_'+response.status;
     fs.renameSync(file,path.join(this.dir,'failed',name));this.alert(category);if(response.status===409)this.counters.conflicts++;
    }else{
     this.failures++;this.retryAt=Date.now()+Math.min(60000,500*2**Math.min(7,this.failures));
     this.alert(response.status===404?'chat_usage_endpoint_unavailable':'chat_usage_unconfirmed');break;
    }
   }catch{
    this.failures++;this.retryAt=Date.now()+Math.min(60000,500*2**Math.min(7,this.failures));this.alert('chat_usage_delivery_failed');break;
   }
  }
 }
 async tick(){
  if(this.closed||this.busy)return;this.busy=true;
  try{
   const names=[...this.sessions.keys()];if(names.length){
    const name=names[this.position++%names.length],session=this.sessions.get(name)?.deref();
    if(session)this.scan(session);else this.sessions.delete(name);
   }
   await this.flush();
  }finally{this.busy=false;}
 }
 async importHistory(conversationId,events){
  const session={id:conversationId,log:events};let scanned;
  do{scanned=this.scan(session);await this.flush();if(!scanned.complete)await new Promise(resolve=>setImmediate(resolve));}
  while(!this.closed&&!scanned.complete&&scanned.events>0);
  return {complete:scanned.complete,...this.status()};
 }
 async stop(){
  clearInterval(this.timer);const deadline=Date.now()+this.timeoutMs+250;
  while((this.busy||this.flushing)&&Date.now()<deadline)await new Promise(resolve=>setTimeout(resolve,10));
  this.close();this.sessions.clear();this.timings.clear();
 }
 close(){
  this.closed=true;clearInterval(this.timer);
  try{const lock=safeRead(this.lock);if(lock.pid===process.pid&&lock.nonce===this.nonce)fs.unlinkSync(this.lock);}catch{}
 }
}
