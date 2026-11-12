/** Crash-tail and write-failure recovery. Synthetic metadata, no live/model API. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { ChatUsageCollector, usageFromEvent } from '../adapters/dsh/chat-usage.mjs';

const root=fs.mkdtempSync(path.join(os.tmpdir(),'usage-crash-recovery-'));
const instances=[],checks=[],warnings=[];let serial=0;
const event=seq=>({type:'assistant/message',seq,time:1700000000000+seq,data:{turn:1,step:1,
 message:{id:'answer-'+seq,role:'assistant',source:{kind:'model',model:'synthetic'},content:[{type:'text',text:'not retained in usage'}]},
 usage:{inputTokens:5,outputTokens:2,totalTokens:7},stream:[]}});
const payload=(seq,conversationId='synthetic')=>usageFromEvent(event(seq),{conversationId});
const walk=dir=>fs.readdirSync(dir,{withFileTypes:true}).flatMap(row=>row.isDirectory()?walk(path.join(dir,row.name)):[path.join(dir,row.name)]);
const check=(name,fn)=>{fn();checks.push(name);console.log('PASS '+name);};
function setup(name){
 const dir=path.join(root,name),records=new Map();let attempts=0;
 const open=()=>{
  const value=new ChatUsageCollector({dir,baseUrl:'http://127.0.0.1:1',token:'synthetic-recovery-token',intervalMs:60000,
   timeoutMs:200,onWarning:code=>warnings.push(code),fetchImpl:async(_url,init)=>{
    const body=JSON.parse(init.body),key=JSON.stringify([body.conversation_id,body.request_id]);attempts++;
    const duplicate=records.has(key);if(!duplicate)records.set(key,body);
    return {ok:true,status:200,json:async()=>({ok:true,data:{accepted:!duplicate,duplicate,
     request_id:body.request_id,source_event_seq:body.source_event_seq,conversation_id:body.conversation_id}})};
   }});
  instances.push(value);return value.init();
 };
 return {dir,open,records,get attempts(){return attempts;}};
}
function faultFile(target,kind,run){
 const originalOpen=fs.openSync,originalWrite=fs.writeSync,originalSync=fs.fsyncSync,descriptors=new Set();let calls=0;
 fs.openSync=function(file,...args){const fd=originalOpen.call(this,file,...args);if(target(String(file)))descriptors.add(fd);return fd;};
 fs.writeSync=function(fd,buffer,...args){
  if(kind==='partial'&&descriptors.has(fd)){
   calls++;if(calls===1)return originalWrite.call(this,fd,Buffer.from(buffer).subarray(0,3));
   throw Object.assign(new Error('synthetic interrupted append'),{code:'EIO'});
  }
  return originalWrite.call(this,fd,buffer,...args);
 };
 fs.fsyncSync=function(fd){if(kind==='fsync'&&descriptors.has(fd))throw Object.assign(new Error('synthetic fsync failure'),{code:'EIO'});return originalSync.call(this,fd);};
 return Promise.resolve().then(run).finally(()=>{fs.openSync=originalOpen;fs.writeSync=originalWrite;fs.fsyncSync=originalSync;});
}
try{
 const tail=setup('tail'),first=tail.open();first.enqueue(payload(1));await first.flush();first.enqueue(payload(2));await first.stop();
 const ackFile=path.join(tail.dir,'acked.jsonl');fs.appendFileSync(ackFile,'{"key":');const damaged=fs.readFileSync(ackFile);
 const resumed=tail.open();
 check('torn ACK tail is quarantined before valid-prefix replay and grants no incomplete ACK',()=>{
  assert(walk(tail.dir).some(file=>file!==ackFile&&fs.readFileSync(file).equals(damaged)),'an exact damaged-journal copy must survive');
  assert.equal(resumed.status().pending,1);assert.equal(resumed.acknowledged.has(JSON.stringify(['synthetic','dsh-event:2'])),false);
  assert(warnings.includes('chat_usage_ack_journal_tail_recovered'));
 });
 assert(resumed.enqueue(payload(1)));await resumed.flush();await resumed.stop();
 const replay=tail.open();replay.enqueue(payload(1));replay.enqueue(payload(2));await replay.flush();
 check('torn-tail restart and replays do not create duplicate usage records',()=>{assert.equal(tail.records.size,2);assert.equal(tail.attempts,2);assert.equal(replay.status().pending,0);});
 await replay.stop();

 const invalid=setup('complete-bad-line'),before=invalid.open();before.enqueue(payload(3));await before.flush();await before.stop();
 const brokenFile=path.join(invalid.dir,'acked.jsonl');fs.appendFileSync(brokenFile,'not-json\n');const brokenBytes=fs.readFileSync(brokenFile);
 check('a malformed complete ACK line stays fail-closed and releases its writer lock',()=>{
  assert.throws(()=>invalid.open(),/jsonl_record_invalid/);assert(fs.readFileSync(brokenFile).equals(brokenBytes));assert(!fs.existsSync(path.join(invalid.dir,'writer.lock')));
 });

 const missingLf=setup('valid-no-newline'),old=missingLf.open();old.enqueue(payload(4));await old.flush();await old.stop();
 const lfFile=path.join(missingLf.dir,'acked.jsonl'),validBytes=fs.readFileSync(lfFile);assert.equal(validBytes.at(-1),10);fs.writeFileSync(lfFile,validBytes.subarray(0,-1));
 const lf=missingLf.open();lf.enqueue(payload(5));await lf.flush();await lf.stop();const reopened=missingLf.open();
 check('a complete final JSON record without LF is not glued to the next append',()=>{
  const rows=fs.readFileSync(lfFile,'utf8').trim().split('\n').map(line=>JSON.parse(line));assert.equal(rows.length,2);
  reopened.enqueue(payload(4));reopened.enqueue(payload(5));assert.equal(reopened.status().pending,0);assert.equal(missingLf.records.size,2);
 });
 await reopened.stop();

 const disk=setup('persist-fsync'),u=disk.open();
 await faultFile(file=>file.includes(path.sep+'pending'+path.sep)&&file.endsWith('.tmp'),'fsync',()=>assert.throws(()=>u.enqueue(payload(6)),/synthetic fsync failure/));
 check('usage persist fsync failure leaves no temporary or partly published pending file',()=>{
  assert(!walk(disk.dir).some(file=>file.endsWith('.tmp')));assert.equal(u.status().pending,0);
 });
 u.enqueue(payload(6));await u.flush();await u.stop();

 const partial=setup('partial-append'),p=partial.open();p.enqueue(payload(7));
 await faultFile(file=>file===path.join(partial.dir,'acked.jsonl'),'partial',()=>p.flush());
 check('a short/failed ACK append keeps its durable pending payload and schedules journal recovery',()=>{
  assert.equal(p.status().pending,1);assert.equal(p.journalRecoveryRequired,true);assert.equal(partial.records.size,1);
 });
 p.retryAt=0;await p.flush();await p.stop();const after=partial.open();after.enqueue(payload(7));await after.flush();
 check('runtime recovery after a torn ACK retries the same identity without double counting',()=>{
  assert.equal(partial.records.size,1);assert.equal(after.status().pending,0);
  assert.equal(fs.readFileSync(path.join(partial.dir,'acked.jsonl'),'utf8').trim().split('\n').map(JSON.parse).length,1);
  assert(!walk(partial.dir).some(file=>file.endsWith('.tmp')));
 });
 await after.stop();

 const timing=setup('late-instance'),t=timing.open(),older={id:'reused',log:[event(8)]},newer={id:'reused',log:[event(8)]},agent={session:newer};
 t.observeFrame(agent,{type:'start',attemptId:'new-instance'});t.observeFrame(agent,{type:'end',attemptId:'new-instance',outcome:{kind:'committed',seq:8}});
 const timingKey='reused|8',liveTiming=t.timings.get(timingKey),started=liveTiming.startedAt;t.detach(older);
 check('late same-ID old instance cannot consume the new instance live start timing',()=>{
  assert.equal(t.timings.get(timingKey),liveTiming);assert.equal(t.timings.get(timingKey).startedAt,started);assert.equal(liveTiming.sessionRef.deref(),newer);
 });
 await t.stop();
 console.log(JSON.stringify({passed:checks.length,failed:0,checks,remote_model_calls:0,production_mutations:0}));
}finally{
 for(const value of instances)await value.stop();
 assert(path.resolve(root).startsWith(path.resolve(os.tmpdir())+path.sep));fs.rmSync(root,{recursive:true,force:true});
}
