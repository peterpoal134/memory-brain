/** Real Node resolution and filesystem-link regression checks. All packages
 * are synthetic, inert fixtures: no host modules, models or user config run.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {discoverHostRuntime} from '../adapters/dsh/runtime-discovery.mjs';
import {probeRuntime} from '../adapters/dsh/host-compat.mjs';

const temporary=fs.mkdtempSync(path.join(os.tmpdir(),'memory-brain-discovery-private-fixture-'));
const names=['dsh-agent-loop','dsh-session','dsh-llm','dsh-tools'];
const vettedSource='throw new Error("SYNTHETIC_MODULE_CODE_MUST_NOT_EXECUTE");\nexport const sourceLabel="vetted_fixture";\n';
const unknownSource='throw new Error("SYNTHETIC_MODULE_CODE_MUST_NOT_EXECUTE");\nexport const sourceLabel="unknown_loop";\n';
const fixtureHash=createHash('sha256').update(vettedSource).digest('hex');
function vettedProbe(root){
  // Discovery tests admit only their exact inert positive fixture, after the
  // real probe confirms the entire required package set is readable.
  const result=probeRuntime(root);
  if(result.compatibility.reason==='unknown_loop_fingerprint'&&result.compatibility.loop_sha256===fixtureHash){
    result.supported=true;result.injectionMode='scoped_agent_bridge';
    Object.assign(result.compatibility,{supported:true,mode:result.injectionMode,reason:'verified_test_fixture'});
  }
  return result;
}
const checks=[];
const check=(name,run)=>checks.push({name,run});
let fixtureNumber=0;
function packageAt(directory,name,source=vettedSource){
  fs.mkdirSync(path.join(directory,'lib'),{recursive:true});
  fs.writeFileSync(path.join(directory,'package.json'),JSON.stringify({name:'@deepseek-ai/'+name,
    version:'0.2.2-rc.3',type:'module',exports:'./lib/index.js',main:'./lib/index.js',
    private_note:'SYNTHETIC_PRIVATE_PACKAGE_NOTE_DO_NOT_REPORT'}));
  fs.writeFileSync(path.join(directory,'lib/index.js'),source);
  return directory;
}
function linkDirectory(target,link){
  fs.mkdirSync(path.dirname(link),{recursive:true});
  fs.symlinkSync(target,link,process.platform==='win32'?'junction':'dir');
}
const moduleDirectory=(root,name)=>path.join(root,'node_modules','@deepseek-ai',name);
const moduleFile=(root,name)=>path.join(moduleDirectory(root,name),'lib/index.js');
function fixture({split=true,source=vettedSource}={}){
  const directory=path.join(temporary,'fixture-'+ ++fixtureNumber),host=path.join(directory,'host');
  const store=path.join(directory,'store','loop-container');
  const actualLoop=moduleDirectory(split?store:host,'dsh-agent-loop');
  packageAt(actualLoop,'dsh-agent-loop',source);
  if(split)linkDirectory(actualLoop,moduleDirectory(host,'dsh-agent-loop'));
  const helpers={};
  for(const name of names.slice(1)){
    helpers[name]=packageAt(path.join(directory,'shared',name),name,
      'throw new Error("SYNTHETIC_HELPER_CODE_MUST_NOT_EXECUTE");\n');
    linkDirectory(helpers[name],moduleDirectory(host,name));
    // Helpers resolved from the loop's real package are the exact same modules
    // as the host's helpers. They need not be siblings in the package store.
    linkDirectory(helpers[name],moduleDirectory(actualLoop,name));
  }
  const entry=path.join(host,'cli','entry.mjs');
  fs.mkdirSync(path.dirname(entry),{recursive:true});fs.writeFileSync(entry,'throw new Error("ENTRY_MUST_NOT_EXECUTE");\n');
  const empty=path.join(directory,'unrelated-empty');fs.mkdirSync(empty,{recursive:true});
  fs.mkdirSync(path.join(host,'private'),{recursive:true});
  fs.writeFileSync(path.join(host,'private/config.json'),JSON.stringify({token:'SYNTHETIC_PRIVATE_TOKEN_DO_NOT_REPORT',
    transcript:'SYNTHETIC_PRIVATE_TRANSCRIPT_DO_NOT_REPORT'}));
  return {directory,host,store,actualLoop,helpers,entry,empty};
}
function options(value,overrides={}){
  return {allowUnverified:true,probeRuntime:vettedProbe,
    inspectRuntime:root=>{const result=vettedProbe(root);if(!result.supported)throw new Error('fixture_host_not_verified');return result;},
    pluginUrl:pathToFileURL(path.join(value.empty,'plugin.mjs')).href,
    environment:{},argv:[process.execPath,value.entry],cwd:value.empty,
    resourcesPath:null,electronVersion:null,execPath:path.join(value.empty,'node'),...overrides};
}
const discover=(value,overrides={})=>discoverHostRuntime(options(value,overrides));
const sameFile=(a,b)=>fs.realpathSync(a).toLowerCase()===fs.realpathSync(b).toLowerCase();
function replaceHostHelper(value,name){
  const target=moduleDirectory(value.host,name);
  // rmdir removes this owned junction/symlink, never the shared helper target.
  if(process.platform==='win32')fs.rmdirSync(target);else fs.unlinkSync(target);
  const different=packageAt(path.join(value.directory,'conflicting',name),name,
    'throw new Error("DIFFERENT_HELPER_COPY_MUST_NOT_EXECUTE");\n');
  linkDirectory(different,target);
}
function assertSafeDiagnostics(result,value){
  const report=result.compatibility,serialized=JSON.stringify(report);
  for(const forbidden of [temporary,path.basename(temporary),value.host,value.actualLoop,'file:///',
    'SYNTHETIC_PRIVATE_PACKAGE_NOTE_DO_NOT_REPORT','SYNTHETIC_PRIVATE_TOKEN_DO_NOT_REPORT',
    'SYNTHETIC_PRIVATE_TRANSCRIPT_DO_NOT_REPORT','SYNTHETIC_MODULE_CODE_MUST_NOT_EXECUTE'])
    assert.equal(serialized.includes(forbidden),false,'copyable diagnostics contain private data');
  const discovery=report.discovery;
  assert.ok(Array.isArray(discovery.attempts),'bounded diagnostic attempts are required');
  assert.ok(discovery.attempts.length<=32,'diagnostic attempt limit exceeded');
  assert.ok(Number.isInteger(discovery.candidate_count)&&discovery.candidate_count>=0&&discovery.candidate_count<=32);
  assert.ok(discovery.asar_enabled===null||typeof discovery.asar_enabled==='boolean');
  for(const attempt of discovery.attempts){
    assert.deepEqual(Object.keys(attempt).sort(),['code','layout','source','stage']);
    for(const value of Object.values(attempt))assert.ok(typeof value==='string'&&/^[A-Za-z0-9_.:-]{1,120}$/.test(value));
  }
  assert.ok(Array.isArray(report.read_errors),'bounded read diagnostics are required');
  assert.ok(report.read_errors.length<=24,'read error limit exceeded');
  for(const error of report.read_errors){
    assert.deepEqual(Object.keys(error).sort(),['code','module','stage']);
    for(const value of Object.values(error))assert.ok(typeof value==='string'&&/^[A-Za-z0-9_.:-]{1,120}$/.test(value));
  }
}

check('fixture uses real Node resolution into a separate loop-only package store',()=>{
  const value=fixture(),resolved=createRequire(value.entry).resolve('@deepseek-ai/dsh-agent-loop');
  assert.ok(sameFile(resolved,path.join(value.actualLoop,'lib/index.js')));
  assert.notEqual(path.resolve(path.dirname(resolved),'../../../..'),value.host);
  for(const name of names.slice(1)){
    const fromHost=createRequire(value.entry).resolve('@deepseek-ai/'+name);
    const fromLoop=createRequire(resolved).resolve('@deepseek-ai/'+name);
    assert.ok(sameFile(fromHost,fromLoop));
  }
});
check('fixed ascent reaches an incomplete root while the actual host root is complete',()=>{
  const value=fixture(),resolved=createRequire(value.entry).resolve('@deepseek-ai/dsh-agent-loop');
  const guessed=path.resolve(path.dirname(resolved),'../../../..');
  assert.equal(probeRuntime(guessed).compatibility.reason,'required_modules_missing');
  assert.equal(probeRuntime(value.host).supported,false);
  assert.equal(vettedProbe(value.host).supported,true);
});
check('split package discovery continues past the incomplete root and admits the same host',()=>{
  const value=fixture(),result=discover(value);
  assert.equal(result.supported,true);assert.ok(sameFile(result.runtime,value.host));
  assert.equal(result.compatibility.reason,'verified_test_fixture');
});
check('strict activation also continues past the incomplete inferred root',()=>{
  const value=fixture(),result=discover(value,{allowUnverified:false});
  assert.equal(result.supported,true);assert.ok(sameFile(result.runtime,value.host));
});
check('a complete guessed store with different sibling helpers cannot hide the actual host root',()=>{
  const value=fixture();
  for(const name of names.slice(1))packageAt(moduleDirectory(value.store,name),name,
    'throw new Error("UNRELATED_STORE_SIBLING_MUST_NOT_EXECUTE");\n');
  assert.equal(vettedProbe(value.store).supported,true,'fixture store is complete but not the anchor module set');
  for(const allowUnverified of [true,false]){
    const result=discover(value,{allowUnverified});
    assert.equal(result.supported,true);assert.ok(sameFile(result.runtime,value.host));
  }
});
check('flat official-style module layouts retain their existing discovery path',()=>{
  const value=fixture({split:false}),result=discover(value);
  assert.equal(result.supported,true);assert.ok(sameFile(result.runtime,value.host));
});
for(const name of names.slice(1))check('shared loop cannot admit a different host '+name+' module instance',()=>{
  const value=fixture();replaceHostHelper(value,name);
  const loop=createRequire(value.entry).resolve('@deepseek-ai/dsh-agent-loop');
  assert.equal(sameFile(createRequire(value.entry).resolve('@deepseek-ai/'+name),createRequire(loop).resolve('@deepseek-ai/'+name)),false);
  const result=discover(value);assert.equal(result.supported,false);
});
check('an unknown complete loop stays closed instead of receiving snapshot compatibility',()=>{
  const value=fixture({source:unknownSource}),result=discover(value);
  assert.equal(result.supported,false);assert.ok(sameFile(result.runtime,value.host));
  assert.equal(result.compatibility.reason,'unknown_loop_fingerprint');
});
check('a complete unrelated working-directory runtime cannot replace the actual host loop identity',()=>{
  const value=fixture(),unrelated=fixture({split:false});replaceHostHelper(value,'dsh-llm');
  const result=discover(value,{cwd:unrelated.host});
  assert.equal(result.supported,false);
});
check('an explicitly configured incomplete runtime is reported without changing its selection',()=>{
  const value=fixture(),result=discover(value,{environment:{XIAOJIU_DSH_RUNTIME:value.store}});
  assert.equal(result.supported,false);assert.ok(sameFile(result.runtime,value.store));
  assert.equal(result.compatibility.reason,'required_modules_missing');
  assert.equal(result.compatibility.discovery.source,'environment');
});
check('explicit unknown source remains authoritative and cannot fall back to a supported host',()=>{
  const value=fixture(),unknown=fixture({split:false,source:unknownSource});
  const result=discover(value,{environment:{XIAOJIU_DSH_RUNTIME:unknown.host}});
  assert.equal(result.supported,false);assert.ok(sameFile(result.runtime,unknown.host));
  assert.equal(result.compatibility.reason,'unknown_loop_fingerprint');
  assert.equal(result.compatibility.discovery.source,'environment');
});
check('read and discovery diagnostics are bounded and contain only safe labels',()=>{
  const value=fixture(),result=discover(value);
  assert.equal(result.supported,true);assertSafeDiagnostics(result,value);
});
check('identity refusals are safe, bounded and observable without paths or source text',()=>{
  const value=fixture();replaceHostHelper(value,'dsh-tools');const result=discover(value);
  assert.equal(result.supported,false);assertSafeDiagnostics(result,value);
  assert.ok(result.compatibility.discovery.attempts.some(item=>/identity/.test(item.code)),
    'identity rejection should be observable without publishing module paths');
});
check('unavailable runtimes produce bounded read-error diagnostics without config data',()=>{
  const value=fixture();const result=discover(value,{environment:{XIAOJIU_DSH_RUNTIME:value.empty}});
  assert.equal(result.supported,false);assertSafeDiagnostics(result,value);
});

let failed=0;
for(const {name,run} of checks){
  try{await run();console.log('PASS '+name);}
  catch(error){failed++;console.error('FAIL '+name+'\n'+error.stack);}
}
console.log(JSON.stringify({passed:checks.length-failed,failed,total:checks.length,model_calls:0,
  synthetic_modules_executed:0,existing_host_mutations:0,real_resolution_and_directory_links:true}));
if(failed)process.exitCode=1;
// Leave only owned temporary fixture files for examination. CI runners clean
// their own temporary directories; this check never recursively removes roots.
