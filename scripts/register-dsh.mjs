#!/usr/bin/env node
/** Register one owned Cordis entry. Never edits a runtime or launches a model. */
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';

const SCRIPT_ROOT=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const ENTRY_ID='memory-brain';
const LEGACY_IDS=new Set(['fish-self','fish-rawqueue','fish-rawrecall','fish-instant',
  'persona-memory','dsh-persona-memory','xiaojiu-brain']);
const MESSAGES={invalid_arguments:'Use absolute --runtime and --profile-patch paths; --apply writes, --remove uninstalls only this entry.',
  absolute_path_required:'All selected installation and profile paths must be absolute.',
  symlink_refused:'A selected path or its existing parent is a symbolic link; no files were changed.',
  invalid_profile_path:'The profile patch must be a .yml or .yaml file in an existing directory.',
  runtime_unavailable:'The selected DSH installation or required local modules are unavailable.',
  request_hook_missing:'This DSH runtime does not consume requestMessages. Registration was refused; the runtime was not patched.',
  package_entry_missing:'The public memory-brain plugin entry is missing from this package.',
  profile_parse_failed:'The profile is not a supported Cordis YAML patch sequence; no files were changed.',
  profile_ambiguous:'The profile has duplicate or overridden memory-brain records; resolve them before registration.',
  bundle_registration_conflict:'This profile already selects a bundle containing memory-brain. Use the bundle or direct registration, not both.',
  entry_conflict:'The memory-brain ID belongs to another plugin path; it was not replaced.',
  mixed_configuration:'The existing registration points to different runtime/configuration settings; it was not overwritten.',
  legacy_memory_conflict:'An existing memory plugin may still be active. Disable it explicitly before installing this adapter.',
  config_missing:'Run the brain installer first; its local private/config.json is required before applying registration.',
  config_invalid:'The local brain configuration is invalid; its values were not printed.',
  profile_changed:'The profile changed during registration. The concurrent edit was preserved.',
  profile_locked:'The profile is currently locked by another writer. Try after the writer finishes.',
  write_failed:'Registration could not be committed. Keep the reported backup and inspect local file permissions.',
  registration_failed:'Registration stopped before completion; no private configuration values were printed.'};

export class RegistrationError extends Error {
  constructor(code){super(MESSAGES[code]||MESSAGES.registration_failed);this.code=code;}
}
const fail=code=>{throw new RegistrationError(code);};
const digest=bytes=>crypto.createHash('sha256').update(bytes).digest('hex');
const comparable=value=>process.platform==='win32'?path.normalize(value).toLowerCase():path.normalize(value);
function samePath(a,b){return typeof a==='string'&&typeof b==='string'&&comparable(path.resolve(a))===comparable(path.resolve(b));}
function exists(filename){try{fs.lstatSync(filename);return true;}catch(error){if(error.code==='ENOENT')return false;throw error;}}

export function assertNoSymlink(filename){
  let current=path.resolve(filename);
  for(;;){
    if(exists(current)&&fs.lstatSync(current).isSymbolicLink())fail('symlink_refused');
    const parent=path.dirname(current);if(parent===current)break;current=parent;
  }
}

export function supportsRequestMessages(source){
  return typeof source==='string'&&/\bwithRequestMessages\s*\(/.test(source)&&
    /this\.withRequestMessages\s*\(/.test(source)&&/decision\.requestMessages\b/.test(source);
}

function parseArgs(argv){
  const options={};
  for(let i=0;i<argv.length;i++){
    const key=argv[i];
    if(key==='--apply'||key==='--remove'){
      const name=key.slice(2);if(options[name])fail('invalid_arguments');options[name]=true;
    }else if(['--runtime','--profile-patch','--package-root'].includes(key)){
      if(!argv[i+1]||argv[i+1].startsWith('--'))fail('invalid_arguments');
      const name={'--runtime':'runtime','--profile-patch':'profilePatch','--package-root':'packageRoot'}[key];
      if(options[name])fail('invalid_arguments');options[name]=argv[++i];
    }else fail('invalid_arguments');
  }
  if(!options.runtime||!options.profilePatch)fail('invalid_arguments');
  return options;
}

async function dependencies(runtime){
  try{
    const require=createRequire(path.join(runtime,'package.json'));
    const yaml=require('yaml'),jsYaml=require('js-yaml');
    const include=await import(pathToFileURL(require.resolve('@deepseek-ai/cordis-plugin-include')).href);
    if(typeof yaml.parseDocument!=='function'||!include.entryListSchema)fail('runtime_unavailable');
    return {yaml,jsYaml,schema:include.entryListSchema};
  }catch(error){if(error instanceof RegistrationError)throw error;fail('runtime_unavailable');}
}

function readProfile(filename){
  assertNoSymlink(filename);
  if(!exists(filename))return {exists:false,bytes:Buffer.alloc(0),mode:0o600};
  const stat=fs.lstatSync(filename);if(!stat.isFile())fail('invalid_profile_path');
  return {exists:true,bytes:fs.readFileSync(filename),mode:stat.mode&0o777};
}

function parseProfile(before,deps){
  const text=before.exists?before.bytes.toString('utf8'):'[]\n';
  try{
    const document=deps.yaml.parseDocument(text,{customTags:[{
      tag:'tag:yaml.org,2002:js',resolve:value=>value}],keepSourceTokens:true});
    if(document.errors.length||!deps.yaml.isSeq(document.contents))fail('profile_parse_failed');
    const rows=deps.jsYaml.load(text,{schema:deps.schema});
    if(!Array.isArray(rows)||rows.some(row=>!row||typeof row!=='object'||Array.isArray(row)))fail('profile_parse_failed');
    return {document,rows,text};
  }catch(error){if(error instanceof RegistrationError)throw error;fail('profile_parse_failed');}
}

function insertedRecords(rows,parentDisabled=false,answer=[]){
  for(const row of rows){
    if(!row||typeof row!=='object'||Array.isArray(row))fail('profile_parse_failed');
    if(Object.hasOwn(row,'insert')){
      if(!Array.isArray(row.insert))fail('profile_parse_failed');
      for(const entry of row.insert){
        if(!entry||typeof entry!=='object'||Array.isArray(entry))fail('profile_parse_failed');
        const record={entry,parentDisabled,inserted:true};answer.push(record);
        if(entry.group&&Array.isArray(entry.config)){
          // Group children are entries, not another profile patch layer.
          insertedRecords([{insert:entry.config}],parentDisabled||entry.disabled===true,answer);
        }
      }
    }else answer.push({entry:row,parentDisabled,inserted:false});
  }
  return answer;
}

function inheritedRecords(runtime,profilePatch,deps){
  const profile=path.dirname(profilePatch),manifestPath=path.join(profile,'package.json');
  const inherited=[],home=[];
  if(exists(manifestPath)){
    assertNoSymlink(manifestPath);
    let manifest;
    try{manifest=JSON.parse(fs.readFileSync(manifestPath,'utf8').replace(/^\uFEFF/,''));}
    catch{fail('profile_parse_failed');}
    const bundles=manifest.dsh?.profile?.bundles||[];
    if(!Array.isArray(bundles)||bundles.some(name=>typeof name!=='string'||!/^(@[a-zA-Z0-9_.-]+\/)?[a-zA-Z0-9_.-]+$/.test(name)))fail('profile_parse_failed');
    const runtimeRequire=createRequire(path.join(runtime,'package.json'));
    let installationAnchor=path.join(runtime,'package.json');
    try{installationAnchor=runtimeRequire.resolve('@deepseek-ai/dsh/package.json');}catch{}
    for(const name of bundles){
      let directory;
      for(const anchor of [installationAnchor,path.join(runtime,'package.json'),manifestPath]){
        for(const base of createRequire(anchor).resolve.paths(name)||[]){
          const candidate=path.join(base,name);
          if(exists(path.join(candidate,'package.json'))){directory=candidate;break;}
        }
        if(directory)break;
      }
      if(!directory)fail('runtime_unavailable');
      let bundle;
      try{bundle=JSON.parse(fs.readFileSync(path.join(directory,'package.json'),'utf8')).dsh?.bundle;}
      catch{fail('profile_parse_failed');}
      const files=typeof bundle?.patch==='string'?[bundle.patch]:bundle?.patch;
      if(!Array.isArray(files)||files.some(file=>typeof file!=='string'))fail('profile_parse_failed');
      for(const file of files){
        const filename=path.resolve(directory,file),relative=path.relative(directory,filename);
        if(relative==='..'||relative.startsWith('..'+path.sep)||path.isAbsolute(relative))fail('profile_parse_failed');
        let bytes;
        try{bytes=fs.readFileSync(filename);}catch{fail('runtime_unavailable');}
        // Deliberately do not call loadProfileDirectory: that host helper can
        // rewrite retired bundle declarations even during a purported read.
        inherited.push(...insertedRecords(parseProfile({exists:true,bytes},deps).rows));
      }
    }
  }
  if(path.basename(path.dirname(profile)).toLowerCase()==='profiles'){
    const filename=path.join(path.dirname(path.dirname(profile)),'cordis.patch.yml');
    if(exists(filename)&&!samePath(filename,profilePatch))home.push(...insertedRecords(parseProfile(readProfile(filename),deps).rows));
  }
  return {inherited,home};
}

function isLegacy(entry){
  const id=String(entry.id||'').toLowerCase(),name=String(entry.name||'').toLowerCase();
  return LEGACY_IDS.has(id)||[...LEGACY_IDS].some(item=>name.includes(item));
}

function conflicts(records,ownedEntryPath){
  const effective=new Map(),anonymous=[];
  for(const record of records){
    const entry=record.entry;
    if(record.inserted){
      const state={...entry,parentDisabled:record.parentDisabled};
      if(entry.id)effective.set(entry.id,state);else anonymous.push(state);
    }else if(entry.id){
      const old=effective.get(entry.id)||{id:entry.id};
      effective.set(entry.id,{...old,...entry});
    }
  }
  // The caller has already checked ID ownership, overrides and exact path.
  // A public entry installed under a folder named after the old project is
  // still this entry; directory text alone must not turn it into a duplicate.
  return [...effective.values(),...anonymous].filter(row=>
    !(row.id===ENTRY_ID&&entryMatches(row.name,ownedEntryPath))&&
    isLegacy(row)&&row.disabled!==true&&!row.parentDisabled)
    .map(row=>String(row.id||'unnamed_memory_plugin'));
}

function entryMatches(name,target){
  if(typeof name!=='string')return false;
  try{return name.startsWith('file:')&&samePath(fileURLToPath(name),target);}catch{return false;}
}

function removeOwnedAST(document,deps){
  let removed=0;
  const visitEntries=sequence=>{
    for(let index=sequence.items.length-1;index>=0;index--){
      const node=sequence.items[index];if(!deps.yaml.isMap(node))continue;
      if(node.get('id')===ENTRY_ID){sequence.delete(index);removed++;continue;}
      if(node.get('group')===true&&deps.yaml.isSeq(node.get('config',true)))visitEntries(node.get('config',true));
    }
  };
  for(let index=document.contents.items.length-1;index>=0;index--){
    const row=document.contents.items[index];if(!deps.yaml.isMap(row))continue;
    const insert=row.get('insert',true);
    if(deps.yaml.isSeq(insert)){
      visitEntries(insert);
      if(insert.items.length===0&&row.items.length===1)document.contents.delete(index);
    }
  }
  return removed;
}

function validateLocalConfig(filename){
  assertNoSymlink(filename);
  if(!exists(filename)||!fs.lstatSync(filename).isFile())fail('config_missing');
  try{
    const data=JSON.parse(fs.readFileSync(filename,'utf8').replace(/^\uFEFF/,''));
    if(!data||typeof data!=='object'||!['127.0.0.1','localhost','::1'].includes(data.host||'127.0.0.1')||
      !Number.isInteger(data.port)||data.port<1024||data.port>65535||
      typeof data.api_token!=='string'||data.api_token.length<24)fail('config_invalid');
  }catch(error){if(error instanceof RegistrationError)throw error;fail('config_invalid');}
}

function sameRegistration(config,expected){
  return config&&typeof config==='object'&&!Array.isArray(config)&&Object.keys(config).length===4&&
    config.enabled===true&&samePath(config.runtimePath,expected.runtimePath)&&
    samePath(config.brainConfigFile,expected.brainConfigFile)&&config.queueNamespace===expected.queueNamespace;
}

function currentMatches(filename,before){
  const current=readProfile(filename);
  return current.exists===before.exists&&digest(current.bytes)===digest(before.bytes);
}

function writeExclusive(filename,bytes,mode){
  const descriptor=fs.openSync(filename,'wx',mode);
  try{fs.writeFileSync(descriptor,bytes);fs.fsyncSync(descriptor);}finally{fs.closeSync(descriptor);}
}

function commit(filename,before,after,record){
  const lock=path.join(path.dirname(filename),'package.json.lock');
  assertNoSymlink(lock);
  let descriptor;
  try{descriptor=fs.openSync(lock,'wx',0o600);fs.writeFileSync(descriptor,String(process.pid)+'\n');fs.fsyncSync(descriptor);}
  catch(error){if(descriptor!==undefined){fs.closeSync(descriptor);try{fs.unlinkSync(lock);}catch{}}
    fail(error.code==='EEXIST'?'profile_locked':'write_failed');}
  const nonce=Date.now()+'-'+crypto.randomBytes(5).toString('hex');
  const temporary=path.join(path.dirname(filename),'.memory-brain-'+nonce+'.tmp');
  let backup,receipt;
  try{
    if(!currentMatches(filename,before))fail('profile_changed');
    const directory=path.join(path.dirname(filename),'.memory-brain-backups');
    assertNoSymlink(directory);fs.mkdirSync(directory,{recursive:true,mode:0o700});
    backup=path.join(directory,nonce+'.before.yml');
    receipt=path.join(directory,nonce+'.json');
    writeExclusive(backup,before.bytes,before.mode||0o600);
    writeExclusive(temporary,after,before.mode||0o600);
    if(!currentMatches(filename,before))fail('profile_changed');
    // Same-directory rename is the commit point. No runtime file is touched.
    fs.renameSync(temporary,filename);
    const receiptData={schema_version:1,operation:record.action,profile_patch:filename,
      plugin_id:ENTRY_ID,plugin_entry:record.entry,before_exists:before.exists,
      before_sha256:digest(before.bytes),after_sha256:digest(after),backup,
      restore_note:'Restore the before file only after confirming the profile still matches after_sha256; keep newer user changes.',
      committed_at:new Date().toISOString(),contains_token:false};
    try{writeExclusive(receipt,Buffer.from(JSON.stringify(receiptData,null,2)+'\n'),0o600);}
    catch{return {backup,receipt:null,receipt_warning:'receipt_write_failed_after_commit'};}
    return {backup,receipt};
  }catch(error){if(error instanceof RegistrationError)throw error;fail('write_failed');}
  finally{
    try{fs.unlinkSync(temporary);}catch{}
    if(descriptor!==undefined)fs.closeSync(descriptor);
    try{fs.unlinkSync(lock);}catch{}
  }
}

export async function registerDSH(options){
  const runtime=options.runtime,profilePatch=options.profilePatch,packageRoot=options.packageRoot||SCRIPT_ROOT;
  if(![runtime,profilePatch,packageRoot].every(value=>typeof value==='string'&&path.isAbsolute(value)))fail('absolute_path_required');
  for(const selected of [runtime,profilePatch,packageRoot])assertNoSymlink(selected);
  if(!['.yml','.yaml'].includes(path.extname(profilePatch).toLowerCase())||
    !exists(path.dirname(profilePatch))||!fs.lstatSync(path.dirname(profilePatch)).isDirectory())fail('invalid_profile_path');
  if(!exists(runtime)||!fs.lstatSync(runtime).isDirectory())fail('runtime_unavailable');
  const root=fs.realpathSync(packageRoot),selectedRuntime=fs.realpathSync(runtime);
  const patch=path.resolve(profilePatch),entry=path.join(root,'adapters','dsh','memory-brain.mjs');
  const brainConfigFile=path.join(root,'private','config.json');
  const deps=await dependencies(selectedRuntime);
  const loop=path.join(selectedRuntime,'node_modules','@deepseek-ai','dsh-agent-loop','lib','index.js');
  let requestHook=false;
  try{requestHook=supportsRequestMessages(fs.readFileSync(loop,'utf8'));}catch{}
  if(!options.remove){
    if(!requestHook)fail('request_hook_missing');
    assertNoSymlink(entry);
    if(!exists(entry)||!fs.lstatSync(entry).isFile())fail('package_entry_missing');
    if(options.apply)validateLocalConfig(brainConfigFile);
  }
  const before=readProfile(patch),{document,rows,text}=parseProfile(before,deps);
  const records=insertedRecords(rows),own=records.filter(record=>record.entry.id===ENTRY_ID);
  // Removal touches only our literal row; a broken unrelated bundle must not
  // prevent safe uninstallation or trigger repairs to that bundle.
  const inherited=options.remove?{inherited:[],home:[]}:inheritedRecords(selectedRuntime,patch,deps);
  if(inherited.inherited.some(record=>record.entry.id===ENTRY_ID))fail('bundle_registration_conflict');
  if(inherited.home.some(record=>record.entry.id===ENTRY_ID))fail('profile_ambiguous');
  if(own.length>1||own.some(record=>!record.inserted))fail('profile_ambiguous');
  if(own.length&&!entryMatches(own[0].entry.name,entry))fail('entry_conflict');
  const config={enabled:true,runtimePath:selectedRuntime,brainConfigFile,
    queueNamespace:digest(Buffer.from(comparable(patch))).slice(0,20)};
  let action;
  if(options.remove){
    action=own.length?'remove':'noop';
    if(own.length&&removeOwnedAST(document,deps)!==1)fail('profile_ambiguous');
  }else{
    if(conflicts([...inherited.inherited,...records,...inherited.home],entry).length)fail('legacy_memory_conflict');
    if(own.length){if(own[0].entry.disabled||own[0].parentDisabled||!sameRegistration(own[0].entry.config,config))fail('mixed_configuration');action='noop';}
    else{document.contents.flow=false;document.add(document.createNode({insert:[{id:ENTRY_ID,name:pathToFileURL(entry).href,config}]}));action='insert';}
  }
  let after=before.bytes;
  if(action!=='noop'){
    let rendered=document.toString();
    if(text.includes('\r\n'))rendered=rendered.replace(/\r?\n/g,'\r\n');
    if(text.startsWith('\uFEFF')&&!rendered.startsWith('\uFEFF'))rendered='\uFEFF'+rendered;
    after=Buffer.from(rendered,'utf8');
    // Validate the exact bytes to be written with the host's own YAML dialect.
    try{if(!Array.isArray(deps.jsYaml.load(rendered,{schema:deps.schema})))fail('profile_parse_failed');}
    catch(error){if(error instanceof RegistrationError)throw error;fail('profile_parse_failed');}
  }
  const summary={ok:true,applied:Boolean(options.apply&&action!=='noop'),dry_run:!options.apply,
    action,plugin_id:ENTRY_ID,profile_patch:patch,plugin_entry:pathToFileURL(entry).href,
    request_hook_present:requestHook,runtime_modified:false,data_deleted:false,token_included:false,
    inherited_configuration_checked:!options.remove,
    before_sha256:digest(before.bytes),after_sha256:digest(after),
    config:options.remove?undefined:config};
  if(options.apply&&action!=='noop')Object.assign(summary,commit(patch,before,after,{action,entry:pathToFileURL(entry).href}));
  return summary;
}

async function main(){
  try{console.log(JSON.stringify(await registerDSH(parseArgs(process.argv.slice(2)))));}
  catch(error){const code=error instanceof RegistrationError?error.code:'registration_failed';
    console.log(JSON.stringify({ok:false,error:code,message:MESSAGES[code],applied:false,token_included:false}));process.exitCode=1;}
}
if(process.argv[1]&&samePath(fileURLToPath(import.meta.url),process.argv[1]))await main();
