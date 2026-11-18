/** Bounded delivered-file maintenance: synthetic files, injected filesystem
 * boundaries and durable ACK recovery. No live queue, HTTP service or model.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import {pathToFileURL} from 'node:url';

const queueModule=process.env.MEMORY_BRAIN_QUEUE_TEST_MODULE
  ?pathToFileURL(path.resolve(process.env.MEMORY_BRAIN_QUEUE_TEST_MODULE)).href
  :new URL('../adapters/dsh/durable-queue.mjs',import.meta.url).href;
const {DurableQueue}=await import(queueModule);
const root=fs.mkdtempSync(path.join(os.tmpdir(),'memory-brain-delivered-maintenance-'));
const NOW=Date.UTC(2026,10,18,12),DAY=86400000;
const hash=bytes=>crypto.createHash('sha256').update(bytes).digest('hex');
const ack=()=>({ok:true,status:200,json:async()=>({ok:true,data:{accepted:true}})});
const make=(name,options={})=>new DurableQueue({dir:path.join(root,name),baseUrl:'http://127.0.0.1:1',
  fetchImpl:async()=>ack(),...options}).init();
const row=id=>({conversation_id:'synthetic-maintenance',message_id:id,role:'user',
  text:'Synthetic original.',recorded_at:'2026-11-18T00:00:00.000Z',metadata:{}});
const deferred=()=>{let resolve;const promise=new Promise(value=>{resolve=value;});return {promise,resolve};};
async function timeout(promise,ms=2000){
  let timer;try{return await Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('injected_boundary_not_reached')),ms);})]);}
  finally{clearTimeout(timer);}
}
const deliveredFile=(q,name)=>path.join(q.dir,'delivered',name);
function fileAt(q,name,mtime=NOW-8*DAY){
  const file=deliveredFile(q,name);fs.writeFileSync(file,'{"synthetic":true}');
  fs.utimesSync(file,mtime/1000,mtime/1000);return file;
}
const snapshot=directory=>{
  const found={};if(!fs.existsSync(directory))return found;
  for(const entry of fs.readdirSync(directory,{withFileTypes:true})){
    const full=path.join(directory,entry.name);
    if(entry.isDirectory())for(const [name,digest] of Object.entries(snapshot(full)))found[entry.name+'/'+name]=digest;
    else if(entry.isFile())found[entry.name]=hash(fs.readFileSync(full));
  }
  return found;
};
const checks=[],measurements={};
async function check(name,run){
  try{await run();checks.push({name,passed:true});console.log('PASS '+name);}
  catch(error){checks.push({name,passed:false,error:error.message});console.error('FAIL '+name+': '+error.message);}
}

function virtualDirectory(q,total=3500){
  const folder=path.join(q.dir,'delivered'),names=Array.from({length:total},(_,i)=>String(i).padStart(5,'0')+'.json');
  const original={readdirSync:fs.readdirSync,statSync:fs.statSync,unlinkSync:fs.unlinkSync,
    opendir:fs.promises.opendir,lstat:fs.promises.lstat,unlink:fs.promises.unlink};
  const counts={sync_readdir:0,sync_stat:0,sync_unlink:0,open:0,read:0,async_lstat:0,async_unlink:0,close:0};
  let position=0;
  const inside=value=>path.dirname(String(value))===folder;
  fs.readdirSync=function(value,...args){if(String(value)===folder){counts.sync_readdir++;return names;}return original.readdirSync.call(this,value,...args);};
  fs.statSync=function(value,...args){if(inside(value)){counts.sync_stat++;return {mtimeMs:NOW,isFile:()=>true};}return original.statSync.call(this,value,...args);};
  fs.unlinkSync=function(value,...args){if(inside(value)){counts.sync_unlink++;return;}return original.unlinkSync.call(this,value,...args);};
  fs.promises.opendir=async function(value,...args){
    if(String(value)!==folder)return original.opendir.call(this,value,...args);
    counts.open++;return {async read(){counts.read++;return position<names.length?{name:names[position++],isFile:()=>true}:null;},
      async close(){counts.close++;}};
  };
  fs.promises.lstat=async function(value,...args){if(inside(value)){counts.async_lstat++;return {mtimeMs:NOW,isFile:()=>true};}return original.lstat.call(this,value,...args);};
  fs.promises.unlink=async function(value,...args){if(inside(value)){counts.async_unlink++;return;}return original.unlink.call(this,value,...args);};
  return {counts,get position(){return position;},restore(){Object.assign(fs,{readdirSync:original.readdirSync,statSync:original.statSync,unlinkSync:original.unlinkSync});
    Object.assign(fs.promises,{opendir:original.opendir,lstat:original.lstat,unlink:original.unlink});}};
}

await check('3500 delivered entries perform no synchronous sweep and at most 200 inspections per call',async()=>{
  const q=make('virtual-bound'),v=virtualDirectory(q);
  try{
    const flight=q.sweep(NOW);await flight;
    measurements.first3500Batch={...v.counts};
    assert.ok(flight&&typeof flight.then==='function','sweep must return an awaitable operation');
    assert.equal(v.counts.sync_readdir+v.counts.sync_stat+v.counts.sync_unlink,0);
    assert.ok(v.counts.async_lstat>0&&v.counts.async_lstat<=200);
    assert.ok(v.counts.read<=200);assert.equal(v.counts.open,1);
  }finally{await q.close();v.restore();}
});

await check('a retained directory cursor reaches all 3500 entries without restarting at the beginning',async()=>{
  const q=make('virtual-cursor'),v=virtualDirectory(q);
  try{
    for(let i=0;i<18;i++)await q.sweep(NOW);
    measurements.full3500Pass={...v.counts,visited:v.position};
    assert.equal(v.position,3500);assert.equal(v.counts.async_lstat,3500);
    assert.equal(v.counts.open,1);assert.equal(v.counts.close,1);
    assert.equal(v.counts.async_unlink,0,'fresh entries have no count-based eviction');
  }finally{await q.close();v.restore();}
});

await check('simultaneous maintenance calls share a single open and a single bounded batch',async()=>{
  const q=make('single-flight'),v=virtualDirectory(q);
  try{
    const a=q.sweep(NOW),b=q.sweep(NOW),c=q.sweep(NOW);
    assert.ok(a&&typeof a.then==='function');assert.equal(a,b);assert.equal(b,c);
    await Promise.all([a,b,c]);assert.equal(v.counts.open,1);assert.equal(v.counts.async_lstat,200);
  }finally{await q.close();v.restore();}
});

await check('real files beyond the first 200 expire and fresh delivered files remain without a quantity cap',async()=>{
  const q=make('actual-tail'),old=[];
  try{
    for(let i=0;i<470;i++)old.push(fileAt(q,'old-'+String(i).padStart(4,'0')+'.json'));
    for(let i=0;i<230;i++)fileAt(q,'fresh-'+String(i).padStart(4,'0')+'.json',NOW);
    await q.sweep(NOW);const first=old.filter(file=>!fs.existsSync(file)).length;
    measurements.actualFiles={total:700,expired:470,fresh:230,first_batch_deleted:first};
    assert.ok(first<=200,'a real first batch must not traverse/delete every old file');
    for(let i=0;i<8&&old.some(file=>fs.existsSync(file));i++)await q.sweep(NOW);
    assert.equal(old.filter(file=>fs.existsSync(file)).length,0,'tail entries must eventually be visited');
    assert.equal(fs.readdirSync(path.join(q.dir,'delivered')).filter(name=>name.startsWith('fresh-')).length,230);
  }finally{await q.close();}
});

await check('mtime TTL retains the seven-day boundary and removes only strictly older regular JSON files',async()=>{
  const q=make('mtime-ttl');
  try{
    const boundary=fileAt(q,'boundary.json',NOW-7*DAY),young=fileAt(q,'young.json',NOW-6*DAY);
    const expired=fileAt(q,'expired.json',NOW-8*DAY),text=fileAt(q,'old.txt',NOW-30*DAY);
    const directory=path.join(q.dir,'delivered','folder.json');fs.mkdirSync(directory);fs.writeFileSync(path.join(directory,'sentinel'),'preserve');
    await q.sweep(NOW);
    assert.equal(fs.existsSync(expired),false);assert.equal(fs.existsSync(boundary),true);
    assert.equal(fs.existsSync(young),true);assert.equal(fs.existsSync(text),true);
    assert.equal(fs.readFileSync(path.join(directory,'sentinel'),'utf8'),'preserve');
  }finally{await q.close();}
});

await check('cleanup never follows a delivered directory link to remove another directory contents',async()=>{
  const q=make('directory-link'),target=path.join(root,'owned-link-target');
  fs.mkdirSync(target);const sentinel=path.join(target,'original.json');fs.writeFileSync(sentinel,'preserve');
  const link=deliveredFile(q,'link.json');fs.symlinkSync(target,link,process.platform==='win32'?'junction':'dir');
  try{await q.sweep(NOW+365*DAY);assert.equal(fs.existsSync(link),true);assert.equal(fs.readFileSync(sentinel,'utf8'),'preserve');}
  finally{await q.close();}
});

await check('missing and unreadable delivered files do not abort later entries and only read failure alerts',async()=>{
  const q=make('lstat-errors'),gone=fileAt(q,'gone.json'),denied=fileAt(q,'denied.json'),later=fileAt(q,'later.json');
  const original=fs.promises.lstat;
  fs.promises.lstat=async function(file,...args){if(file===gone)throw Object.assign(new Error('injected gone'),{code:'ENOENT'});
    if(file===denied)throw Object.assign(new Error('injected denied'),{code:'EACCES'});return original.call(this,file,...args);};
  try{await q.sweep(NOW);assert.equal(fs.existsSync(later),false);assert.equal(fs.existsSync(denied),true);
    assert.equal(q.state.alerts,1);assert.equal(q.state.last_error,'queue_delivered_cleanup_file_failed');}
  finally{await q.close();fs.promises.lstat=original;}
});

for(const stage of ['lstat','unlink'])await check('unload waits for issued '+stage+' and closes the cursor before releasing writer ownership',async()=>{
  const q=make('close-'+stage),file=fileAt(q,'expired.json'),started=deferred(),release=deferred();
  const original=fs.promises[stage],originalUnlink=fs.unlinkSync,events=[];
  let flight,closing;
  fs.promises[stage]=async function(value,...args){if(value===file){events.push(stage+'_started');started.resolve();await release.promise;
      const result=await original.call(this,value,...args);events.push(stage+'_finished');return result;}return original.call(this,value,...args);};
  fs.unlinkSync=function(value,...args){if(value===q.lock&&JSON.parse(fs.readFileSync(value,'utf8')).nonce===q.lockNonce)
      events.push('writer_released');return originalUnlink.call(this,value,...args);};
  try{
    flight=q.sweep(NOW);assert.ok(flight&&typeof flight.then==='function');await timeout(started.promise);
    closing=q.close();assert.equal(q.closed,true);assert.ok(fs.existsSync(q.lock),'issued IO still owns the writer lock');
    assert.throws(()=>new DurableQueue({dir:q.dir,baseUrl:q.baseUrl}).init(),/queue_already_in_use/);
    assert.throws(()=>q.enqueue(row('after-close')),/queue_closed/);
    release.resolve();await Promise.all([flight,closing]);assert.equal(fs.existsSync(q.lock),false);
    assert.ok(events.indexOf(stage+'_finished')<events.indexOf('writer_released'));
    assert.equal(fs.existsSync(file),stage==='lstat','closing during lstat must prevent a new unlink');
    const reopened=new DurableQueue({dir:q.dir,baseUrl:q.baseUrl,fetchImpl:async()=>ack()}).init();await reopened.close();
    measurements['unload_'+stage]=events;
  }finally{release.resolve();await Promise.resolve(flight).catch(()=>{});await Promise.resolve(closing).catch(()=>{});await q.close();fs.promises[stage]=original;fs.unlinkSync=originalUnlink;}
});

await check('unload also retains ownership until an already issued directory close completes',async()=>{
  const q=make('close-directory'),v=virtualDirectory(q,1),entered=deferred(),release=deferred();
  const originalOpen=fs.promises.opendir;
  let flight,closing;
  fs.promises.opendir=async function(...args){const dir=await originalOpen.apply(this,args),close=dir.close.bind(dir);
    dir.close=async()=>{entered.resolve();await release.promise;return close();};return dir;};
  try{
    flight=q.sweep(NOW);assert.ok(flight&&typeof flight.then==='function');await timeout(entered.promise);
    closing=q.close();assert.ok(fs.existsSync(q.lock));release.resolve();await Promise.all([flight,closing]);
    assert.equal(fs.existsSync(q.lock),false);assert.equal(v.counts.close,1);
  }finally{release.resolve();await Promise.resolve(flight).catch(()=>{});await Promise.resolve(closing).catch(()=>{});await q.close();fs.promises.opendir=originalOpen;v.restore();}
});

await check('idle close remains synchronous and a closed queue issues no maintenance IO',async()=>{
  const q=make('idle-close'),v=virtualDirectory(q);
  try{assert.equal(q.close(),undefined);assert.equal(fs.existsSync(q.lock),false);
    await q.sweep(NOW);assert.equal(v.counts.open,0);assert.equal(v.counts.read,0);}
  finally{v.restore();}
});

await check('expired delivered payload deletion preserves journals, receipt indexes, pending and failed across restart',async()=>{
  const q=make('ack-recovery'),delivered=row('accepted'),rejected=row('rejected'),pending=row('pending');
  try{
    q.enqueue(delivered);await q.flush();q.fetch=async()=>({ok:false,status:422,json:async()=>({ok:false})});
    q.enqueue(rejected);await q.flush();q.fetch=async()=>({ok:true,status:200,json:async()=>({ok:true})});
    q.enqueue(pending);await q.flush();
    const before={seen:fs.readFileSync(path.join(q.dir,'seen.jsonl')),acked:fs.readFileSync(path.join(q.dir,'acked.jsonl')),
      identity:snapshot(path.join(q.dir,'identity-index')),ack:snapshot(path.join(q.dir,'ack-index')),
      pending:snapshot(path.join(q.dir,'pending')),failed:snapshot(path.join(q.dir,'failed'))};
    for(const name of fs.readdirSync(path.join(q.dir,'delivered')))fs.utimesSync(deliveredFile(q,name),0,0);
    await q.sweep(NOW);assert.equal(fs.readdirSync(path.join(q.dir,'delivered')).length,0);
    assert.deepEqual(fs.readFileSync(path.join(q.dir,'seen.jsonl')),before.seen);assert.deepEqual(fs.readFileSync(path.join(q.dir,'acked.jsonl')),before.acked);
    assert.deepEqual(snapshot(path.join(q.dir,'identity-index')),before.identity);assert.deepEqual(snapshot(path.join(q.dir,'ack-index')),before.ack);
    assert.deepEqual(snapshot(path.join(q.dir,'pending')),before.pending);assert.deepEqual(snapshot(path.join(q.dir,'failed')),before.failed);
  }finally{await q.close();}
  const reopened=make('ack-recovery');
  try{assert.equal(reopened.enqueue(delivered).duplicate,true);assert.equal(await reopened.drainUntil(delivered,0),true);
    assert.equal(reopened.stats().pending,1);assert.equal(reopened.stats().failed_files,1);
    assert.throws(()=>reopened.enqueue({...delivered,text:'Changed synthetic body.'}),/body_changed/);
    measurements.ackAfterRestart={duplicate:true,confirmed:true,pending:1,failed:1};}
  finally{await reopened.close();}
});

const failed=checks.filter(item=>!item.passed).length;
console.log(JSON.stringify({passed:checks.length-failed,failed,total:checks.length,measurements,model_calls:0,live_queue_mutations:0}));
if(failed)process.exitCode=1;
