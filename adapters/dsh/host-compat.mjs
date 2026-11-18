/** Read-only, source-fingerprint gated host compatibility diagnostics.
 * Only `compatibility` is safe to copy into a user-visible report. The outer
 * object deliberately retains module paths for trusted in-process activation.
 * No DSH code, private configuration, environment or session is executed/read.
 */
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {matchVerifiedLoop} from './request-bridge.mjs';

const packageRoot=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../..');
const sha256=bytes=>crypto.createHash('sha256').update(bytes).digest('hex');
const safeVersion=value=>typeof value==='string'&&/^[0-9A-Za-z.+_-]{1,80}$/.test(value)?value:null;
const modules=Object.freeze({loop:'dsh-agent-loop',session:'dsh-session',llm:'dsh-llm',tools:'dsh-tools',connection:'dsh-client-connection',compaction:'dsh-compaction',basicCompaction:'dsh-compaction-basic',toolResultPruner:'dsh-compaction-tool-result-pruner',tokenMeter:'dsh-token-meter',cordis:'cordis'});
const required=Object.freeze(['loop','session','llm','tools']);
const accessCodes=new Set(['ENOENT','ENOTDIR','EACCES','EPERM','EISDIR','EINVAL','ELOOP','ENAMETOOLONG','EIO','EMFILE','ENFILE','ENOMEM','ENOTSUP','ENOSYS','EBUSY','EROFS','ETIMEDOUT']);
const errorCode=error=>accessCodes.has(error?.code)?error.code:'unknown';
const denied=error=>error?.code==='EACCES'||error?.code==='EPERM';
const unreadable=error=>error?.code!=='ENOENT'&&error?.code!=='ENOTDIR';
function version(filename){
  try{return safeVersion(JSON.parse(fs.readFileSync(filename,'utf8').replace(/^\uFEFF/,'')).version);}catch{return null;}
}

export function probeRuntime(runtimePath){
  if(typeof runtimePath!=='string'||!path.isAbsolute(runtimePath)||runtimePath.includes('\0')){
    throw Object.assign(new Error('dsh_runtime_absolute_path_required'),{code:'dsh_runtime_absolute_path_required'});
  }
  const compatibility={plugin_version:version(path.join(packageRoot,'package.json')),node_version:process.versions.node,
    platform:process.platform,arch:process.arch,supported:false,mode:'diagnostic_only',reason:'runtime_unavailable',
    modules:Object.fromEntries(Object.keys(modules).map(key=>[key,{version:null,sha256:null,...(key==='loop'?{canonical_sha256:null}:{})}])),
    loop_sha256:null,loop_canonical_sha256:null,read_errors:[]};
  const result={runtime:null,sessionModule:null,toolsModule:null,llmModule:null,compactionModule:null,basicCompactionModule:null,toolResultPrunerModule:null,tokenMeterModule:null,cordisModule:null,injectionMode:'diagnostic_only',supported:false,compatibility};
  const readError=(module,stage,error)=>{if(compatibility.read_errors.length<24)compatibility.read_errors.push({module,stage,code:errorCode(error)});};
  const runtimeError=(stage,error)=>{
    readError('runtime',stage,error);
    compatibility.reason=denied(error)?'runtime_access_denied':/\.asar(?:[\\/]|$)/i.test(runtimePath)?'asar_runtime_unavailable':'runtime_unavailable';
    return result;
  };
  let runtime;
  try{runtime=fs.realpathSync(runtimePath);}catch(error){return runtimeError('realpath',error);}
  try{if(!fs.statSync(runtime).isDirectory())return runtimeError('stat',{code:'ENOTDIR'});}catch(error){return runtimeError('stat',error);}
  result.runtime=runtime;
  const files={};let loopBytes=null;
  for(const [key,name] of Object.entries(modules)){
    const directory=path.join(runtime,'node_modules','@deepseek-ai',name),file=path.join(directory,'lib','index.js');files[key]=file;
    compatibility.modules[key].version=version(path.join(directory,'package.json'));
    try{const bytes=fs.readFileSync(file);compatibility.modules[key].sha256=sha256(bytes);if(key==='loop')loopBytes=bytes;}
    catch(error){if(required.includes(key))readError(key,'read',error);}
  }
  compatibility.loop_sha256=compatibility.modules.loop.sha256;
  let code;
  if(!loopBytes){compatibility.reason=compatibility.read_errors.some(unreadable)?'required_modules_unreadable':'required_modules_missing';return result;}
  try{new TextDecoder('utf-8',{fatal:true}).decode(loopBytes);code=loopBytes.toString('utf8');}
  catch{compatibility.reason='loop_source_invalid_utf8';return result;}
  // The raw and representation-normalized digests remain available to users.
  const canonical=code.replace(/^\uFEFF/,'').replace(/\r\n/g,'\n');
  compatibility.loop_canonical_sha256=sha256(Buffer.from(canonical,'utf8'));
  compatibility.modules.loop.canonical_sha256=compatibility.loop_canonical_sha256;
  if(required.some(key=>compatibility.modules[key].sha256===null)){compatibility.reason=compatibility.read_errors.some(unreadable)?'required_modules_unreadable':'required_modules_missing';return result;}
  // Internal-only module references are never included in compatibility JSON.
  result.sessionModule=pathToFileURL(files.session).href;result.toolsModule=pathToFileURL(files.tools).href;result.llmModule=pathToFileURL(files.llm).href;
  if(compatibility.modules.compaction.sha256!==null)result.compactionModule=pathToFileURL(files.compaction).href;
  for(const key of ['basicCompaction','toolResultPruner','tokenMeter','cordis'])if(compatibility.modules[key].sha256!==null)result[key+'Module']=pathToFileURL(files[key]).href;
  if(Number(process.versions.node.split('.')[0])<22){compatibility.reason='node_22_or_later_required';return result;}
  const verified=matchVerifiedLoop(code,compatibility.loop_sha256,canonical,compatibility.loop_canonical_sha256);
  if(!verified){compatibility.reason='unknown_loop_fingerprint';return result;}
  result.injectionMode=verified.mode;
  result.bridgeSourceSha256=verified.sourceSha256;
  result.bridgeSyntaxEquivalent=verified.syntaxEquivalent===true;
  compatibility.reason=verified.reason;
  result.supported=true;compatibility.supported=true;compatibility.mode=result.injectionMode;return result;
}
