/** Synthetic linked package layouts; no host modules or model calls execute. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {discoverHostRuntime} from '../adapters/dsh/runtime-discovery.mjs';
import {probeRuntime} from '../adapters/dsh/host-compat.mjs';

const temporary=fs.mkdtempSync(path.join(os.tmpdir(),'memory-brain-identity-fixture-'));
const names=['dsh-agent-loop','dsh-session','dsh-llm','dsh-tools'];
const vetted='throw Error("FIXTURE_MUST_NOT_EXECUTE");export const vettedFixture=true;\n';
const unknown='throw Error("FIXTURE_MUST_NOT_EXECUTE");export const unsupported=true;\n';
const fixtureHash=createHash('sha256').update(vetted).digest('hex');
function vettedProbe(root){
  // A test-only gate for the inert positive fixture. Keep all real filesystem
  // read failures and all other host admission decisions intact.
  const result=probeRuntime(root);
  if(result.compatibility.reason==='unknown_loop_fingerprint'&&result.compatibility.loop_sha256===fixtureHash){
    result.supported=true;result.injectionMode='scoped_agent_bridge';
    Object.assign(result.compatibility,{supported:true,mode:result.injectionMode,reason:'verified_test_fixture'});
  }
  return result;
}
let serial=0;
const moduleDirectory=(root,name)=>path.join(root,'node_modules','@deepseek-ai',name);
const moduleFile=(root,name)=>path.join(moduleDirectory(root,name),'lib/index.js');
function packageAt(directory,name,code){
  fs.mkdirSync(path.join(directory,'lib'),{recursive:true});
  fs.writeFileSync(path.join(directory,'package.json'),JSON.stringify({name:'@deepseek-ai/'+name,version:'0.2.2-rc.3',type:'module',exports:'./lib/index.js'}));
  fs.writeFileSync(path.join(directory,'lib/index.js'),code);
}
function link(target,directory){fs.mkdirSync(path.dirname(directory),{recursive:true});fs.symlinkSync(target,directory,process.platform==='win32'?'junction':'dir');}
function unlink(directory){if(process.platform==='win32')fs.rmdirSync(directory);else fs.unlinkSync(directory);}
function fixture({split=true,source=vetted}={}){
  const directory=path.join(temporary,'case-'+ ++serial),host=path.join(directory,'host'),store=path.join(directory,'store'),helpers={};
  const loopPackage=moduleDirectory(split?store:host,'dsh-agent-loop');
  packageAt(loopPackage,'dsh-agent-loop',source);if(split)link(loopPackage,moduleDirectory(host,'dsh-agent-loop'));
  for(const name of names.slice(1)){
    helpers[name]=path.join(directory,'shared',name);packageAt(helpers[name],name,'throw Error("HELPER_MUST_NOT_EXECUTE");\n');
    link(helpers[name],moduleDirectory(host,name));link(helpers[name],moduleDirectory(loopPackage,name));
  }
  const entry=path.join(host,'cli','entry.mjs'),empty=path.join(directory,'empty');
  fs.mkdirSync(path.dirname(entry),{recursive:true});fs.writeFileSync(entry,'throw Error("ENTRY_MUST_NOT_EXECUTE");\n');fs.mkdirSync(empty,{recursive:true});
  return {directory,host,store,loopPackage,helpers,entry,empty};
}
function options(value,overrides={}){
  return {allowUnverified:true,probeRuntime:vettedProbe,inspectRuntime:root=>{const result=vettedProbe(root);if(!result.supported)throw Error('unverified_host');return result;},
    environment:{},argv:[process.execPath,value.entry],pluginUrl:pathToFileURL(path.join(value.empty,'plugin.mjs')).href,
    cwd:value.empty,resourcesPath:null,electronVersion:null,execPath:path.join(value.empty,'node'),...overrides};
}
function discover(value,overrides={}){
  const result=discoverHostRuntime(options(value,overrides)),serialized=JSON.stringify(result.compatibility);
  assert.equal(serialized.includes(temporary),false);assert.equal(serialized.includes('file:///'),false);
  assert(result.compatibility.discovery.attempts.length<=32);assert(result.compatibility.discovery.candidate_count<=32);
  return result;
}
function replaceHost(value,name){
  unlink(moduleDirectory(value.host,name));const replacement=path.join(value.directory,'different',name);
  packageAt(replacement,name,'throw Error("OTHER_INSTANCE_MUST_NOT_EXECUTE");\n');link(replacement,moduleDirectory(value.host,name));
}
function scatterSession(value){
  unlink(moduleDirectory(value.host,'dsh-session'));link(value.helpers['dsh-session'],moduleDirectory(value.directory,'dsh-session'));
  const resolver=createRequire(value.entry),loop=resolver.resolve('@deepseek-ai/dsh-agent-loop');
  assert.equal(fs.realpathSync(resolver.resolve('@deepseek-ai/dsh-session')),
    fs.realpathSync(createRequire(loop).resolve('@deepseek-ai/dsh-session')));
  assert.equal(probeRuntime(value.host).compatibility.reason,'required_modules_missing');
}
const tests=[];const test=(name,run)=>tests.push({name,run});
test('a coherent desktop runtime argument retains support',()=>{
  const value=fixture({split:false}),entry=path.join(value.host,'node_modules','@deepseek-ai','dsh-desktop-host','lib','index.js');
  const result=discover(value,{argv:[process.execPath,entry,value.host]});assert.equal(result.supported,true);
  assert.equal(result.compatibility.discovery.source,'desktop_host_argument');
});
for(const name of names.slice(1))test('desktop runtime argument cannot bypass '+name+' identity checks',()=>{
  const value=fixture({split:false});replaceHost(value,name);
  const entry=path.join(value.host,'node_modules','@deepseek-ai','dsh-desktop-host','lib','index.js');
  const result=discover(value,{argv:[process.execPath,entry,value.host]});assert.equal(result.supported,false);
  assert.equal(result.compatibility.reason,'host_module_identity_mismatch');
});
test('an explicitly selected mixed runtime refuses instead of falling back to a working host',()=>{
  const value=fixture({split:false}),other=fixture({split:false});replaceHost(value,'dsh-llm');
  const result=discover(other,{environment:{XIAOJIU_DSH_RUNTIME:value.host}});
  assert.equal(result.supported,false);assert.equal(result.compatibility.reason,'host_module_identity_mismatch');
  assert.equal(result.compatibility.discovery.source,'environment');
});
test('an identified scattered host never switches to a different plugin-local runtime',()=>{
  const value=fixture(),other=fixture({split:false});scatterSession(value);
  const result=discover(value,{pluginUrl:pathToFileURL(path.join(other.host,'plugin.mjs')).href});
  assert.equal(result.supported,false,'a host anchor already resolved; another loop instance cannot replace it');
  assert.equal(result.injectionMode,'diagnostic_only');assert.equal(result.compatibility.reason,'host_module_identity_mismatch');
});
test('an identified scattered host never switches to a different working-directory runtime',()=>{
  const value=fixture(),other=fixture({split:false});scatterSession(value);
  const result=discover(value,{cwd:other.host});assert.equal(result.supported,false);
  assert.equal(result.injectionMode,'diagnostic_only');assert.equal(result.compatibility.reason,'host_module_identity_mismatch');
});
test('an authoritative complete unknown host never falls through to a known plugin runtime',()=>{
  const value=fixture({split:false,source:unknown}),other=fixture({split:false});
  const result=discover(value,{pluginUrl:pathToFileURL(path.join(other.host,'plugin.mjs')).href});
  assert.equal(result.supported,false);assert.equal(result.compatibility.reason,'unknown_loop_fingerprint');
  assert.equal(fs.realpathSync(result.runtime),fs.realpathSync(value.host));
});

let failed=0;
for(const {name,run} of tests){try{run();console.log('PASS '+name);}catch(error){failed++;console.error('FAIL '+name+'\n'+error.stack);}}
console.log(JSON.stringify({passed:tests.length-failed,failed,total:tests.length,network_calls:0,model_calls:0,host_mutations:0}));
if(failed)process.exitCode=1;
// Retain only these owned synthetic fixture directories for failed-case review.
