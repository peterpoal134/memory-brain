/** Lightweight cursor-commit checks: memory-only event batches and a handful
 * of real durable records. No large histories, live queues, models or network.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {DurableQueue} from '../adapters/dsh/durable-queue.mjs';

const adapter=process.env.CAPTURE_CURSOR_ADAPTER_ENTRY
  ?pathToFileURL(path.resolve(process.env.CAPTURE_CURSOR_ADAPTER_ENTRY)).href
  :new URL('../adapters/dsh/xiaojiu-brain.mjs',import.meta.url).href;
const {catchUp}=await import(adapter);
const root=fs.mkdtempSync(path.join(os.tmpdir(),'memory-brain-cursor-batch-'));
const TIME=Date.UTC(2026,10,18,12),checks=[],metrics={};
const message=id=>({id,role:'user',source:{kind:'user'},content:[{type:'text',text:'Synthetic '+id+'.'}]});
const event=(seq,id)=>({seq,time:TIME+seq,type:'user/message',data:message(id)});
const session=(name,log)=>({id:'synthetic-'+name,log});
const identity=item=>`${item.seq}|${item.type}|${item.data?.id||item.data?.message?.id||''}`;
const claimLog=()=>[
  {seq:0,time:TIME,type:'agent/inbox/spliced',data:{target:'next-turn',start:0,inserted:[message('first'),message('second')]}},
  {seq:1,time:TIME+1,type:'agent/inbox/spliced',data:{target:'next-turn',start:0,removedCount:2,inserted:[]}},
  event(2,'third'),
];
const key=payload=>JSON.stringify([payload.conversation_id,payload.message_id,payload.role]);
function memoryQueue(){
  const state={cursor:null,writes:[],accepted:[],seen:new Set(),remaining:Infinity};
  return {state,cursor:()=>state.cursor,saveCursor(_id,value){state.cursor=value;state.writes.push(value);},
    enqueue(payload){const id=key(payload);if(state.seen.has(id))return {duplicate:true};
      if(state.remaining<=0)return null;state.remaining--;state.seen.add(id);state.accepted.push(payload);return {accepted:true};}};
}
const reply=()=>({ok:true,status:200,json:async()=>({ok:true,data:{accepted:true}})});
function realQueue(name,deliveries=[]){
  return new DurableQueue({dir:path.join(root,name),baseUrl:'http://127.0.0.1:1',
    fetchImpl:async(_url,options)=>{deliveries.push(JSON.parse(options.body).message_id);return reply();}}).init();
}
const pendingIds=q=>q.pendingFiles().map(name=>JSON.parse(fs.readFileSync(path.join(q.dir,'pending',name),'utf8')).payload.message_id);
async function check(name,run){
  try{await run();checks.push({name,passed:true});console.log('PASS '+name);}
  catch(error){checks.push({name,passed:false,error:error.message});console.error('FAIL '+name+': '+error.message);}
}

await check('200 non-message events commit the final cursor once without enqueueing originals',()=>{
  const q=memoryQueue(),s=session('metadata',Array.from({length:200},(_,seq)=>({seq,time:TIME+seq,type:'tool/result',data:{synthetic:true}})));
  assert.equal(catchUp(q,s),200);metrics.metadata={events:200,cursor_writes:q.state.writes.length,originals:q.state.accepted.length};
  assert.equal(q.state.accepted.length,0);assert.equal(q.state.writes.length,1);assert.equal(q.state.cursor.index,199);
});

await check('a full 200-message batch retains order and commits its cursor once',()=>{
  const q=memoryQueue(),s=session('messages',Array.from({length:200},(_,seq)=>event(seq,'m'+seq)));
  assert.equal(catchUp(q,s),200);metrics.messages={events:200,cursor_writes:q.state.writes.length,originals:q.state.accepted.length};
  assert.equal(q.state.writes.length,1);assert.deepEqual(q.state.accepted.map(item=>item.message_id),Array.from({length:200},(_,i)=>'m'+i));
});

await check('a capacity gap commits only the last fully captured event and leaves newer input behind it',()=>{
  const q=memoryQueue();q.state.remaining=1;const s=session('capacity',[event(0,'first'),event(1,'second'),event(2,'third')]);
  assert.equal(catchUp(q,s),1);assert.deepEqual(q.state.accepted.map(item=>item.message_id),['first']);
  assert.equal(q.state.cursor.index,0);assert.equal(q.state.writes.length,1);
  q.state.remaining=Infinity;catchUp(q,s);assert.equal(q.state.cursor.index,2);
  assert.deepEqual(q.state.accepted.map(item=>item.message_id),['first','second','third']);
});

await check('capacity refusal before any completed event cannot create or advance a cursor',()=>{
  const q=memoryQueue();q.state.remaining=0;const s=session('no-progress',[event(0,'first')]);
  assert.equal(catchUp(q,s),0);assert.equal(q.state.cursor,null);assert.equal(q.state.writes.length,0);
});

await check('partial success inside one claimed inbox event never advances beyond the unsaved message',()=>{
  const q=memoryQueue();q.state.remaining=1;const s=session('multi-message',claimLog());
  assert.equal(catchUp(q,s),1);assert.equal(q.state.cursor.index,0);
  assert.deepEqual(q.state.accepted.map(item=>item.message_id),['first']);
  q.state.remaining=Infinity;catchUp(q,s);assert.equal(q.state.cursor.index,2);
  assert.deepEqual(q.state.accepted.map(item=>item.message_id),['first','second','third']);
});

await check('a real cursor-write failure reopens from durable receipts without duplicate originals',async()=>{
  const deliveries=[],s=session('cursor-failure',[event(0,'first'),event(1,'second')]);let q=realQueue('cursor-failure',deliveries);
  try{
    q.saveCursor=()=>{throw new Error('synthetic_cursor_write_unavailable');};
    assert.throws(()=>catchUp(q,s),/synthetic_cursor_write_unavailable/);
    assert.ok(pendingIds(q).length>=1);assert.equal(q.cursor(s.id),null);
  }finally{await q.close();}
  q=realQueue('cursor-failure',deliveries);
  try{
    catchUp(q,s);assert.deepEqual(pendingIds(q),['first','second']);assert.equal(q.cursor(s.id).index,1);
    await q.flush();assert.deepEqual(deliveries,['first','second']);assert.equal(q.pendingFiles().length,0);
    metrics.cursorFailureRecovery={originals:deliveries.length,duplicates:deliveries.length-new Set(deliveries).size};
  }finally{await q.close();}
});

await check('enqueue failure after durable publish cannot commit the failed event and replays it safely',async()=>{
  const deliveries=[],s=session('enqueue-published',[event(0,'first'),event(1,'second'),event(2,'third')]);let q=realQueue('enqueue-published',deliveries);
  try{
    const original=q.enqueue.bind(q);q.enqueue=payload=>{const result=original(payload);if(payload.message_id==='second')throw new Error('synthetic_after_durable_enqueue');return result;};
    assert.throws(()=>catchUp(q,s),/synthetic_after_durable_enqueue/);
    assert.ok(q.cursor(s.id)===null||q.cursor(s.id).index===0,'failed event must remain behind the durable cursor');
    assert.deepEqual(pendingIds(q),['first','second']);
  }finally{await q.close();}
  q=realQueue('enqueue-published',deliveries);
  try{catchUp(q,s);assert.equal(q.cursor(s.id).index,2);assert.deepEqual(pendingIds(q),['first','second','third']);
    await q.flush();assert.deepEqual(deliveries,['first','second','third']);}
  finally{await q.close();}
});

await check('enqueue failure during the second message of one event cannot skip that event on restart',async()=>{
  const deliveries=[],s=session('enqueue-partial-event',claimLog());let q=realQueue('enqueue-partial-event',deliveries);
  try{
    const original=q.enqueue.bind(q);q.enqueue=payload=>{if(payload.message_id==='second')throw new Error('synthetic_before_enqueue');return original(payload);};
    assert.throws(()=>catchUp(q,s),/synthetic_before_enqueue/);
    assert.ok(q.cursor(s.id)===null||q.cursor(s.id).index===0,'a partly captured event cannot be committed');
    assert.deepEqual(pendingIds(q),['first']);
  }finally{await q.close();}
  q=realQueue('enqueue-partial-event',deliveries);
  try{catchUp(q,s);assert.equal(q.cursor(s.id).index,2);assert.deepEqual(pendingIds(q),['first','second','third']);
    await q.flush();assert.deepEqual(deliveries,['first','second','third']);}
  finally{await q.close();}
});

await check('captureAfter applies to claim admission while preserving the original inbox timestamp',()=>{
  const deferred=message('deferred'),q=memoryQueue(),s=session('capture-boundary',[
    {...event(0,'old'),time:TIME-10},
    {seq:1,time:TIME-5,type:'agent/inbox/spliced',data:{target:'next-turn',start:0,inserted:[deferred]}},
    {seq:2,time:TIME+1,type:'agent/inbox/spliced',data:{target:'next-turn',start:0,removedCount:1,inserted:[]}},
    {seq:3,time:TIME+2,type:'user/message',data:deferred},
    {...event(4,'new'),time:TIME+3},
  ]);
  assert.equal(catchUp(q,s,{captureAfter:TIME}),5);
  assert.deepEqual(q.state.accepted.map(item=>item.message_id),['deferred','new']);
  assert.equal(q.state.accepted[0].recorded_at,new Date(TIME-5).toISOString());
  assert.equal(q.state.cursor.index,4);assert.equal(q.state.writes.length,1);
});

await check('an already completed log performs no cursor write or enqueue',()=>{
  const q=memoryQueue(),last=event(0,'existing'),s=session('already-done',[last]);
  q.state.cursor={index:0,identity:identity(last)};
  assert.equal(catchUp(q,s),0);assert.equal(q.state.writes.length,0);assert.equal(q.state.accepted.length,0);
});

const failed=checks.filter(item=>!item.passed).length;
console.log(JSON.stringify({passed:checks.length-failed,failed,total:checks.length,metrics,model_calls:0,
  network_calls:0,live_queue_mutations:0,maximum_synthetic_history:200}));
if(failed)process.exitCode=1;
