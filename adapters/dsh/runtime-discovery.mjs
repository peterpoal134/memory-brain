/** Bounded discovery of the running host, including Electron's virtual ASAR FS.
 * No extraction, package download, disk scan or compatibility bypass happens here.
 */
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';

const MAX_CANDIDATES=32,MAX_SEARCH_ROOTS=8,MAX_ATTEMPTS=32;
const required=['dsh-agent-loop','dsh-session','dsh-llm','dsh-tools'];
const partialReasons=new Set(['runtime_unavailable','asar_runtime_unavailable','runtime_access_denied','required_modules_missing','required_modules_unreadable']);
const osCodes=new Set(['ENOENT','ENOTDIR','EACCES','EPERM','EISDIR','EINVAL','ELOOP','ENAMETOOLONG','EIO','EMFILE','ENFILE','ENOMEM','ENOTSUP','ENOSYS','EBUSY','EROFS','ETIMEDOUT','MODULE_NOT_FOUND','ERR_MODULE_NOT_FOUND','ERR_PACKAGE_PATH_NOT_EXPORTED','ERR_INVALID_PACKAGE_CONFIG','ERR_INVALID_PACKAGE_TARGET','ERR_UNSUPPORTED_DIR_IMPORT']);
const safeError=error=>osCodes.has(error?.code)?error.code:'unknown';
const safeVersion=value=>typeof value==='string'&&/^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$/.test(value)?value:null;
const asarPath=value=>/\.asar(?:[\\/]|$)/i.test(value);
const identity=value=>{const resolved=fs.realpathSync(value);return process.platform==='win32'?resolved.toLowerCase():resolved;};
function moduleIdentities(resolver){
  const expected=Object.fromEntries(required.map(name=>[name,identity(resolver.resolve('@deepseek-ai/'+name))]));
  const loopResolver=createRequire(resolver.resolve('@deepseek-ai/dsh-agent-loop'));
  if(!required.slice(1).every(name=>identity(loopResolver.resolve('@deepseek-ai/'+name))===expected[name]))throw new Error('module_identity_mismatch');
  return expected;
}
function absolute(value){
  if(typeof value!=='string'||!value||value.includes('\0'))return null;
  try{if(value.startsWith('file:'))value=fileURLToPath(value);}catch{return null;}
  return path.isAbsolute(value)?path.resolve(value):null;
}
function desktopRuntimeArgument(argv){
  const entry=absolute(argv[1]),declared=absolute(argv[2]);
  if(!entry||!declared||!/[\\/]node_modules[\\/]@deepseek-ai[\\/]dsh-desktop-host[\\/]lib[\\/](?:index|cli)\.js$/i.test(entry))return null;
  const expected=path.resolve(path.dirname(entry),'../../../..');
  const equal=process.platform==='win32'?expected.toLowerCase()===declared.toLowerCase():expected===declared;
  return equal?declared:null;
}

export function discoverHostRuntime({allowUnverified=false,probeRuntime,inspectRuntime,pluginUrl,
  environment=process.env,argv=process.argv,cwd=process.cwd(),execPath=process.execPath,
  resourcesPath=process.resourcesPath,electronVersion=process.versions.electron}={}){
  if(typeof probeRuntime!=='function'||typeof inspectRuntime!=='function')throw new TypeError('runtime_discovery_inspectors_required');
  const electron=safeVersion(electronVersion),seen=new Set(),attempts=[];
  let count=0,partial=null,authority=null;
  const note=(source,candidate,stage,code)=>{
    if(attempts.length<MAX_ATTEMPTS)attempts.push({source,layout:candidate?(asarPath(candidate)?'asar':'directory'):'unknown',stage,code});
  };
  const decorate=(result,source,candidate)=>{
    result.compatibility={...result.compatibility,discovery:{source,
      layout:candidate?(asarPath(candidate)?'asar':'directory'):'unknown',candidate_count:count,
      electron_version:electron,asar_enabled:electron?process.noAsar!==true:null,attempts:[...attempts]}};
    return result;
  };
  function remember(result,source,root){
    // Prefer an identified but incomplete DSH to a merely inaccessible path.
    const identified=Object.values(result.compatibility?.modules||{}).some(module=>module.sha256!==null);
    const previous=partial&&Object.values(partial.result.compatibility?.modules||{}).some(module=>module.sha256!==null);
    if(!partial||identified&&!previous)partial={result,source,root};
  }
  function candidate(value,source,expected=null){
    const root=absolute(value);if(!root)return null;
    const key=process.platform==='win32'?root.toLowerCase():root;
    if(seen.has(key))return null;
    if(count>=MAX_CANDIDATES){note(source,root,'candidate','candidate_limit');return null;}
    seen.add(key);count++;
    const result=probeRuntime(root),reason=result.compatibility?.reason;
    const identified=Object.values(result.compatibility?.modules||{}).some(module=>module.sha256!==null);
    note(source,root,'candidate',reason||'unknown');
    if(partialReasons.has(reason)){
      if(identified||['asar_runtime_unavailable','runtime_access_denied','required_modules_unreadable'].includes(reason))remember(result,source,root);
      return null;
    }
    {
      const inferred=expected!==null;
      let matches=false;
      try{
        expected=expected||authority||moduleIdentities(createRequire(path.join(root,'package.json')));
        matches=required.every(name=>identity(path.join(root,'node_modules','@deepseek-ai',name,'lib/index.js'))===expected[name]);
      }catch{}
      if(!matches){
        note(source,root,'identity','module_identity_mismatch');
        result.supported=false;result.injectionMode='diagnostic_only';
        result.compatibility={...result.compatibility,supported:false,mode:'diagnostic_only',reason:'host_module_identity_mismatch'};
        // A directory inferred from one resolved package is only a guess. It
        // must not hide the real anchor's complete, same-identity lookup root.
        if(inferred){remember(result,source,root);return null;}
        if(!allowUnverified)throw new Error('host_module_identity_mismatch');
        return decorate(result,source,root);
      }
      note(source,root,'identity','same_module_set');
    }
    // A complete unverified build is authoritative: do not search for a known
    // older build to bypass its fingerprint. Explicit paths use the same rule.
    if(!allowUnverified&&!result.supported)inspectRuntime(root);
    return decorate(result,source,root);
  }
  function moduleAnchor(value,source){
    const anchor=absolute(value);if(!anchor)return null;
    let resolver,loop;
    try{resolver=createRequire(anchor);loop=resolver.resolve('@deepseek-ai/dsh-agent-loop');}
    catch(error){note(source,anchor,'resolve',safeError(error));return null;}
    note(source,loop,'resolve','resolved');
    let expected;
    try{
      expected=moduleIdentities(resolver);
      if(authority&&!required.every(name=>expected[name]===authority[name]))throw new Error('module_identity_mismatch');
      // Once an entry resolves a coherent host module set, later plugin/cwd
      // anchors cannot replace it with another installed DSH, even if its
      // scattered dependencies cannot be represented by one complete root.
      authority=authority||expected;
    }catch(error){
      note(source,loop,'identity',osCodes.has(error?.code)?error.code:'module_identity_mismatch');
      // Do not combine helper classes or private LLM brands from distinct
      // installations. Keep safe read-only diagnostics for this resolved loop.
      const root=path.resolve(path.dirname(loop),'../../../..'),result=probeRuntime(root);
      result.supported=false;result.injectionMode='diagnostic_only';
      result.compatibility={...result.compatibility,supported:false,mode:'diagnostic_only',reason:'host_module_identity_mismatch'};
      if(!allowUnverified)throw new Error('host_module_identity_mismatch');
      return decorate(result,source,root);
    }
    let found=candidate(path.resolve(path.dirname(loop),'../../../..'),source,expected);if(found)return found;
    let roots;
    try{roots=resolver.resolve.paths('@deepseek-ai/dsh-agent-loop');}
    catch(error){note(source,anchor,'resolve',safeError(error));return null;}
    if(!Array.isArray(roots))return null;
    const searched=new Set();
    // Node's own declared lookup scope, never a recursive directory search.
    for(const modules of roots.slice(0,16)){
      if(searched.size>=MAX_SEARCH_ROOTS||count>=MAX_CANDIDATES)break;
      const directory=absolute(modules);
      if(!directory||path.basename(directory).toLowerCase()!=='node_modules')continue;
      const root=path.dirname(directory),key=process.platform==='win32'?root.toLowerCase():root;
      if(searched.has(key))continue;searched.add(key);
      // All four exact modules must belong to this anchor and to the loop's
      // dependency scope. A shared loop file alone does not establish identity.
      let matches=false;
      try{matches=required.every(name=>identity(path.join(directory,'@deepseek-ai',name,'lib/index.js'))===expected[name]);}catch{}
      if(!matches){note(source+'_search',root,'identity','module_identity_mismatch');continue;}
      found=candidate(root,source+'_search',expected);if(found)return found;
    }
    return null;
  }
  const configured=environment?.XIAOJIU_DSH_RUNTIME;
  if(configured){
    const root=path.resolve(configured),found=candidate(root,'environment');if(found)return found;
    const result=partial?.result||probeRuntime(root);
    if(!allowUnverified&&!result.supported)inspectRuntime(root);
    return decorate(result,'environment',root);
  }
  let found=candidate(desktopRuntimeArgument(argv),'desktop_host_argument');if(found)return found;
  found=moduleAnchor(argv[1],'host_module_anchor');if(found)return found;
  const entry=absolute(argv[1]);
  if(entry){
    const match=entry.match(/^(.*?\.asar)(?:[\\/]|$)/i);
    if(match)for(const root of [path.join(match[1],'dsh'),match[1]]){
      found=candidate(root,'asar_host_entry');if(found)return found;
    }
  }
  const resources=absolute(resourcesPath);
  if(electron&&resources)for(const root of [path.join(resources,'app.asar','dsh'),path.join(resources,'app.asar')]){
    found=candidate(root,'electron_resources');if(found)return found;
  }
  const executable=absolute(execPath);
  if(electron&&executable)for(const base of [path.join(path.dirname(executable),'resources'),path.resolve(path.dirname(executable),'../Resources')]){
    for(const root of [path.join(base,'app.asar','dsh'),path.join(base,'app.asar')]){
      found=candidate(root,'electron_executable');if(found)return found;
    }
  }
  found=moduleAnchor(pluginUrl,'plugin_module_anchor');if(found)return found;
  found=moduleAnchor(path.join(cwd,'package.json'),'working_directory_anchor');if(found)return found;
  if(allowUnverified){
    if(partial)return decorate(partial.result,partial.source,partial.root);
    const result=probeRuntime(path.resolve(cwd));result.supported=false;result.injectionMode='diagnostic_only';
    result.compatibility={...result.compatibility,supported:false,mode:'diagnostic_only',reason:'dsh_runtime_not_resolved'};
    return decorate(result,'unresolved',null);
  }
  throw new Error('dsh_runtime_not_resolved_use_the_connection_launcher');
}
