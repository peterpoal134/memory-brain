#!/usr/bin/env node
/** Opt-in, exact-build DSH request snapshot compatibility. No service control. */
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';

const ROOT=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const TARGET='node_modules/@deepseek-ai/dsh-agent-loop/lib/index.js';
const BEFORE='cf57e3ecd487ac5c2553455aaa5cef2c6fb41e6bd61be5aa455675d1d7193033';
const AFTER='05da9a39de1e0109861423eb3617a9cdcfb7fb8cc822feb455337c1f333098f8';
const COMPAT_ID='dsh-request-messages-0.2.1-rc.5';
const MESSAGES={invalid_arguments:'Use --runtime <absolute path> for a read-only check. Writes require --apply or --rollback and --dsh-stopped.',
  absolute_path_required:'The runtime path must be absolute.',
  symlink_refused:'A selected runtime path or target is a symbolic link; no runtime bytes were changed.',
  runtime_unavailable:'The exact supported DSH runtime target is unavailable.',
  manifest_invalid:'The shipped compatibility manifest is invalid or does not name this supported build.',
  unsupported_hash:'This runtime is not one of the two exact supported builds. No best-effort patch is attempted.',
  stopped_confirmation_required:'Stop the DSH host first, then explicitly pass --dsh-stopped. This tool never stops it for you.',
  replacement_invalid:'The small replacement hunks did not reproduce the exact supported after hash.',
  writer_locked:'Another compatibility writer owns the lock; no runtime bytes were changed.',
  runtime_changed:'The target changed during preparation. The concurrent edit was not overwritten.',
  backup_invalid:'The original-byte backup is missing or does not match the supported before hash.',
  own_receipt_required:'Rollback requires this tool\'s valid local receipt and original-byte backup.',
  rollback_target_mismatch:'Rollback requires the target to still match the exact installed after hash.',
  write_failed:'The compatibility operation could not finish. Preserve the local compatibility backup directory.',
  compatibility_failed:'Compatibility stopped without printing file contents or private configuration.'};

export class HookError extends Error{
  constructor(code,details={}){super(MESSAGES[code]||MESSAGES.compatibility_failed);this.code=code;this.details=details;}
}
const fail=(code,details)=>{throw new HookError(code,details);};
const hash=bytes=>crypto.createHash('sha256').update(bytes).digest('hex');
const comparable=value=>process.platform==='win32'?path.normalize(value).toLowerCase():path.normalize(value);
const same=(a,b)=>comparable(path.resolve(a))===comparable(path.resolve(b));
function exists(filename){try{fs.lstatSync(filename);return true;}catch(error){if(error.code==='ENOENT')return false;throw error;}}
function noSymlinks(filename){
  let current=path.resolve(filename);
  for(;;){if(exists(current)&&fs.lstatSync(current).isSymbolicLink())fail('symlink_refused');
    const parent=path.dirname(current);if(parent===current)break;current=parent;}
}

export function loadManifest(){
  let manifest;
  try{manifest=JSON.parse(fs.readFileSync(path.join(ROOT,'compat','dsh-request-messages.json'),'utf8'));}
  catch{fail('manifest_invalid');}
  if(manifest.schema_version!==1||manifest.compat_id!==COMPAT_ID||manifest.target!==TARGET||
    manifest.before_sha256!==BEFORE||manifest.after_sha256!==AFTER||manifest.newline_policy?.before!=='LF'||
    manifest.newline_policy?.after!=='CRLF'||manifest.license?.spdx!=='MIT'||
    !manifest.license?.notice?.includes('Copyright (c) 2026 DeepSeek')||
    !Array.isArray(manifest.changes)||manifest.changes.length!==2||
    manifest.changes.some(change=>typeof change.before!=='string'||typeof change.after!=='string'||change.count!==1))fail('manifest_invalid');
  return manifest;
}

export function patchedBytes(bytes,manifest=loadManifest()){
  if(hash(bytes)!==BEFORE)fail('unsupported_hash',{observed_sha256:hash(bytes)});
  let code=bytes.toString('utf8');
  for(const change of manifest.changes){
    if(code.split(change.before).length-1!==1)fail('replacement_invalid');
    code=code.replace(change.before,change.after);
  }
  const result=Buffer.from(code.replace(/\r?\n/g,'\r\n'),'utf8');
  if(hash(result)!==AFTER)fail('replacement_invalid');
  return result;
}

function readTarget(filename){
  noSymlinks(filename);
  if(!exists(filename)||!fs.lstatSync(filename).isFile())fail('runtime_unavailable');
  return {bytes:fs.readFileSync(filename),mode:fs.lstatSync(filename).mode&0o777};
}

function writeExclusive(filename,bytes,mode=0o600){
  const descriptor=fs.openSync(filename,'wx',mode);
  try{fs.writeFileSync(descriptor,bytes);fs.fsyncSync(descriptor);}finally{fs.closeSync(descriptor);}
}

function replaceAtomic(filename,bytes,mode=0o600){
  const temporary=path.join(path.dirname(filename),'.memory-brain-hook-'+process.pid+'-'+crypto.randomBytes(6).toString('hex')+'.tmp');
  try{writeExclusive(temporary,bytes,mode);fs.renameSync(temporary,filename);}
  finally{try{fs.unlinkSync(temporary);}catch{}}
}

function receiptAt(filename){
  noSymlinks(filename);
  if(!exists(filename))fail('own_receipt_required');
  let value;
  try{value=JSON.parse(fs.readFileSync(filename,'utf8'));}catch{fail('own_receipt_required');}
  if(value.schema_version!==1||value.compat_id!==COMPAT_ID||value.target!==TARGET||
    value.before_sha256!==BEFORE||value.after_sha256!==AFTER||
    value.backup_file!=='request-messages-'+BEFORE+'.before.js'||
    !['prepared','applied','rolled_back'].includes(value.state))fail('own_receipt_required');
  return value;
}

export async function requestHook(options){
  if(!options||typeof options.runtime!=='string'||!path.isAbsolute(options.runtime))fail('absolute_path_required');
  if(options.apply&&options.rollback)fail('invalid_arguments');
  const manifest=loadManifest(),runtime=path.resolve(options.runtime);
  noSymlinks(runtime);
  const target=path.join(runtime,...TARGET.split('/'));
  const initial=readTarget(target),initialHash=hash(initial.bytes);
  const state=initialHash===BEFORE?'patch_required':initialHash===AFTER?'already_patched':'unsupported';
  if(state==='unsupported')fail('unsupported_hash',{observed_sha256:initialHash});
  const base={ok:true,compat_id:COMPAT_ID,state,target:TARGET,observed_sha256:initialHash,
    before_sha256:BEFORE,after_sha256:AFTER,runtime,applied:false,rolled_back:false,
    stopped_confirmation:options.dshStopped===true,services_started:0,services_stopped:0,data_deleted:false};
  if(!options.apply&&!options.rollback)return {...base,action:'check',read_only:true};
  if(options.dshStopped!==true)fail('stopped_confirmation_required');
  if(options.apply&&state==='already_patched')return {...base,action:'noop',read_only:true};
  if(options.rollback&&initialHash!==AFTER)fail('rollback_target_mismatch');
  const directory=path.join(runtime,'.memory-brain-compat');noSymlinks(directory);
  const receiptFile=path.join(directory,'request-messages.json');
  // Rollback does not create a receipt or substitute a reconstructed original.
  let receipt=options.rollback?receiptAt(receiptFile):null;
  if(options.rollback&&!exists(directory))fail('own_receipt_required');
  fs.mkdirSync(directory,{recursive:true,mode:0o700});
  const lock=path.join(directory,'request-hook.lock');noSymlinks(lock);
  let descriptor;
  try{descriptor=fs.openSync(lock,'wx',0o600);fs.writeFileSync(descriptor,String(process.pid)+'\n');fs.fsyncSync(descriptor);}
  catch(error){if(descriptor!==undefined){fs.closeSync(descriptor);try{fs.unlinkSync(lock);}catch{}}
    fail(error.code==='EEXIST'?'writer_locked':'write_failed');}
  let backup;
  try{
    if(hash(readTarget(target).bytes)!==initialHash)fail('runtime_changed');
    backup=path.join(directory,'request-messages-'+BEFORE+'.before.js');noSymlinks(backup);noSymlinks(receiptFile);
    let replacement;
    if(options.rollback){
      receipt=receiptAt(receiptFile);
      if(!exists(backup))fail('backup_invalid');
      replacement=fs.readFileSync(backup);if(hash(replacement)!==BEFORE)fail('backup_invalid');
    }else{
      replacement=patchedBytes(initial.bytes,manifest);
      if(exists(backup)){if(hash(fs.readFileSync(backup))!==BEFORE)fail('backup_invalid');}
      else writeExclusive(backup,initial.bytes,initial.mode||0o600);
      receipt={schema_version:1,compat_id:COMPAT_ID,target:TARGET,before_sha256:BEFORE,after_sha256:AFTER,
        backup_file:path.basename(backup),state:'prepared',prepared_at:new Date().toISOString(),
        stopped_confirmation:true,newline_policy:manifest.newline_policy};
      replaceAtomic(receiptFile,Buffer.from(JSON.stringify(receipt,null,2)+'\n'));
    }
    if(hash(readTarget(target).bytes)!==initialHash)fail('runtime_changed');
    replaceAtomic(target,replacement,initial.mode||0o600);
    const expected=options.rollback?BEFORE:AFTER;
    if(hash(readTarget(target).bytes)!==expected)fail('write_failed');
    receipt.state=options.rollback?'rolled_back':'applied';
    receipt[options.rollback?'rolled_back_at':'applied_at']=new Date().toISOString();
    let receiptWarning;
    try{replaceAtomic(receiptFile,Buffer.from(JSON.stringify(receipt,null,2)+'\n'));}
    catch{receiptWarning='receipt_update_failed_after_commit';}
    return {...base,state:options.rollback?'patch_required':'already_patched',
      action:options.rollback?'rollback':'apply',read_only:false,applied:!options.rollback,
      rolled_back:options.rollback,result_sha256:expected,backup,receipt:receiptFile,
      ...(receiptWarning?{warning:receiptWarning}:{})};
  }catch(error){if(error instanceof HookError)throw error;fail('write_failed');}
  finally{if(descriptor!==undefined)fs.closeSync(descriptor);try{fs.unlinkSync(lock);}catch{}}
}

function parseArgs(argv){
  const options={};
  for(let i=0;i<argv.length;i++){
    const key=argv[i];
    if(key==='--runtime'){if(options.runtime||!argv[i+1]||argv[i+1].startsWith('--'))fail('invalid_arguments');options.runtime=argv[++i];}
    else if(['--apply','--rollback','--dsh-stopped'].includes(key)){
      const field={'--apply':'apply','--rollback':'rollback','--dsh-stopped':'dshStopped'}[key];
      if(options[field])fail('invalid_arguments');options[field]=true;
    }else fail('invalid_arguments');
  }
  if(!options.runtime)fail('invalid_arguments');return options;
}

async function main(){
  try{console.log(JSON.stringify(await requestHook(parseArgs(process.argv.slice(2)))));}
  catch(error){const known=error instanceof HookError,code=known?error.code:'compatibility_failed';
    console.log(JSON.stringify({ok:false,error:code,message:MESSAGES[code],...(known?error.details:{}),
      services_started:0,services_stopped:0,data_deleted:false}));process.exitCode=1;}
}
if(process.argv[1]&&same(fileURLToPath(import.meta.url),process.argv[1]))await main();
