/** DSH_RUNTIME=<read-only installation> node tests/test_register_dsh.mjs */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { spawnSync } from 'node:child_process';
import { registerDSH, supportsRequestMessages, RegistrationError } from '../scripts/register-dsh.mjs';

const runtime=process.env.DSH_RUNTIME;
if(!runtime||!path.isAbsolute(runtime))throw new Error('Set DSH_RUNTIME to an existing read-only DSH installation.');
const require=createRequire(path.join(runtime,'package.json'));
const yaml=require('js-yaml');
const {entryListSchema}=await import(pathToFileURL(require.resolve('@deepseek-ai/cordis-plugin-include')).href);
const root=fs.mkdtempSync(path.join(os.tmpdir(),'memory-brain-register-'));
let passed=0,failed=0;
const test=async(name,fn)=>{try{await fn();passed++;console.log('PASS '+name);}catch(error){failed++;console.error('FAIL '+name+': '+error.stack);}};
function fixture(name,before='# Keep this comment\n[]\n'){
  const packageRoot=path.join(root,name,'package'),profile=path.join(root,name,'profile');
  fs.mkdirSync(path.join(packageRoot,'adapters','dsh'),{recursive:true});
  fs.mkdirSync(path.join(packageRoot,'private'),{recursive:true});fs.mkdirSync(profile,{recursive:true});
  fs.writeFileSync(path.join(packageRoot,'adapters','dsh','memory-brain.mjs'),'export async function apply() {}\n');
  fs.writeFileSync(path.join(packageRoot,'private','config.json'),JSON.stringify({host:'127.0.0.1',port:18880,api_token:'synthetic-token-never-print-in-result'}));
  const profilePatch=path.join(profile,'cordis.patch.yml');fs.writeFileSync(profilePatch,before);
  return {runtime,packageRoot,profilePatch};
}
const read=options=>fs.readFileSync(options.profilePatch,'utf8');
const parsed=options=>yaml.load(read(options),{schema:entryListSchema});
async function rejects(options,code){await assert.rejects(()=>registerDSH(options),error=>error instanceof RegistrationError&&error.code===code);}

try{
  await test('dry-run is read only and reports safe exact plan',async()=>{
    const options=fixture('preview'),before=read(options);const result=await registerDSH(options);
    assert.equal(result.action,'insert');assert.equal(result.applied,false);assert.equal(read(options),before);
    assert.equal(fs.existsSync(path.join(path.dirname(options.profilePatch),'.memory-brain-backups')),false);
    assert.equal(JSON.stringify(result).includes('synthetic-token'),false);assert.equal(result.config.queueNamespace.length,20);
  });
  await test('apply creates exactly one entry and preserves original-byte backup',async()=>{
    const options=fixture('apply'),before=fs.readFileSync(options.profilePatch);const result=await registerDSH({...options,apply:true});
    assert.equal(result.applied,true);assert.deepEqual(fs.readFileSync(result.backup),before);
    assert.equal(parsed(options).flatMap(row=>row.insert||[]).filter(row=>row.id==='memory-brain').length,1);
    const receipt=JSON.parse(fs.readFileSync(result.receipt,'utf8'));assert.equal(receipt.after_sha256,result.after_sha256);
    assert.equal(JSON.stringify(receipt).includes('synthetic-token'),false);
  });
  await test('reapply is byte-identical and does not create a second backup',async()=>{
    const options=fixture('reapply');await registerDSH({...options,apply:true});const before=read(options);
    const directory=path.join(path.dirname(options.profilePatch),'.memory-brain-backups'),files=fs.readdirSync(directory);
    const result=await registerDSH({...options,apply:true});assert.equal(result.action,'noop');assert.equal(result.applied,false);
    assert.equal(read(options),before);assert.deepEqual(fs.readdirSync(directory),files);
  });
  await test('remove previews then removes only own entry and retains data and comments',async()=>{
    const options=fixture('remove','# User plugin stays\n- insert:\n    - id: normal-tool\n      name: user-tool\n');
    fs.mkdirSync(path.join(options.packageRoot,'data'));fs.writeFileSync(path.join(options.packageRoot,'data','keep.txt'),'keep');
    await registerDSH({...options,apply:true});const before=read(options);
    assert.equal((await registerDSH({...options,remove:true})).action,'remove');assert.equal(read(options),before);
    const result=await registerDSH({...options,remove:true,apply:true});assert.equal(result.action,'remove');
    assert.equal(parsed(options).flatMap(row=>row.insert||[]).some(row=>row.id==='memory-brain'),false);
    assert.equal(parsed(options)[0].insert[0].id,'normal-tool');assert(read(options).includes('# User plugin stays'));
    assert.equal(fs.readFileSync(path.join(options.packageRoot,'data','keep.txt'),'utf8'),'keep');
    assert.equal((await registerDSH({...options,remove:true,apply:true})).action,'noop');
  });
  await test('host !!js expressions comments and CRLF survive AST editing',async()=>{
    const options=fixture('dialect','# important comment\r\n- id: harmless\r\n  disabled: !!js env.CUSTOM_SWITCH === "off"\r\n  config:\r\n    text: "keep # literal" # inline comment\r\n');
    await registerDSH({...options,apply:true});const after=read(options);
    assert(after.includes('!!js'));assert(after.includes('# important comment'));assert(after.includes('# inline comment'));
    assert(after.includes('\r\n'));assert.equal(parsed(options)[0].disabled.__jsExpr,'env.CUSTOM_SWITCH === "off"');
  });
  await test('active legacy memory rejects without changing bytes or creating backups',async()=>{
    for(const id of ['fish-self','fish-rawqueue','fish-rawrecall','fish-instant','persona-memory','xiaojiu-brain']){
      const options=fixture('conflict-'+id,'- insert:\n    - id: '+id+'\n      name: file:///legacy/plugin.mjs\n');const before=read(options);
      await rejects({...options,apply:true},'legacy_memory_conflict');assert.equal(read(options),before);
      assert.equal(fs.existsSync(path.join(path.dirname(options.profilePatch),'.memory-brain-backups')),false);
    }
  });
  await test('outer disabled legacy stays disabled and is not rewritten',async()=>{
    const options=fixture('disabled','- insert:\n    - id: fish-rawrecall\n      name: legacy\n      config:\n        enabled: true\n- id: fish-rawrecall\n  disabled: true\n');
    await registerDSH({...options,apply:true});assert.equal(parsed(options)[0].insert[0].config.enabled,true);
    assert.equal(parsed(options)[1].disabled,true);
  });
  await test('same ID from another package refuses apply and remove',async()=>{
    const options=fixture('wrong-path','- insert:\n    - id: memory-brain\n      name: file:///another/install/memory-brain.mjs\n');const before=read(options);
    await rejects({...options,apply:true},'entry_conflict');await rejects({...options,remove:true,apply:true},'entry_conflict');
    assert.equal(read(options),before);
  });
  await test('profile bundle memory conflict is detected without invoking a mutating host loader',async()=>{
    const options=fixture('bundle'),profile=path.dirname(options.profilePatch);
    const directory=path.join(profile,'node_modules','synthetic-memory-bundle');fs.mkdirSync(directory,{recursive:true});
    fs.writeFileSync(path.join(profile,'package.json'),JSON.stringify({dsh:{profile:{bundles:['synthetic-memory-bundle']}}}));
    fs.writeFileSync(path.join(directory,'package.json'),JSON.stringify({name:'synthetic-memory-bundle',dsh:{bundle:{patch:'./cordis.patch.yml'}}}));
    const patch=path.join(directory,'cordis.patch.yml');
    fs.writeFileSync(patch,'- insert:\n    - id: fish-self\n      name: old-plugin\n');
    const before=read(options);await rejects({...options,apply:true},'legacy_memory_conflict');assert.equal(read(options),before);
    fs.writeFileSync(patch,'- insert:\n    - id: memory-brain\n      name: ./adapters/dsh/memory-brain.mjs\n');
    await rejects({...options,apply:true},'bundle_registration_conflict');assert.equal(read(options),before);
  });
  await test('uninstall does not repair or require an unrelated broken bundle',async()=>{
    const options=fixture('uninstall-broken');await registerDSH({...options,apply:true});
    const manifest=path.join(path.dirname(options.profilePatch),'package.json');
    const before=JSON.stringify({dsh:{profile:{bundles:['not-installed-unrelated-bundle']}}});fs.writeFileSync(manifest,before);
    const result=await registerDSH({...options,remove:true,apply:true});assert.equal(result.action,'remove');
    assert.equal(fs.readFileSync(manifest,'utf8'),before);
  });
  await test('a user override or mixed brain configuration is not silently replaced',async()=>{
    const options=fixture('mixed');await registerDSH({...options,apply:true});
    const text=read(options).replace('enabled: true','enabled: false');fs.writeFileSync(options.profilePatch,text);
    await rejects({...options,apply:true},'mixed_configuration');assert.equal(read(options),text);
    fs.appendFileSync(options.profilePatch,'- id: memory-brain\n  disabled: true\n');const overridden=read(options);
    await rejects({...options,remove:true,apply:true},'profile_ambiguous');assert.equal(read(options),overridden);
  });
  await test('apply requires generated local configuration and never prints its contents',async()=>{
    const options=fixture('config');fs.unlinkSync(path.join(options.packageRoot,'private','config.json'));
    assert.equal((await registerDSH(options)).action,'insert');await rejects({...options,apply:true},'config_missing');
    assert.equal(read(options),'# Keep this comment\n[]\n');
  });
  await test('missing hook is rejected by the structural compatibility probe',async()=>{
    assert.equal(supportsRequestMessages('withRequestMessages(request,messages) {}'),false);
    assert.equal(supportsRequestMessages('this.buildRequest(decision.requestMessages)'),false);
    assert.equal(supportsRequestMessages('withRequestMessages(a,b){} this.withRequestMessages(request,decision.requestMessages)'),true);
  });
  await test('existing host writer lock is respected without altering profile',async()=>{
    const options=fixture('lock');fs.writeFileSync(path.join(path.dirname(options.profilePatch),'package.json.lock'),'other-writer');
    const before=read(options);await rejects({...options,apply:true},'profile_locked');assert.equal(read(options),before);
    assert.equal(fs.readFileSync(path.join(path.dirname(options.profilePatch),'package.json.lock'),'utf8'),'other-writer');
  });
  await test('malformed profile and relative paths fail before writes',async()=>{
    const options=fixture('invalid','not: a-sequence\n');await rejects({...options,apply:true},'profile_parse_failed');
    await rejects({...options,profilePatch:'relative.yml'},'absolute_path_required');
  });
  await test('symbolic-link parent is refused without changing its target',async()=>{
    const options=fixture('symlink'),before=read(options),link=path.join(root,'symlink','profile-link');
    fs.symlinkSync(path.dirname(options.profilePatch),link,process.platform==='win32'?'junction':'dir');
    await rejects({...options,profilePatch:path.join(link,'cordis.patch.yml'),apply:true},'symlink_refused');
    assert.equal(read(options),before);
  });
  await test('a concurrent edit at the CAS boundary is preserved and not overwritten',async()=>{
    const options=fixture('cas'),originalRead=fs.readFileSync;let reads=0;
    const concurrent='# New external edit must survive\n[]\n';
    fs.readFileSync=function(filename,...args){
      if(String(filename)===options.profilePatch&&++reads===2)fs.writeFileSync(filename,concurrent);
      return originalRead.call(fs,filename,...args);
    };
    try{await rejects({...options,apply:true},'profile_changed');}
    finally{fs.readFileSync=originalRead;}
    assert.equal(read(options),concurrent);
    assert.equal(fs.existsSync(path.join(path.dirname(options.profilePatch),'package.json.lock')),false);
  });
  await test('CLI emits a safe JSON failure rather than private parser details',async()=>{
    const options=fixture('cli','[secret-sensitive-unclosed\n');
    const child=spawnSync(process.execPath,[path.resolve('scripts/register-dsh.mjs'),'--runtime',runtime,'--profile-patch',options.profilePatch,
      '--package-root',options.packageRoot,'--apply'],{encoding:'utf8',windowsHide:true});
    assert.equal(child.status,1);const result=JSON.parse(child.stdout.trim());assert.equal(result.error,'profile_parse_failed');
    assert(!child.stdout.includes('secret-sensitive'));assert(!child.stdout.includes('synthetic-token'));assert.equal(child.stderr,'');
  });
}finally{
  const target=path.resolve(root);
  assert.equal(path.dirname(target),path.resolve(os.tmpdir()));
  assert(path.basename(target).startsWith('memory-brain-register-'));
  fs.rmSync(target,{recursive:true,force:true});
}
console.log(JSON.stringify({passed,failed,production_mutations:0,real_model_calls:0}));
if(failed)process.exitCode=1;
