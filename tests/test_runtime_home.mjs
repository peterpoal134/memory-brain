/** First-run installation uses only temporary homes, injected Python processes
 * and a loopback HTTP fixture. No pip, model API or installed DSH is invoked.
 * Windows secret tests exercise real CurrentUser DPAPI with synthetic secrets.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import http from 'node:http';
import crypto from 'node:crypto';
import {spawn} from 'node:child_process';
import {createRuntimeManager,resolveRuntimeHome,executeProcess} from '../adapters/dsh/runtime-home.mjs';

const VERSION='0.2.0a999';
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const tests=[];function test(name,operation){tests.push({name,operation});}
function bytesTree(directory){
  const entries=[];const walk=base=>{for(const item of fs.readdirSync(base,{withFileTypes:true})){const file=path.join(base,item.name);
    if(item.isDirectory())walk(file);else entries.push([path.relative(directory,file),crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex')]);}};
  walk(directory);return JSON.stringify(entries.sort());
}
function runLocal(command,args,options){return new Promise((resolve,reject)=>{
  const child=spawn(command,args,{cwd:options.cwd,env:options.env,windowsHide:true,stdio:['pipe','pipe','pipe'],shell:false});let stdout='',stderr='';
  child.stdout.on('data',data=>stdout+=data.toString('utf8'));child.stderr.on('data',data=>stderr+=data.toString('utf8'));
  child.once('error',reject);child.once('close',code=>resolve({code,stdout,stderr}));child.stdin.end(options.input||'');
});}
async function fixture({source=false,failInstall=0,missingPython=false,listedPython=false}={}){
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'memory-brain-runtime-test-')),packageRoot=path.join(root,'package'),home=path.join(root,'profile-home');
  fs.mkdirSync(packageRoot);fs.writeFileSync(path.join(packageRoot,'pyproject.toml'),'[project]\nname = "xiaojiu-brain"\nversion = "'+VERSION+'"\n');
  fs.mkdirSync(path.join(packageRoot,'xiaojiu_brain'));fs.writeFileSync(path.join(packageRoot,'xiaojiu_brain','__init__.py'),'# synthetic source\n');
  if(source){fs.mkdirSync(path.join(packageRoot,'scripts'));fs.writeFileSync(path.join(packageRoot,'scripts','build_runtime_wheel.py'),'# synthetic builder\n');}
  fs.mkdirSync(path.join(packageRoot,'wheels'));fs.mkdirSync(path.join(packageRoot,'compat'));
  const dependencyName='tzdata-2026.6-py2.py3-none-any.whl',dependencyFile=path.join(packageRoot,'wheels',dependencyName);
  fs.writeFileSync(dependencyFile,'synthetic vendored dependency');
  fs.writeFileSync(path.join(packageRoot,'compat','runtime-dependencies.json'),JSON.stringify({format:1,files:[{filename:dependencyName,
    sha256:crypto.createHash('sha256').update(fs.readFileSync(dependencyFile)).digest('hex'),license:'Apache-2.0',
    source:'https://files.pythonhosted.org/packages/synthetic/'+dependencyName}]}));
  if(!source)fs.writeFileSync(path.join(packageRoot,'wheels','xiaojiu_brain-'+VERSION+'-py3-none-any.whl'),'not executed');
  const calls=[],state={installedVersion:null,server:null,serverVersion:VERSION,starts:0,shutdowns:0,pip:0,init:0,venv:0,activeInstall:0,maxActiveInstall:0};
  const argument=(args,key)=>args[args.indexOf(key)+1];
  const configPath=path.join(home,'private','config.json'),markerPath=path.join(home,'runtime-manager.json');
  const python311=path.join(root,'installed-python-311','python.exe'),python314=path.join(root,'installed-python-314','python.exe');
  if(listedPython)for(const file of [python311,python314]){fs.mkdirSync(path.dirname(file),{recursive:true});fs.writeFileSync(file,'synthetic installed interpreter');}
  async function serve(config,{wrongToken=false,wrongVersion=false}={}){
    if(state.server)return;
    const server=http.createServer((request,response)=>{
      if(request.headers.authorization!=='Bearer '+(wrongToken?'different-fixture-token':config.api_token)){
        request.resume();response.writeHead(401,{'Content-Type':'application/json','Connection':'close'});response.end(JSON.stringify({ok:false,error:'unauthorized'}));return;
      }
      let input='';request.on('data',chunk=>input+=chunk);request.on('end',()=>{
        const send=data=>{response.writeHead(200,{'Content-Type':'application/json','Connection':'close'});response.end(JSON.stringify({ok:true,data}));};
        if(request.url==='/v1/status')send({version:wrongVersion?'9.9.9':state.serverVersion,build_id:'synthetic-test-build'});
        else if(request.url==='/v1/session/ticket')send({ticket:crypto.randomBytes(32).toString('base64url')});
        else if(request.url==='/v1/admin/shutdown'){
          state.shutdowns++;send({stopping:true});setTimeout(()=>{server.close();server.closeAllConnections?.();state.server=null;},15);
        }else{response.writeHead(404);response.end();}
      });
    });
    await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(config.port,config.host,resolve);});state.server=server;
  }
  const execute=async(command,args,options)=>{
    calls.push({command,args:[...args],options});
    if(command==='powershell.exe')return runLocal(command,args,options);
    if(command==='py'&&['--list-paths','-0p'].includes(args[0])){
      if(!listedPython)throw Error('synthetic launcher unavailable');
      return {code:0,stdout:' -3.14-64 * '+python314+'\n -3.11-64 '+python311+'\n -3.99 '+path.join(root,'missing-python.exe')+'\n'};
    }
    if(args.includes('-c')&&String(args.at(-1)).includes('sys.version_info.major')){
      if(missingPython)throw Error('synthetic Python missing');return {code:0,stdout:JSON.stringify({major:3,minor:11,version:'3.11.9'})};
    }
    if(args.includes('-c')&&String(args.at(-1)).includes('importlib.metadata'))return {code:0,stdout:JSON.stringify({version:state.installedVersion,prefix:path.join(home,'.venv')})};
    if(args.includes('venv')){
      state.venv++;const python=path.join(home,'.venv',process.platform==='win32'?'Scripts':'bin',process.platform==='win32'?'python.exe':'python');
      fs.mkdirSync(path.dirname(python),{recursive:true});fs.writeFileSync(python,'synthetic executable');return {code:0,stdout:''};
    }
    if(args.some(value=>String(value).endsWith('build_runtime_wheel.py'))){
      const sourcePath=argument(args,'--source');assert.ok(sourcePath.startsWith(path.join(home,'tmp','kernel-source-')));
      assert.ok(fs.existsSync(path.join(sourcePath,'xiaojiu_brain','__init__.py')));
      const directory=argument(args,'--out');const file=path.join(directory,'xiaojiu_brain-'+VERSION+'-py3-none-any.whl');
      fs.writeFileSync(file,'synthetic built wheel');return {code:0,stdout:JSON.stringify({ok:true,wheel:file,
        sha256:crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex')})};
    }
    if(args.includes('pip')){
      state.pip++;state.activeInstall++;state.maxActiveInstall=Math.max(state.maxActiveInstall,state.activeInstall);await pause(30);state.activeInstall--;
      if(failInstall-->0)return {code:1,stdout:'',stderr:'synthetic install failure'};
      const target=args.at(-1);assert.ok(target.startsWith(packageRoot)||target.startsWith(path.join(home,'tmp')));
      if(source)assert.ok(target.endsWith('.whl')&&fs.existsSync(target));
      state.installedVersion=VERSION;return {code:0,stdout:'installed synthetic fixture'};
    }
    if(args.includes('init')){
      state.init++;const file=argument(args,'--config');assert.equal(fs.existsSync(file),false);
      fs.mkdirSync(path.dirname(file),{recursive:true});fs.writeFileSync(file,JSON.stringify({owner:argument(args,'--owner'),host:'127.0.0.1',
        port:Number(argument(args,'--port')),data_dir:argument(args,'--data-dir'),api_token:crypto.randomBytes(32).toString('hex'),
        llm_key_env:'DEEPSEEK_API_KEY',embed_key_env:'XIAOJIU_EMBED_KEY',llm_url:'',llm_model:'',embed_url:'',embed_model:'bge-m3'}));
      return {code:0,stdout:'synthetic config created'};
    }
    if(args.at(-1)==='start'){state.starts++;await serve(JSON.parse(fs.readFileSync(configPath,'utf8')));return {code:0,stdout:'synthetic started'};}
    throw Error('unexpected synthetic invocation');
  };
  const manager=()=>createRuntimeManager({packageRoot,home,execute});
  const result={root,packageRoot,home,configPath,markerPath,state,calls,execute,manager,serve,python311,python314,dependencyFile,
    config:()=>JSON.parse(fs.readFileSync(configPath,'utf8')),
    close:async()=>{if(state.server){const server=state.server;state.server=null;server.closeAllConnections?.();await new Promise(resolve=>server.close(resolve));}
      fs.rmSync(root,{recursive:true,force:true});}};
  return result;
}
async function using(options,operation){const value=await fixture(options);try{return await operation(value);}finally{await value.close();}}

test('stable profile homes and explicit paths do not depend on package location',async()=>{
  const explicit=path.resolve(os.tmpdir(),'explicit-brain-home');assert.equal(await resolveRuntimeHome({runtimeHome:explicit}),explicit);
  const a=await resolveRuntimeHome({profile:'profile / A'}),b=await resolveRuntimeHome({profile:'profile / A'}),c=await resolveRuntimeHome({profile:'profile \\ A'});
  assert.equal(a,b);assert.notEqual(a,c);assert.ok(path.isAbsolute(a));assert.ok(!a.includes('profile / A'));
  await assert.rejects(resolveRuntimeHome({runtimeHome:'relative'}),/runtime_home_must_be_absolute/);
  await assert.rejects(resolveRuntimeHome({packageRoot:5}),/package_root_must_be_absolute/);
});
test('status does not initialize, install, write state or repeatedly spawn Python',()=>using({},async f=>{
  const before=bytesTree(f.packageRoot),m=f.manager();const a=await m.status(),count=f.calls.length;await m.status();await m.status();
  assert.equal(a.state,'not_initialized');assert.equal(f.calls.length,count);assert.equal(f.state.pip,0);assert.equal(fs.existsSync(f.home),false);assert.equal(bytesTree(f.packageRoot),before);
}));
test('explicit dependency consent is required before venv or pip',()=>using({},async f=>{
  await assert.rejects(f.manager().initialize(),/dependency_downloads_not_authorized/);assert.equal(f.state.pip,0);assert.equal(f.state.venv,0);assert.equal(fs.existsSync(f.configPath),false);
}));
test('wheel installation and init are idempotent and package is unchanged',()=>using({},async f=>{
  const before=bytesTree(f.packageRoot),m=f.manager();assert.equal((await m.initialize({allowDownloads:true})).state,'ready');
  const cfg=f.config(),bytes=fs.readFileSync(f.configPath);fs.writeFileSync(path.join(f.home,'data','preserve.txt'),'existing data');
  await m.initialize({allowDownloads:false});assert.equal(f.state.pip,1);assert.equal(f.state.init,1);assert.equal(f.state.venv,1);
  assert.ok(fs.readFileSync(f.configPath).equals(bytes));assert.equal(cfg.owner,'local-owner');assert.ok(cfg.port>=18880&&cfg.port<=18899);
  assert.equal(cfg.llm_key_env,'MEMORY_BRAIN_LLM_KEY');assert.equal(cfg.embed_key_env,'MEMORY_BRAIN_EMBED_KEY');assert.equal(bytesTree(f.packageRoot),before);
  assert.equal(fs.readFileSync(path.join(f.home,'data','preserve.txt'),'utf8'),'existing data');
}));
test('source install stages outside read-only package and removes only own staging',()=>using({source:true},async f=>{
  const before=bytesTree(f.packageRoot);await f.manager().initialize({allowDownloads:true});const pip=f.calls.find(call=>call.args.includes('pip'));
  assert.ok(pip.args.at(-1).startsWith(path.join(f.home,'tmp','kernel-wheel-')));assert.equal(fs.existsSync(pip.args.at(-1)),false);assert.equal(bytesTree(f.packageRoot),before);
  assert.ok(pip.args.includes('--only-binary=:all:'));
}));
test('installation failures leave retryable state without deleting existing data',()=>using({failInstall:1},async f=>{
  const m=f.manager();await assert.rejects(m.initialize({allowDownloads:true}),/owned_process_failed/);
  fs.writeFileSync(path.join(f.home,'data','keep'),'keep');assert.equal((await m.initialize({allowDownloads:true})).state,'ready');
  assert.equal(f.state.pip,2);assert.equal(f.state.venv,1);assert.equal(fs.readFileSync(path.join(f.home,'data','keep'),'utf8'),'keep');
}));
test('missing Python is an actionable category without Python downloads',()=>using({missingPython:true},async f=>{
  await assert.rejects(f.manager().initialize({allowDownloads:true}),/python_310_required/);assert.equal(f.state.pip,0);assert.equal(f.state.venv,0);
}));
test('Windows enumeration prefers an existing tested Python 3.11 without acquisition commands',()=>using({listedPython:true},async f=>{
  await f.manager().initialize({allowDownloads:true});
  if(process.platform==='win32'){
    assert.equal(f.calls.find(call=>call.args.includes('venv')).command,f.python311);
    assert.ok(f.calls.some(call=>call.command==='py'&&call.args[0]==='--list-paths'));
    assert.ok(!f.calls.some(call=>call.command==='py'&&call.args.some(arg=>/^-3/.test(arg))));
    assert.ok(!f.calls.some(call=>call.command.endsWith('missing-python.exe')));
  }
}));
test('two managers serialize initialization on the same disk home',()=>using({},async f=>{
  await Promise.all([f.manager().initialize({allowDownloads:true}),f.manager().initialize({allowDownloads:true})]);
  assert.equal(f.state.maxActiveInstall,1);assert.equal(f.state.pip,1);assert.equal(f.state.init,1);assert.equal(fs.existsSync(path.join(f.home,'.runtime-manager.lock')),false);
}));
test('unmanaged configuration is refused and untouched',()=>using({},async f=>{
  fs.mkdirSync(path.dirname(f.configPath),{recursive:true});fs.writeFileSync(f.configPath,'{"existing":"not ours"}');const before=fs.readFileSync(f.configPath);
  await assert.rejects(f.manager().initialize({allowDownloads:true}),/unmanaged_home_configuration/);assert.ok(fs.readFileSync(f.configPath).equals(before));assert.equal(f.state.pip,0);
}));
test('ambiguous or wrong wheel is refused before pip',()=>using({},async f=>{
  fs.writeFileSync(path.join(f.packageRoot,'wheels','xiaojiu_brain-0.0.1-py3-none-any.whl'),'no');
  await assert.rejects(f.manager().initialize({allowDownloads:true}),/ambiguous_kernel_wheel/);assert.equal(f.state.pip,0);
}));
test('child processes use owned tmp and never inherit production token',()=>using({},async f=>{
  const saved={};for(const key of ['XIAOJIU_BRAIN_TOKEN','PIP_INDEX_URL','PIP_EXTRA_INDEX_URL','PIP_CONFIG_FILE'])saved[key]=process.env[key];
  process.env.XIAOJIU_BRAIN_TOKEN='synthetic-inherited-token';process.env.PIP_INDEX_URL='https://user:synthetic@example.invalid/simple';
  process.env.PIP_EXTRA_INDEX_URL='https://example.invalid/extra';process.env.PIP_CONFIG_FILE='do-not-read-this-config';
  try{await f.manager().initialize({allowDownloads:true});}finally{for(const [key,value] of Object.entries(saved)){if(value===undefined)delete process.env[key];else process.env[key]=value;}}
  for(const call of f.calls){assert.equal(call.options.env.XIAOJIU_BRAIN_TOKEN,undefined);assert.equal(call.options.env.PYTHONUTF8,'1');assert.equal(call.options.env.TEMP,path.join(f.home,'tmp'));assert.equal(call.options.windowsHide,true);
    assert.equal(call.options.env.PIP_INDEX_URL,undefined);assert.equal(call.options.env.PIP_EXTRA_INDEX_URL,undefined);assert.equal(call.options.env.PIP_CONFIG_FILE,os.devNull);}
  const pip=f.calls.find(call=>call.args.includes('pip'));assert.ok(pip.args.includes('--no-index'));assert.ok(!pip.args.includes('--index-url'));
  assert.equal(pip.args[pip.args.indexOf('--find-links')+1],path.join(f.packageRoot,'wheels'));assert.ok(!pip.args.some(arg=>/^https?:/.test(arg)));
}));
test('missing dependency manifest refuses installation rather than falling back to network',()=>using({},async f=>{
  fs.unlinkSync(path.join(f.packageRoot,'compat','runtime-dependencies.json'));
  await assert.rejects(f.manager().initialize({allowDownloads:true}),/runtime_dependency_manifest_missing/);assert.equal(f.state.pip,0);assert.equal(f.state.venv,0);
}));
test('corrupted vendored wheel is refused before any pip invocation',()=>using({},async f=>{
  fs.appendFileSync(f.dependencyFile,'corrupted');await assert.rejects(f.manager().initialize({allowDownloads:true}),/runtime_dependency_integrity_failed/);
  assert.equal(f.state.pip,0);assert.equal(f.state.venv,0);
}));
test('model settings update only supplied fields and reject URLs with credentials or query',()=>using({},async f=>{
  const m=f.manager();await m.initialize({allowDownloads:true});const token=f.config().api_token;
  await m.saveModels({llm_url:'https://example.invalid/v1',llm_model:'synthetic-model'});await m.saveModels({embed_model:'synthetic-embedding'});
  assert.equal(f.config().llm_model,'synthetic-model');assert.equal(f.config().api_token,token);assert.equal(f.config().embed_model,'synthetic-embedding');
  const before=fs.readFileSync(f.configPath);for(const value of ['https://user:pass@example.invalid','https://example.invalid?api_key=hidden','file:///bad','https://example.invalid/#x']){
    await assert.rejects(m.saveModels({llm_url:value}),/model_url_invalid/);assert.ok(fs.readFileSync(f.configPath).equals(before));}
  await assert.rejects(m.saveModels({port:9999}),/model_fields_invalid/);await assert.rejects(m.saveModels({llm_model:'a\nb'}),/model_name_invalid/);
}));
test('start is isolated/idempotent, trusted connection private, tickets never permanent tokens',()=>using({},async f=>{
  const m=f.manager();await m.initialize({allowDownloads:true});assert.equal((await m.start()).running,true);await m.start();assert.equal(f.state.starts,1);
  const cfg=f.config(),connection=await m.connectionOptions(),status=await m.status(),panel=await m.panelUrl();
  assert.equal(connection.token,cfg.api_token);assert.equal(connection.configFile,f.configPath);assert.ok(connection.queueDir.startsWith(f.home));
  assert.ok(!JSON.stringify(status).includes(cfg.api_token));assert.ok(!JSON.stringify(status).includes(f.configPath));assert.ok(!panel.includes(cfg.api_token));assert.ok(panel.includes('/brain#ticket='));
  const start=f.calls.find(call=>call.args.at(-1)==='start');assert.equal(start.command,connection.pythonPath);assert.equal(start.args[0],'-I');assert.equal(start.args.at(-1),'start');
}));
test('saving models on running kernel requires explicit restart and never stops it',()=>using({},async f=>{
  const m=f.manager();await m.initialize({allowDownloads:true});await m.start();const saved=await m.saveModels({llm_model:'new-model'});
  assert.equal(saved.restart_required,true);assert.equal((await m.status()).restart_required,true);assert.equal(f.state.shutdowns,0);assert.equal(f.state.starts,1);
  const reloaded=f.manager();assert.equal((await reloaded.status()).restart_required,true);await reloaded.start();assert.equal((await reloaded.status()).restart_required,true);
  assert.equal(f.state.starts,1);assert.equal((await reloaded.stop()).stopped,true);await reloaded.start();assert.equal((await reloaded.status()).restart_required,false);
  assert.equal((await f.manager().status()).restart_required,false);assert.equal(f.state.starts,2);
}));
test('explicit stop uses authenticated shutdown, waits release, and preserves data',()=>using({},async f=>{
  const m=f.manager();await m.initialize({allowDownloads:true});await m.start();fs.writeFileSync(path.join(f.home,'data','keep'),'keep');
  assert.equal((await m.stop()).already_stopped,false);assert.equal(f.state.shutdowns,1);assert.equal((await m.stop()).already_stopped,true);
  assert.equal(fs.readFileSync(path.join(f.home,'data','keep'),'utf8'),'keep');assert.ok(fs.existsSync(f.configPath));
}));
test('unknown listener or wrong version never receives shutdown',()=>using({},async f=>{
  const m=f.manager();await m.initialize({allowDownloads:true});await f.serve(f.config(),{wrongToken:true});
  await assert.rejects(m.stop(),/kernel_identity_not_confirmed/);await assert.rejects(m.start(),/kernel_identity_not_confirmed/);assert.equal(f.state.starts,0);
  assert.equal(f.state.shutdowns,0);const old=f.state.server;f.state.server=null;old.closeAllConnections?.();await new Promise(resolve=>old.close(resolve));
  await f.serve(f.config(),{wrongVersion:true});await assert.rejects(m.stop(),/running_kernel_version_mismatch/);await assert.rejects(m.start(),/running_kernel_version_mismatch/);assert.equal(f.state.shutdowns,0);
}));
test('dispose never stops separately running kernel',()=>using({},async f=>{
  const m=f.manager();await m.initialize({allowDownloads:true});await m.start();await m.dispose();assert.equal(f.state.shutdowns,0);assert.ok(f.state.server);
  assert.equal((await m.status()).state,'disposed');await assert.rejects(m.start(),/runtime_manager_disposed/);await assert.rejects(m.stop(),/runtime_manager_disposed/);
}));
test('running kernel upgrade is refused without changing installed files',()=>using({},async f=>{
  const m=f.manager();await m.initialize({allowDownloads:true});await m.start();f.state.installedVersion='0.0.1';const count=f.state.pip;
  await assert.rejects(f.manager().initialize({allowDownloads:true}),/kernel_running_upgrade_requires_stop/);assert.equal(f.state.pip,count);assert.equal(f.state.shutdowns,0);
}));
test('stopped old environments require maintenance rather than in-place pip upgrade',()=>using({},async f=>{
  const m=f.manager();await m.initialize({allowDownloads:true});f.state.installedVersion='0.0.1';const count=f.state.pip,config=fs.readFileSync(f.configPath);
  fs.writeFileSync(path.join(f.home,'data','keep'),'old data');await assert.rejects(f.manager().initialize({allowDownloads:true}),/managed_kernel_upgrade_requires_maintenance/);
  assert.equal(f.state.pip,count);assert.ok(fs.readFileSync(f.configPath).equals(config));assert.equal(fs.readFileSync(path.join(f.home,'data','keep'),'utf8'),'old data');
}));
test('state paths reject package locations and symbolic links',()=>using({},async f=>{
  assert.throws(()=>createRuntimeManager({packageRoot:f.packageRoot,home:path.join(f.packageRoot,'private')}),/runtime_home_must_be_outside_package/);
  assert.throws(()=>createRuntimeManager({packageRoot:f.packageRoot,home:path.join(f.root,'node_modules','state')}),/runtime_home_must_be_outside_package/);
  fs.mkdirSync(f.home);const outside=path.join(f.root,'outside');fs.mkdirSync(outside);fs.symlinkSync(outside,path.join(f.home,'data'),process.platform==='win32'?'junction':'dir');
  await assert.rejects(f.manager().initialize({allowDownloads:true}),/runtime_state_symlink_refused/);assert.equal(f.state.pip,0);
}));
test('Windows DPAPI stores only ciphertext, retains empty keys, passes secrets only to own child',()=>using({},async f=>{
  const m=f.manager();await m.initialize({allowDownloads:true});
  if(process.platform!=='win32'){
    await assert.rejects(m.saveModels({llm_key:'synthetic-secret'}),/model_keys_require_environment_on_this_platform/);return;
  }
  const llm='test-only-llm-'+crypto.randomBytes(12).toString('hex'),embed='test-only-embed-'+crypto.randomBytes(12).toString('hex');
  await m.saveModels({llm_key:llm});await m.saveModels({embed_key:embed,llm_key:''});
  assert.equal((await m.status()).models.llm_key_saved,true);assert.equal((await m.status()).models.embed_key_saved,true);
  for(const name of ['private/config.json','runtime-manager.json','private/model-secrets.dpapi']){
    const bytes=fs.readFileSync(path.join(f.home,name));assert.ok(!bytes.includes(Buffer.from(llm)));assert.ok(!bytes.includes(Buffer.from(embed)));
  }
  assert.ok(!f.calls.some(call=>call.args.some(arg=>arg.includes(llm)||arg.includes(embed))));await m.start();
  const start=f.calls.find(call=>call.args.at(-1)==='start');assert.ok(start.options.env.MEMORY_BRAIN_LLM_KEY===llm);assert.ok(start.options.env.MEMORY_BRAIN_EMBED_KEY===embed);
  assert.ok(!JSON.stringify(await m.status()).includes(llm));assert.ok(!JSON.stringify(await m.status()).includes(embed));
}));
async function ownedProcessFamily(mode){
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'memory-brain-owned-helper-')),parentFile=path.join(root,'parent.pid'),descendantFile=path.join(root,'descendant.pid');
  const childSource=`require('node:fs').writeFileSync(process.argv[1],String(process.pid));setInterval(()=>{},1000);`;
  const parentSource=`const fs=require('node:fs'),{spawn}=require('node:child_process');fs.writeFileSync(process.argv[1],String(process.pid));`+
    `spawn(process.execPath,['-e',${JSON.stringify(childSource)},process.argv[2]],{stdio:['ignore','inherit','inherit'],windowsHide:true});`+
    `setInterval(()=>{if(${JSON.stringify(mode)}==='output'&&fs.existsSync(process.argv[2]))process.stdout.write('x'.repeat(4096));},20);`;
  const alive=pid=>{try{process.kill(pid,0);return true;}catch{return false;}};
  try{
    await assert.rejects(executeProcess(process.execPath,['-e',parentSource,parentFile,descendantFile],
      {cwd:root,env:{...process.env},timeout:mode==='timeout'?1200:5000,maxBuffer:mode==='output'?1024:65536}),
      mode==='timeout'?/owned_process_timeout/:/owned_process_output_limit/);
    assert.ok(fs.existsSync(parentFile)&&fs.existsSync(descendantFile),'both owned helper generations actually started');
    const pids=[parentFile,descendantFile].map(file=>Number(fs.readFileSync(file,'utf8')));
    const deadline=Date.now()+3000;while(pids.some(alive)&&Date.now()<deadline)await pause(30);
    assert.ok(pids.every(pid=>!alive(pid)),'helper process tree is gone before cleanup completes');
  }finally{fs.rmSync(root,{recursive:true,force:true});}
}
test('real helper timeout closes only its created child and descendant process tree',()=>ownedProcessFamily('timeout'));
test('real helper output limit also awaits owned descendant cleanup',()=>ownedProcessFamily('output'));

test('a failed secret replacement still persists the running-kernel restart requirement',()=>using({},async f=>{
  const m=f.manager();await m.initialize({allowDownloads:true});await m.start();
  const before=fs.readFileSync(f.configPath),rename=fs.renameSync;
  try{
    fs.renameSync=(from,to)=>{if(String(to).endsWith('model-secrets.dpapi'))throw Object.assign(new Error('synthetic_write_failure'),{code:'EACCES'});return rename(from,to);};
    if(process.platform==='win32')await assert.rejects(m.saveModels({llm_key:'only-synthetic-key'}));
    else {
      fs.renameSync=(from,to)=>{if(String(to)===f.configPath)throw Object.assign(new Error('synthetic_write_failure'),{code:'EACCES'});return rename(from,to);};
      await assert.rejects(m.saveModels({llm_model:'synthetic-new-model'}));
    }
  }finally{fs.renameSync=rename;}
  assert.equal((await f.manager().status()).restart_required,true);
  assert.ok(fs.readFileSync(f.configPath).equals(before));
}));

test('unconfirmed installer cleanup quarantines that home and refuses another installation',()=>using({},async f=>{
  const execute=async(command,args,options)=>{if(args.includes('pip'))throw Object.assign(new Error('only a synthetic cleanup error'),{code:'owned_process_cleanup_failed'});return f.execute(command,args,options);};
  const manager=createRuntimeManager({packageRoot:f.packageRoot,home:f.home,execute});
  await assert.rejects(manager.initialize({allowDownloads:true}),/owned_process_cleanup_failed/);
  assert.equal(JSON.parse(fs.readFileSync(f.markerPath,'utf8')).install_cleanup_pending,true);
  assert.equal((await f.manager().status()).error,'previous_install_cleanup_unconfirmed');
  await assert.rejects(f.manager().initialize({allowDownloads:true}),/previous_install_cleanup_unconfirmed/);
  assert.equal(f.state.pip,0);assert.equal(fs.existsSync(f.configPath),false);
}));

let failures=0;
for(const {name,operation} of tests){try{await operation();console.log('PASS '+name);}catch(error){failures++;console.error('FAIL '+name+'\n'+(error.stack||error));}}
console.log(JSON.stringify({passed:tests.length-failures,failed:failures,total:tests.length}));if(failures)process.exitCode=1;
