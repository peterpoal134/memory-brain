/** Exact-build compatibility tests on temporary copies, never the real runtime. */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { loadManifest,patchedBytes,requestHook,HookError } from '../scripts/dsh-request-hook.mjs';

const realRuntime=process.env.DSH_RUNTIME;
if(!realRuntime||!path.isAbsolute(realRuntime))throw new Error('Set DSH_RUNTIME to a supported read-only DSH installation.');
const manifest=loadManifest(),hash=bytes=>crypto.createHash('sha256').update(bytes).digest('hex');
const realTarget=path.join(realRuntime,...manifest.target.split('/')),realBefore=fs.readFileSync(realTarget);
let baseline=realBefore;
if(hash(realBefore)===manifest.after_sha256){
  let code=realBefore.toString('utf8').replace(/\r\n/g,'\n');
  for(const change of [...manifest.changes].reverse()){
    assert.equal(code.split(change.after).length-1,1);code=code.replace(change.after,change.before);
  }
  baseline=Buffer.from(code,'utf8');
}
assert.equal(hash(baseline),manifest.before_sha256,'real runtime must be this one supported build');
const after=patchedBytes(baseline);
const root=fs.mkdtempSync(path.join(os.tmpdir(),'memory-brain-hook-'));
let passed=0,failed=0;
const test=async(name,fn)=>{try{await fn();passed++;console.log('PASS '+name);}catch(error){failed++;console.error('FAIL '+name+': '+error.stack);}};
function fixture(name,bytes=baseline){
  const runtime=path.join(root,name),target=path.join(runtime,...manifest.target.split('/'));
  fs.mkdirSync(path.dirname(target),{recursive:true});fs.writeFileSync(target,bytes);
  fs.mkdirSync(path.join(runtime,'data'));fs.writeFileSync(path.join(runtime,'data','keep.txt'),'USER_DATA_UNTOUCHED');
  return {runtime,target};
}
const rejects=(options,code)=>assert.rejects(()=>requestHook(options),error=>error instanceof HookError&&error.code===code);
try{
  await test('known original check is read only and creates no backups or locks',async()=>{
    const one=fixture('check');const result=await requestHook({runtime:one.runtime});
    assert.equal(result.state,'patch_required');assert.equal(result.read_only,true);
    assert.deepEqual(fs.readFileSync(one.target),baseline);
    assert.equal(fs.existsSync(path.join(one.runtime,'.memory-brain-compat')),false);
  });
  await test('two narrow hunks plus declared newline policy reproduce exact after bytes',async()=>{
    assert.equal(manifest.changes.length,2);assert.equal(hash(after),manifest.after_sha256);
    assert(after.includes(Buffer.from('decision.requestMessages')));
    assert(after.includes(Buffer.from('withRequestMessages(request, messages)')));
    assert.equal(after.toString('utf8').split('\n').length-1,after.toString('utf8').split('\r\n').length-1);
    assert(manifest.license.notice.includes('Permission is hereby granted'));
    assert(!JSON.stringify(manifest).includes('E:/'));assert(!JSON.stringify(manifest).includes('C:/'));
    assert(JSON.stringify(manifest).length<10000);
  });
  await test('writes require explicit stopped acknowledgement and never stop services',async()=>{
    const one=fixture('stop-gate');await rejects({runtime:one.runtime,apply:true},'stopped_confirmation_required');
    assert.deepEqual(fs.readFileSync(one.target),baseline);
    assert.equal(fs.existsSync(path.join(one.runtime,'.memory-brain-compat')),false);
  });
  await test('explicit apply creates exact backup and valid own receipt',async()=>{
    const one=fixture('apply');const result=await requestHook({runtime:one.runtime,apply:true,dshStopped:true});
    assert.equal(result.applied,true);assert.equal(result.services_started,0);assert.equal(result.services_stopped,0);
    assert.deepEqual(fs.readFileSync(one.target),after);assert.deepEqual(fs.readFileSync(result.backup),baseline);
    const receipt=JSON.parse(fs.readFileSync(result.receipt,'utf8'));assert.equal(receipt.state,'applied');
    assert.equal(receipt.before_sha256,manifest.before_sha256);assert.equal(receipt.after_sha256,manifest.after_sha256);
  });
  await test('reapply is idempotent and preserves receipt and original backup bytes',async()=>{
    const one=fixture('again');const first=await requestHook({runtime:one.runtime,apply:true,dshStopped:true});
    const receipt=fs.readFileSync(first.receipt),backup=fs.readFileSync(first.backup);
    const result=await requestHook({runtime:one.runtime,apply:true,dshStopped:true});assert.equal(result.action,'noop');
    assert.deepEqual(fs.readFileSync(first.receipt),receipt);assert.deepEqual(fs.readFileSync(first.backup),backup);
    assert.deepEqual(fs.readFileSync(one.target),after);
  });
  await test('rollback restores exact LF original with own receipt and keeps data',async()=>{
    const one=fixture('rollback');const applied=await requestHook({runtime:one.runtime,apply:true,dshStopped:true});
    await rejects({runtime:one.runtime,rollback:true},'stopped_confirmation_required');
    const result=await requestHook({runtime:one.runtime,rollback:true,dshStopped:true});
    assert.equal(result.rolled_back,true);assert.deepEqual(fs.readFileSync(one.target),baseline);
    assert.equal(fs.readFileSync(path.join(one.runtime,'data','keep.txt'),'utf8'),'USER_DATA_UNTOUCHED');
    assert.equal(JSON.parse(fs.readFileSync(applied.receipt,'utf8')).state,'rolled_back');
    await rejects({runtime:one.runtime,rollback:true,dshStopped:true},'rollback_target_mismatch');
  });
  await test('already patched target without own receipt is not eligible for rollback',async()=>{
    const one=fixture('no-receipt',after);await rejects({runtime:one.runtime,rollback:true,dshStopped:true},'own_receipt_required');
    assert.deepEqual(fs.readFileSync(one.target),after);
    assert.equal(fs.existsSync(path.join(one.runtime,'.memory-brain-compat')),false);
  });
  await test('unknown hash is refused for check apply and rollback without modifying bytes',async()=>{
    const unknown=Buffer.from('// UNKNOWN BUILD\n'),one=fixture('unknown',unknown);
    for(const options of [{},{apply:true,dshStopped:true},{rollback:true,dshStopped:true}]){
      await rejects({runtime:one.runtime,...options},'unsupported_hash');assert.deepEqual(fs.readFileSync(one.target),unknown);
    }
    assert.equal(fs.existsSync(path.join(one.runtime,'.memory-brain-compat')),false);
  });
  await test('modified installed target and corrupt backups are never overwritten on rollback',async()=>{
    const one=fixture('tampered'),result=await requestHook({runtime:one.runtime,apply:true,dshStopped:true});
    fs.writeFileSync(result.backup,'corrupt original');
    await rejects({runtime:one.runtime,rollback:true,dshStopped:true},'backup_invalid');assert.deepEqual(fs.readFileSync(one.target),after);
    fs.writeFileSync(one.target,Buffer.concat([after,Buffer.from('//external change\n')]));const external=fs.readFileSync(one.target);
    await rejects({runtime:one.runtime,rollback:true,dshStopped:true},'unsupported_hash');assert.deepEqual(fs.readFileSync(one.target),external);
  });
  await test('CAS refuses a concurrent target edit and releases its own lock',async()=>{
    const one=fixture('cas'),originalRead=fs.readFileSync;let reads=0;
    const external=Buffer.from('// EXTERNAL EDIT MUST SURVIVE\n');
    fs.readFileSync=function(filename,...args){
      if(String(filename)===one.target&&++reads===2)fs.writeFileSync(filename,external);
      return originalRead.call(fs,filename,...args);
    };
    try{await rejects({runtime:one.runtime,apply:true,dshStopped:true},'runtime_changed');}
    finally{fs.readFileSync=originalRead;}
    assert.deepEqual(fs.readFileSync(one.target),external);
    assert.equal(fs.existsSync(path.join(one.runtime,'.memory-brain-compat','request-hook.lock')),false);
  });
  await test('another writer and symbolic-link runtime are refused',async()=>{
    const one=fixture('locked'),directory=path.join(one.runtime,'.memory-brain-compat');fs.mkdirSync(directory);
    fs.writeFileSync(path.join(directory,'request-hook.lock'),'other');
    await rejects({runtime:one.runtime,apply:true,dshStopped:true},'writer_locked');assert.deepEqual(fs.readFileSync(one.target),baseline);
    const link=path.join(root,'linked-runtime');fs.symlinkSync(one.runtime,link,process.platform==='win32'?'junction':'dir');
    await rejects({runtime:link,apply:true,dshStopped:true},'symlink_refused');
  });
  await test('CLI default outputs a read-only JSON check and leaves real runtime bytes unchanged',async()=>{
    const one=fixture('cli'),script=path.resolve('scripts/dsh-request-hook.mjs');
    const child=spawnSync(process.execPath,[script,'--runtime',one.runtime],{encoding:'utf8',windowsHide:true});
    assert.equal(child.status,0);assert.equal(child.stderr,'');const result=JSON.parse(child.stdout.trim());
    assert.equal(result.action,'check');assert.equal(result.read_only,true);assert.equal(result.applied,false);
    assert.deepEqual(fs.readFileSync(realTarget),realBefore);
  });
}finally{
  const target=path.resolve(root);assert.equal(path.dirname(target),path.resolve(os.tmpdir()));
  assert(path.basename(target).startsWith('memory-brain-hook-'));fs.rmSync(target,{recursive:true,force:true});
}
console.log(JSON.stringify({passed,failed,production_mutations:0,real_model_calls:0}));
if(failed)process.exitCode=1;
