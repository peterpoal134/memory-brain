/** User-owned first-run kernel installation. Package/node_modules stay read-only.
 * No model calls, service shutdown, Python download or UI-supplied commands.
 */
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import net from 'node:net';
import crypto from 'node:crypto';
import { spawn } from 'node:child_process';

const OWNER='local-owner',LLM_KEY='MEMORY_BRAIN_LLM_KEY',EMBED_KEY='MEMORY_BRAIN_EMBED_KEY';
const MANAGER='memory-brain-runtime-home';
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const hash=value=>crypto.createHash('sha256').update(value).digest('hex');
const normalize=value=>process.platform==='win32'?path.resolve(value).toLowerCase():path.resolve(value);
const inside=(root,target)=>{const relative=path.relative(root,target);return relative===''||relative!=='..'&&!relative.startsWith('..'+path.sep)&&!path.isAbsolute(relative);};
const safeError=code=>Object.assign(new Error(code),{code});

export async function resolveRuntimeHome({packageRoot,runtimeHome,profile='default'}={}){
  if(packageRoot!==undefined&&(typeof packageRoot!=='string'||!path.isAbsolute(packageRoot)))throw safeError('package_root_must_be_absolute');
  if(runtimeHome!==undefined){
    if(typeof runtimeHome!=='string'||!path.isAbsolute(runtimeHome)||runtimeHome.includes('\0'))throw safeError('runtime_home_must_be_absolute');
    return path.resolve(runtimeHome);
  }
  if(typeof profile!=='string'||!profile||profile.length>4096||profile.includes('\0'))throw safeError('profile_identifier_invalid');
  const stem=profile.replace(/[^a-zA-Z0-9_-]/g,'_').slice(0,36)||'profile';
  const ident=stem+'-'+hash(profile).slice(0,16);
  const base=process.platform==='win32'?(process.env.LOCALAPPDATA||path.join(os.homedir(),'AppData','Local')):
    path.join(os.homedir(),'.local','share');
  return path.join(base,process.platform==='win32'?'MemoryBrain':'memory-brain','profiles',ident);
}

// Exported for a bounded real-process regression, not a UI command interface.
export async function executeProcess(command,args,{cwd,env,input='',timeout=30000,maxBuffer=262144,windowsHide=true}={}){
  return new Promise((resolve,reject)=>{
    const child=spawn(command,args,{cwd,env,windowsHide,shell:false,detached:process.platform!=='win32',stdio:['pipe','pipe','pipe']});
    let stdout='',stderr='',ended=false,terminating=false,closed=false,closeResolve;
    const closePromise=new Promise(resolve=>closeResolve=resolve);
    const timer=setTimeout(()=>{void failAfterCleanup('owned_process_timeout');},timeout);
    const finish=(error,result)=>{if(ended)return;ended=true;clearTimeout(timer);error?reject(error):resolve(result);};
    async function failAfterCleanup(code){
      if(ended||terminating)return;terminating=true;clearTimeout(timer);
      try{
        if(!closed&&Number.isInteger(child.pid)&&child.pid>0){
          if(process.platform==='win32'){
            // This PID is the still-live ChildProcess created immediately
            // above. Never scan python.exe, user services or unrelated PIDs.
            if(child.exitCode!==null||child.signalCode!==null)throw safeError('owned_process_cleanup_failed');
            await new Promise((resolve,reject)=>{
              const killer=spawn('taskkill.exe',['/PID',String(child.pid),'/T','/F'],{windowsHide:true,shell:false,stdio:'ignore'});
              const limit=setTimeout(()=>{killer.kill();reject(safeError('owned_process_cleanup_failed'));},10000);
              killer.once('error',()=>{clearTimeout(limit);reject(safeError('owned_process_cleanup_failed'));});
              killer.once('close',result=>{clearTimeout(limit);result===0||closed?resolve():reject(safeError('owned_process_cleanup_failed'));});
            });
          }else{
            // A dedicated group contains only this helper and its children;
            // the application's process group is never targeted.
            try{process.kill(-child.pid,'SIGKILL');}catch(error){if(error.code!=='ESRCH')throw error;}
          }
          await Promise.race([closePromise,delay(5000).then(()=>{if(!closed)throw safeError('owned_process_cleanup_failed');})]);
        }
        finish(safeError(code));
      }catch{finish(safeError('owned_process_cleanup_failed'));}
    }
    const capture=key=>chunk=>{
      if(terminating||ended)return;
      if(key==='stdout')stdout+=chunk.toString('utf8');else stderr+=chunk.toString('utf8');
      if(Buffer.byteLength(stdout)+Buffer.byteLength(stderr)>maxBuffer)void failAfterCleanup('owned_process_output_limit');
    };
    child.stdout.on('data',capture('stdout'));child.stderr.on('data',capture('stderr'));
    child.on('error',()=>finish(safeError('owned_process_unavailable')));
    child.on('close',code=>{closed=true;closeResolve();if(!terminating)finish(null,{code,stdout,stderr});});
    child.stdin.on('error',()=>{});child.stdin.end(input);
  });
}

// Fixed code only. Secrets and ciphertext travel on stdin, never argv, shell
// interpolation, manager logs, status responses or a temporary plaintext file.
const DPAPI_SCRIPT=String.raw`
$ErrorActionPreference='Stop'
[Console]::InputEncoding=[Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Security
$request=[Console]::In.ReadToEnd() | ConvertFrom-Json
if($request.operation -eq 'protect') {
  $clear=[Text.Encoding]::UTF8.GetBytes([string]$request.data)
  try {
    $cipher=[Security.Cryptography.ProtectedData]::Protect($clear,$null,[Security.Cryptography.DataProtectionScope]::CurrentUser)
    [Console]::Out.Write([Convert]::ToBase64String($cipher))
  } finally { [Array]::Clear($clear,0,$clear.Length) }
} elseif($request.operation -eq 'unprotect') {
  $cipher=[Convert]::FromBase64String([string]$request.data)
  $clear=[Security.Cryptography.ProtectedData]::Unprotect($cipher,$null,[Security.Cryptography.DataProtectionScope]::CurrentUser)
  try { [Console]::Out.Write([Text.Encoding]::UTF8.GetString($clear)) }
  finally { [Array]::Clear($clear,0,$clear.Length) }
} else { throw 'unsupported_operation' }
`;
const PYTHON_PROBE='import sys,json; print(json.dumps({"major":sys.version_info.major,"minor":sys.version_info.minor,"version":sys.version.split()[0]}))';
const INSTALLED_PROBE='import sys,json,importlib.metadata as m\ntry: v=m.version("xiaojiu-brain")\nexcept m.PackageNotFoundError: v=None\nprint(json.dumps({"version":v,"prefix":sys.prefix}))';

function readJSON(filename){return JSON.parse(fs.readFileSync(filename,'utf8').replace(/^\uFEFF/,''));}
function writeAtomic(filename,value,{exclusive=false}={}){
  const bytes=Buffer.isBuffer(value)?value:Buffer.from(JSON.stringify(value,null,2)+'\n');
  const temporary=path.join(path.dirname(filename),'.runtime-'+crypto.randomBytes(10).toString('hex')+'.tmp');
  const descriptor=fs.openSync(temporary,'wx',0o600);
  try{fs.writeFileSync(descriptor,bytes);fs.fsyncSync(descriptor);}finally{fs.closeSync(descriptor);}
  try{
    if(exclusive){fs.linkSync(temporary,filename);fs.unlinkSync(temporary);}
    else fs.renameSync(temporary,filename);
  }finally{try{fs.unlinkSync(temporary);}catch{}}
}

export function createRuntimeManager({packageRoot,home,execute=executeProcess}={}){
  if(typeof packageRoot!=='string'||!path.isAbsolute(packageRoot)||typeof home!=='string'||!path.isAbsolute(home))throw safeError('runtime_paths_must_be_absolute');
  const root=fs.realpathSync(packageRoot),runtimeHome=path.resolve(home);
  if(inside(root,runtimeHome)||runtimeHome.split(path.sep).some(part=>part.toLowerCase()==='node_modules'))throw safeError('runtime_home_must_be_outside_package');
  const match=fs.readFileSync(path.join(root,'pyproject.toml'),'utf8').match(/^version\s*=\s*"([0-9][A-Za-z0-9.+-]*)"\s*$/m);
  if(!match)throw safeError('package_version_unavailable');
  const expectedVersion=match[1],privateDir=path.join(runtimeHome,'private'),configFile=path.join(privateDir,'config.json');
  const markerFile=path.join(runtimeHome,'runtime-manager.json'),lockFile=path.join(runtimeHome,'.runtime-manager.lock');
  const secretsFile=path.join(privateDir,'model-secrets.dpapi');
  const pythonPath=path.join(runtimeHome,'.venv',process.platform==='win32'?'Scripts':'bin',process.platform==='win32'?'python.exe':'python');
  let disposed=false,state='not_initialized',error=null,restartRequired=false,pythonCache=null,installedCache=null;
  const managedPaths=['private','data','logs','tmp','.venv'];
  function contained(filename){if(!inside(runtimeHome,path.resolve(filename)))throw safeError('runtime_path_outside_home');return filename;}
  function checkPaths(){
    if(fs.existsSync(runtimeHome)&&fs.lstatSync(runtimeHome).isSymbolicLink())throw safeError('runtime_home_symlink_refused');
    for(const name of [...managedPaths,'private/config.json','private/model-secrets.dpapi','runtime-manager.json','.runtime-manager.lock']){
      const target=contained(path.join(runtimeHome,name));
      if(fs.existsSync(target)&&fs.lstatSync(target).isSymbolicLink())throw safeError('runtime_state_symlink_refused');
    }
  }
  function prepareDirectories(){
    checkPaths();fs.mkdirSync(runtimeHome,{recursive:true,mode:0o700});
    for(const name of ['private','data','logs','tmp'])fs.mkdirSync(contained(path.join(runtimeHome,name)),{recursive:true,mode:0o700});
    checkPaths();
  }
  function env(){
    const value={...process.env,PYTHONUTF8:'1',PYTHONIOENCODING:'utf-8',TEMP:path.join(runtimeHome,'tmp'),
      TMP:path.join(runtimeHome,'tmp'),TMPDIR:path.join(runtimeHome,'tmp'),NO_PROXY:'127.0.0.1,localhost,::1',no_proxy:'127.0.0.1,localhost,::1',
      PIP_DISABLE_PIP_VERSION_CHECK:'1',PIP_NO_INPUT:'1'};
    delete value.XIAOJIU_BRAIN_TOKEN;delete value.PYTHONPATH;delete value.PYTHONHOME;
    // Installation has one explicit public index. Ignore inherited pip config,
    // custom URLs and additional indexes, including URLs that contain secrets.
    for(const key of Object.keys(value))if(key.toUpperCase().startsWith('PIP_'))delete value[key];
    Object.assign(value,{PIP_CONFIG_FILE:os.devNull,PIP_DISABLE_PIP_VERSION_CHECK:'1',PIP_NO_INPUT:'1'});
    return value;
  }
  async function run(command,args,{input='',timeout=30000,extraEnv={}}={}){
    let result;
    try{result=await execute(command,args,{cwd:fs.existsSync(runtimeHome)?runtimeHome:root,env:{...env(),...extraEnv},input,timeout,maxBuffer:262144,windowsHide:true});}
    catch(caught){
      if(['owned_process_timeout','owned_process_output_limit','owned_process_unavailable','owned_process_cleanup_failed'].includes(caught?.code))throw safeError(caught.code);
      throw safeError('owned_process_failed');
    }
    if(!result||result.code!==undefined&&result.code!==0||result.exitCode!==undefined&&result.exitCode!==0)throw safeError('owned_process_failed');
    return String(result.stdout||'').replace(/^\uFEFF/,'').trim();
  }
  function marker(){
    checkPaths();if(!fs.existsSync(markerFile))return null;
    let value;try{value=readJSON(markerFile);}catch{throw safeError('runtime_manager_marker_invalid');}
    if(value.manager!==MANAGER||value.schema_version!==1)throw safeError('runtime_manager_marker_invalid');
    return value;
  }
  function config(){
    checkPaths();const owned=marker();if(!owned)throw safeError(fs.existsSync(configFile)?'unmanaged_home_configuration':'runtime_not_initialized');
    if(!fs.existsSync(configFile))throw safeError('runtime_not_initialized');
    let value;try{value=readJSON(configFile);}catch{throw safeError('local_configuration_invalid');}
    const data=path.resolve(privateDir,value.data_dir||'');
    if(normalize(data)!==normalize(path.join(runtimeHome,'data'))||!['127.0.0.1','localhost','::1'].includes(value.host)||!Number.isInteger(value.port)||value.port<18880||value.port>18899||
      value.owner!==OWNER||typeof value.api_token!=='string'||value.api_token.length<24)throw safeError('local_configuration_invalid');
    if(value.llm_key_env!==LLM_KEY||value.embed_key_env!==EMBED_KEY)throw safeError('model_key_environment_not_owned');
    return {...value,data_dir:data};
  }
  const baseUrl=value=>'http://'+(value.host==='::1'?'[::1]':value.host)+':'+value.port;
  async function http(value,endpoint,body,timeout=900){
    try{
      const response=await fetch(baseUrl(value)+endpoint,{method:body===undefined?'GET':'POST',redirect:'error',signal:AbortSignal.timeout(timeout),
        headers:{Authorization:'Bearer '+value.api_token,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
      const result=await response.json();return response.ok&&result?.ok===true?result.data:null;
    }catch{return null;}
  }
  async function probePython(){
    if(pythonCache&&Date.now()-pythonCache.at<60000)return pythonCache.value;
    const candidates=[];
    if(process.platform==='win32'){
      // Enumeration never requests a new interpreter. Probe already installed
      // absolute executables, preferring the package's tested Python 3.11.
      // Do not ask a newer `py -3.x` manager to acquire a missing interpreter.
      let listing='';for(const flag of ['--list-paths','-0p']){
        try{listing=await run('py',[flag],{timeout:6000});if(listing)break;}catch{}
      }
      const found=[];const pattern=/^\s*-(?:V:)?(3)\.(\d+)[^\s]*\s+\*?\s*(.+?python(?:w)?\.exe)\s*$/gim;
      for(const match of listing.matchAll(pattern)){
        const minor=Number(match[2]),executable=match[3].trim();
        if(minor>=10&&path.isAbsolute(executable)&&fs.existsSync(executable))found.push({minor,executable});
      }
      const priority=minor=>({11:0,12:1,13:2,10:3})[minor]??4;
      found.sort((a,b)=>priority(a.minor)-priority(b.minor)||a.minor-b.minor);
      for(const item of found)if(!candidates.some(([command])=>normalize(command)===normalize(item.executable)))candidates.push([item.executable,[]]);
      candidates.push(['python',[]],['python3',[]]);
    }else candidates.push(['python3',[]],['python',[]]);
    for(const [command,prefix] of candidates){
      try{const data=JSON.parse(await run(command,[...prefix,'-I','-c',PYTHON_PROBE],{timeout:6000}));
        if(data.major===3&&Number.isInteger(data.minor)&&data.minor>=10){const value={command,prefix,version:data.version};pythonCache={at:Date.now(),value};return value;}}
      catch{}
    }
    pythonCache={at:Date.now(),value:null};return null;
  }
  async function installed(){
    if(installedCache&&Date.now()-installedCache.at<60000)return installedCache.value;
    if(!fs.existsSync(pythonPath))return null;
    try{const data=JSON.parse(await run(pythonPath,['-I','-c',INSTALLED_PROBE],{timeout:6000}));
      if(!data.prefix||normalize(data.prefix)!==normalize(path.join(runtimeHome,'.venv')))return null;
      const value=typeof data.version==='string'?data.version:null;installedCache={at:Date.now(),value};return value;
    }catch{return null;}
  }
  async function withLock(operation){
    prepareDirectories();const nonce=crypto.randomBytes(16).toString('hex'),until=Date.now()+15000;
    for(;;){
      checkPaths();
      try{const descriptor=fs.openSync(lockFile,'wx',0o600);try{fs.writeFileSync(descriptor,JSON.stringify({pid:process.pid,nonce}));fs.fsyncSync(descriptor);}finally{fs.closeSync(descriptor);}break;}
      catch(caught){
        if(caught.code!=='EEXIST')throw safeError('runtime_home_lock_failed');
        try{const before=fs.readFileSync(lockFile),old=JSON.parse(before);let alive=true;
          if(Number.isInteger(old.pid)&&old.pid>0&&typeof old.nonce==='string'){
            try{process.kill(old.pid,0);}catch(problem){if(problem.code==='ESRCH')alive=false;}
            if(!alive&&fs.readFileSync(lockFile).equals(before)){fs.unlinkSync(lockFile);continue;}
          }
        }catch{}
        if(Date.now()>=until)throw safeError('runtime_home_busy');await delay(75);
      }
    }
    try{return await operation();}
    finally{try{if(readJSON(lockFile).nonce===nonce)fs.unlinkSync(lockFile);}catch{}}
  }
  async function freePort(){
    for(let port=18880;port<=18899;port++){
      const available=await new Promise(resolve=>{const server=net.createServer();server.once('error',()=>resolve(false));
        server.listen({host:'127.0.0.1',port,exclusive:true},()=>server.close(()=>resolve(true)));});
      if(available)return port;
    }throw safeError('no_free_local_kernel_port');
  }
  function wheel(){
    const directory=path.join(root,'wheels');if(!fs.existsSync(directory))return null;
    const names=fs.readdirSync(directory).filter(name=>/^xiaojiu_brain-.*\.whl$/.test(name));
    if(names.length>1)throw safeError('ambiguous_kernel_wheel');
    if(!names.length)return null;
    if(!names[0].startsWith('xiaojiu_brain-'+expectedVersion+'-'))throw safeError('kernel_wheel_version_mismatch');
    return path.join(directory,names[0]);
  }
  function runtimeDependencies(){
    const file=path.join(root,'compat','runtime-dependencies.json'),directory=path.join(root,'wheels');
    if(!fs.existsSync(file)||fs.lstatSync(file).isSymbolicLink())throw safeError('runtime_dependency_manifest_missing');
    let manifest;try{manifest=readJSON(file);}catch{throw safeError('runtime_dependency_manifest_invalid');}
    // This release has exactly one platform-conditional runtime dependency.
    // Package metadata changes must ship an explicitly reviewed new manifest.
    if(manifest?.format!==1||!Array.isArray(manifest.files)||manifest.files.length!==1||
      !fs.existsSync(directory)||fs.lstatSync(directory).isSymbolicLink())throw safeError('runtime_dependency_manifest_invalid');
    for(const item of manifest.files){
      if(!item||typeof item!=='object'||typeof item.filename!=='string'||
        !/^tzdata-[0-9][A-Za-z0-9.+-]*-py2\.py3-none-any\.whl$/.test(item.filename)||
        !/^[a-f0-9]{64}$/.test(item.sha256||'')||item.license!=='Apache-2.0'||
        typeof item.source!=='string'||!item.source.startsWith('https://files.pythonhosted.org/'))throw safeError('runtime_dependency_manifest_invalid');
      const dependency=path.join(directory,item.filename);
      if(!fs.existsSync(dependency)||!fs.statSync(dependency).isFile()||fs.lstatSync(dependency).isSymbolicLink())throw safeError('runtime_dependency_missing');
      if(hash(fs.readFileSync(dependency))!==item.sha256)throw safeError('runtime_dependency_integrity_failed');
    }
    return directory;
  }
  function stageSource(){
    const target=fs.mkdtempSync(path.join(runtimeHome,'tmp','kernel-source-'));
    for(const name of ['pyproject.toml','README.md','LICENSE','NOTICE.md','MANIFEST.in']){
      const source=path.join(root,name);if(fs.existsSync(source))fs.copyFileSync(source,path.join(target,name));
    }
    const copy=(from,to)=>{
      fs.mkdirSync(to,{recursive:true});
      for(const entry of fs.readdirSync(from,{withFileTypes:true})){
        if(entry.name==='__pycache__'||entry.name.startsWith('.'))continue;
        if(entry.isSymbolicLink())throw safeError('source_package_symlink_refused');
        const source=path.join(from,entry.name),dest=path.join(to,entry.name);
        if(entry.isDirectory())copy(source,dest);
        else if(entry.isFile()&&['.py','.js','.css','.html','.svg','.bin','.json','.md','.png'].includes(path.extname(entry.name)))fs.copyFileSync(source,dest);
      }
    };
    copy(path.join(root,'xiaojiu_brain'),path.join(target,'xiaojiu_brain'));return target;
  }
  async function sourceWheel(source){
    const helper=path.join(root,'scripts','build_runtime_wheel.py');
    if(!fs.existsSync(helper)||fs.lstatSync(helper).isSymbolicLink())throw safeError('runtime_wheel_builder_missing');
    const directory=fs.mkdtempSync(path.join(runtimeHome,'tmp','kernel-wheel-'));
    try{
      let result;try{result=JSON.parse(await run(pythonPath,['-I',helper,'--source',source,'--out',directory],{timeout:30000}));}
      catch(caught){throw safeError(caught.code||'runtime_wheel_build_failed');}
      if(result?.ok!==true||typeof result.wheel!=='string'||!path.isAbsolute(result.wheel)||
        normalize(path.dirname(result.wheel))!==normalize(directory)||!path.basename(result.wheel).startsWith('xiaojiu_brain-'+expectedVersion+'-')||
        !result.wheel.endsWith('.whl')||!fs.existsSync(result.wheel)||fs.lstatSync(result.wheel).isSymbolicLink()||
        hash(fs.readFileSync(result.wheel))!==result.sha256)throw safeError('runtime_wheel_build_failed');
      return {file:result.wheel,directory};
    }catch(caught){
      if(caught.code!=='owned_process_cleanup_failed')try{fs.rmSync(directory,{recursive:true,force:true});}catch{}
      throw caught;
    }
  }
  async function dpapi(operation,data){
    if(process.platform!=='win32')throw safeError('model_keys_require_environment_on_this_platform');
    const output=await run('powershell.exe',['-NoProfile','-NonInteractive','-Command',DPAPI_SCRIPT],
      {input:JSON.stringify({operation,data}),timeout:10000});
    if(operation==='protect'){
      if(!/^[A-Za-z0-9+/]+={0,2}$/.test(output))throw safeError('model_secret_protection_failed');return Buffer.from(output,'base64');
    }
    let result;try{result=JSON.parse(output);}catch{throw safeError('model_secret_unprotect_failed');}
    if(!result||typeof result!=='object'||Array.isArray(result)||Object.keys(result).some(key=>![LLM_KEY,EMBED_KEY].includes(key))||
      Object.values(result).some(value=>typeof value!=='string'))throw safeError('model_secret_unprotect_failed');
    return result;
  }
  async function loadSecrets(){return fs.existsSync(secretsFile)?dpapi('unprotect',fs.readFileSync(secretsFile).toString('base64')):{};}
  async function status(){
    let cfg=null,owned=null,installedVersion=null,running=false,python=null,problem=error;
    try{checkPaths();owned=marker();if(owned?.install_cleanup_pending)problem='previous_install_cleanup_unconfirmed';if(fs.existsSync(configFile))cfg=config();installedVersion=await installed();python=await probePython();
      if(cfg){const reply=await http(cfg,'/v1/status');running=typeof reply?.version==='string';if(running&&reply.version!==expectedVersion)problem='running_kernel_version_mismatch';}
    }catch(caught){problem=typeof caught.code==='string'?caught.code:'runtime_status_unavailable';}
    const derived=disposed?'disposed':state==='initializing'?'initializing':problem?'error':
      running?'running':cfg&&installedVersion===expectedVersion?'ready':installedVersion&&installedVersion!==expectedVersion?'upgrade_required':'not_initialized';
    return {state:derived,expected_version:expectedVersion,installed_version:installedVersion,
      python:{available:Boolean(python),version:python?.version||null},
      models:{embedding_configured:Boolean(cfg?.embed_url&&cfg?.embed_model),derivation_configured:Boolean(cfg?.llm_url&&cfg?.llm_model),
        embed_key_saved:Boolean(fs.existsSync(secretsFile)&&owned?.secrets?.embed_key),llm_key_saved:Boolean(fs.existsSync(secretsFile)&&owned?.secrets?.llm_key)},
      running,restart_required:Boolean(restartRequired||owned?.restart_required),error:problem||null,
      key_storage:process.platform==='win32'?'windows_current_user_dpapi':'environment_only'};
  }
  async function initialize({allowDownloads=false}={}){
    if(disposed)throw safeError('runtime_manager_disposed');
    return withLock(async()=>{
      state='initializing';error=null;
      try{
        let owned=marker();
        if(owned?.install_cleanup_pending)throw safeError('previous_install_cleanup_unconfirmed');
        if(!owned&&fs.existsSync(configFile))throw safeError('unmanaged_home_configuration');
        if(!owned){owned={schema_version:1,manager:MANAGER,created_at:new Date().toISOString(),secrets:{embed_key:false,llm_key:false}};writeAtomic(markerFile,owned,{exclusive:true});}
        const version=await installed();
        if(version!==expectedVersion){
          if(fs.existsSync(configFile)){
            const current=config();if(await http(current,'/v1/status'))throw safeError('kernel_running_upgrade_requires_stop');
          }
          // First release deliberately refuses an in-place upgrade of an
          // existing environment. A failed pip upgrade must not damage it.
          if(version!==null)throw safeError('managed_kernel_upgrade_requires_maintenance');
          if(allowDownloads!==true)throw safeError('dependency_downloads_not_authorized');
          const dependencies=runtimeDependencies();
          const python=await probePython();if(!python)throw safeError('python_310_required');
          if(!fs.existsSync(pythonPath))await run(python.command,[...python.prefix,'-I','-m','venv',path.join(runtimeHome,'.venv')],{timeout:90000});
          const packaged=wheel(),source=packaged?null:stageSource();let generated=null,installationError=null;
          try{
            if(!packaged)generated=await sourceWheel(source);
            await run(pythonPath,['-I','-m','pip','install','--no-index','--find-links',dependencies,'--only-binary=:all:',
              '--no-input','--disable-pip-version-check','--progress-bar','off','--no-cache-dir',packaged||generated.file],{timeout:240000});
          }catch(caught){installationError=caught;throw caught;}
          finally{
            if(installationError?.code!=='owned_process_cleanup_failed')for(const temporary of [source,generated?.directory]){
              if(temporary&&inside(path.join(runtimeHome,'tmp'),temporary)&&/^kernel-(?:source|wheel)-/.test(path.basename(temporary)))
                try{fs.rmSync(temporary,{recursive:true,force:true});}catch{}
            }
          }
          installedCache=null;if(await installed()!==expectedVersion)throw safeError('installed_kernel_version_mismatch');
        }
        if(!fs.existsSync(configFile))await run(pythonPath,['-I','-m','xiaojiu_brain','--config',configFile,'init',
          '--data-dir',path.join(runtimeHome,'data'),'--port',String(await freePort()),'--owner',OWNER],{timeout:15000});
        // CLI owns exclusive config creation. Only this manager's new/retrying
        // home receives the neutral environment names; no token/data is reset.
        const raw=readJSON(configFile);
        if(raw.llm_key_env!==LLM_KEY||raw.embed_key_env!==EMBED_KEY){
          if(owned.initialized_at)throw safeError('model_key_environment_not_owned');
          raw.llm_key_env=LLM_KEY;raw.embed_key_env=EMBED_KEY;writeAtomic(configFile,raw);
        }
        config();owned.version=expectedVersion;owned.initialized_at||=new Date().toISOString();writeAtomic(markerFile,owned);state='ready';
        return await status();
      }catch(caught){
        error=typeof caught.code==='string'?caught.code:'runtime_initialization_failed';state='error';
        if(error==='owned_process_cleanup_failed'){
          const owned=marker();if(owned){owned.install_cleanup_pending=true;writeAtomic(markerFile,owned);}
        }
        throw safeError(error);
      }
    });
  }
  async function start(){
    if(disposed)throw safeError('runtime_manager_disposed');
    return withLock(async()=>{
      try{
        const cfg=config();if(await installed()!==expectedVersion)throw safeError('kernel_initialization_required');
        const current=await http(cfg,'/v1/status');
        if(current){if(current.version!==expectedVersion)throw safeError('running_kernel_version_mismatch');state='running';error=null;return await status();}
        if(await portOpen(cfg))throw safeError('kernel_identity_not_confirmed');
        const secrets=await loadSecrets();
        await run(pythonPath,['-I','-m','xiaojiu_brain','--config',configFile,'start'],{timeout:30000,extraEnv:secrets});
        const ready=await http(cfg,'/v1/status',undefined,2000);
        if(ready?.version!==expectedVersion)throw safeError('owned_kernel_readiness_not_confirmed');
        const owned=marker();owned.restart_required=false;writeAtomic(markerFile,owned);
        state='running';error=null;restartRequired=false;return await status();
      }catch(caught){error=typeof caught.code==='string'?caught.code:'owned_kernel_start_failed';throw safeError(error);}
    });
  }
  async function portOpen(value){
    return new Promise(resolve=>{
      let finished=false;const socket=net.createConnection({host:value.host,port:value.port});
      const done=result=>{if(finished)return;finished=true;socket.destroy();resolve(result);};
      socket.setTimeout(350,()=>done(true));socket.once('connect',()=>done(true));socket.once('error',()=>done(false));
    });
  }
  async function stop(){
    if(disposed)throw safeError('runtime_manager_disposed');
    return withLock(async()=>{
      const cfg=config(),current=await http(cfg,'/v1/status');
      if(!current){if(await portOpen(cfg))throw safeError('kernel_identity_not_confirmed');return {stopped:true,already_stopped:true};}
      if(current.version!==expectedVersion)throw safeError('running_kernel_version_mismatch');
      // Authentication uses only this manager-owned configuration. Never kill a
      // PID or infer service identity from a listening port alone.
      const reply=await http(cfg,'/v1/admin/shutdown',{},2000);
      if(reply?.stopping!==true)throw safeError('owned_kernel_shutdown_not_confirmed');
      const until=Date.now()+12000;
      while(Date.now()<until){if(!await portOpen(cfg)){state='ready';error=null;return {stopped:true,already_stopped:false};}await delay(100);}
      throw safeError('owned_kernel_shutdown_pending');
    });
  }
  function modelURL(value){
    if(typeof value!=='string'||value.length>2000||/[\x00-\x20]/.test(value))throw safeError('model_url_invalid');
    if(value==='')return '';
    let parsed;try{parsed=new URL(value);}catch{throw safeError('model_url_invalid');}
    if(!['http:','https:'].includes(parsed.protocol)||!parsed.hostname||parsed.username||parsed.password||parsed.search||parsed.hash)throw safeError('model_url_invalid');
    return value;
  }
  async function saveModels(values){
    if(disposed)throw safeError('runtime_manager_disposed');
    const allowed=['embed_url','embed_model','llm_url','llm_model','embed_key','llm_key'];
    if(!values||typeof values!=='object'||Array.isArray(values)||Object.keys(values).some(key=>!allowed.includes(key)))throw safeError('model_fields_invalid');
    const changes={},keys={};
    for(const [key,value] of Object.entries(values)){
      if(key.endsWith('_url'))changes[key]=modelURL(value);
      else if(key.endsWith('_model')){
        if(typeof value!=='string'||value.length>200||/[\x00-\x1f\x7f]/.test(value))throw safeError('model_name_invalid');changes[key]=value.trim();
      }else{
        if(typeof value!=='string'||value.length>4096||/[\x00-\x20\x7f]/.test(value))throw safeError('model_key_invalid');
        if(value)keys[key==='llm_key'?LLM_KEY:EMBED_KEY]=value;
      }
    }
    if(Object.keys(keys).length&&process.platform!=='win32')throw safeError('model_keys_require_environment_on_this_platform');
    return withLock(async()=>{
      const cfg=config(),before=fs.readFileSync(configFile),owned=marker();
      const running=Boolean(await http(cfg,'/v1/status'));
      // Conservatively persist before replacing either ciphertext or config.
      // A crash/failure can require one extra restart, never silently claim
      // that an old process has already adopted a newly saved API key.
      owned.restart_required=Boolean(running||owned.restart_required);writeAtomic(markerFile,owned);
      restartRequired=Boolean(owned.restart_required);
      if(Object.keys(keys).length){
        const secrets={...await loadSecrets(),...keys},cipher=await dpapi('protect',JSON.stringify(secrets));
        writeAtomic(secretsFile,cipher);owned.secrets={embed_key:Boolean(secrets[EMBED_KEY]),llm_key:Boolean(secrets[LLM_KEY])};
      }
      if(!fs.readFileSync(configFile).equals(before))throw safeError('local_configuration_changed');
      writeAtomic(markerFile,owned);
      writeAtomic(configFile,{...readJSON(configFile),...changes});restartRequired=Boolean(owned.restart_required);error=null;
      return {saved:true,restart_required:Boolean(owned.restart_required),models:(await status()).models};
    });
  }
  async function panelUrl(){
    if(disposed)throw safeError('runtime_manager_disposed');const cfg=config();
    const current=await http(cfg,'/v1/status');if(current?.version!==expectedVersion)throw safeError('owned_kernel_not_running');
    const reply=await http(cfg,'/v1/session/ticket',{},2000);
    if(typeof reply?.ticket!=='string'||!/^[A-Za-z0-9_-]{40,}$/.test(reply.ticket))throw safeError('panel_ticket_unavailable');
    return baseUrl(cfg)+'/brain#ticket='+reply.ticket;
  }
  async function connectionOptions(){
    if(disposed)throw safeError('runtime_manager_disposed');const cfg=config();
    // Trusted in-process adapter activation only. Never return this object from
    // an HTTP/UI status endpoint: it deliberately contains the management token.
    return {configFile,pythonPath,baseUrl:baseUrl(cfg),token:cfg.api_token,queueDir:path.join(cfg.data_dir,'dsh-queue'),home:runtimeHome};
  }
  async function dispose(){disposed=true;/* The separately owned kernel is never killed on plugin unload. */}
  return Object.freeze({status,initialize,start,stop,saveModels,panelUrl,connectionOptions,dispose});
}
