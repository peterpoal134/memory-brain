/** Read-only compatibility admission checks. The optional real runtime is never
 * imported, patched or started. DSH_COMPAT_RUNTIME must name its runtime root.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {probeRuntime} from '../adapters/dsh/host-compat.mjs';
import {VERIFIED_LOOP_BUILDS} from '../adapters/dsh/request-bridge.mjs';

const repository=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const temporary=fs.mkdtempSync(path.join(os.tmpdir(),'memory-brain-host-compat-'));
const modules=Object.freeze({loop:'dsh-agent-loop',session:'dsh-session',llm:'dsh-llm',tools:'dsh-tools',
  connection:'dsh-client-connection',compaction:'dsh-compaction',basicCompaction:'dsh-compaction-basic',
  toolResultPruner:'dsh-compaction-tool-result-pruner',tokenMeter:'dsh-token-meter',cordis:'cordis'});
const hash=bytes=>crypto.createHash('sha256').update(bytes).digest('hex');
const canonical=bytes=>bytes.toString('utf8').replace(/^\uFEFF/,'').replace(/\r\n/g,'\n');
const checks=[];
const check=(name,run)=>checks.push({name,run});
const moduleFile=(runtime,key)=>path.join(runtime,'node_modules','@deepseek-ai',modules[key],'lib','index.js');
const packageFile=(runtime,key)=>path.join(runtime,'node_modules','@deepseek-ai',modules[key],'package.json');
const unknown='export const versionLabel="0.2.2-rc.3";\n';
function fixture(loop=unknown){
  const runtime=fs.mkdtempSync(path.join(temporary,'runtime-'));
  for(const [key,name] of Object.entries(modules)){
    const directory=path.join(runtime,'node_modules','@deepseek-ai',name);
    fs.mkdirSync(path.join(directory,'lib'),{recursive:true});
    fs.writeFileSync(packageFile(runtime,key),JSON.stringify({version:'0.2.2-rc.3',private_label:'DO_NOT_REPORT_PRIVATE_METADATA'}));
    fs.writeFileSync(moduleFile(runtime,key),key==='loop'?loop:'throw new Error("HOST_MODULE_MUST_NOT_EXECUTE");\n');
  }
  fs.mkdirSync(path.join(runtime,'private'));
  fs.writeFileSync(path.join(runtime,'private','config.json'),'DO_NOT_READ_PRIVATE_TOKEN_OR_TRANSCRIPT');
  return runtime;
}
function safeReport(report,runtime){
  assert.deepEqual(Object.keys(report.modules),Object.keys(modules));
  assert.equal(report.node_version,process.versions.node);
  assert.equal(report.platform,process.platform);
  const serialized=JSON.stringify(report);
  for(const forbidden of [runtime,'DO_NOT_REPORT_PRIVATE_METADATA','DO_NOT_READ_PRIVATE_TOKEN_OR_TRANSCRIPT','file:///'])
    assert.equal(serialized.includes(forbidden),false,'diagnostic leaked '+forbidden);
  for(const item of Object.values(report.modules)){
    assert.ok(item.version===null||/^[0-9A-Za-z.+_-]{1,80}$/.test(item.version));
    assert.ok(item.sha256===null||/^[a-f0-9]{64}$/.test(item.sha256));
  }
}

check('an unverified loop stays diagnostic-only despite the correct version label',()=>{
  const runtime=fixture(),result=probeRuntime(runtime);
  assert.equal(result.supported,false);assert.equal(result.injectionMode,'diagnostic_only');
  assert.equal(result.compatibility.reason,'unknown_loop_fingerprint');
  assert.equal(result.compatibility.modules.loop.version,'0.2.2-rc.3');
  safeReport(result.compatibility,runtime);
});
check('required missing modules do not grant compatibility',()=>{
  for(const key of ['loop','session','llm','tools']){
    const runtime=fixture();fs.unlinkSync(moduleFile(runtime,key));const result=probeRuntime(runtime);
    assert.equal(result.supported,false);assert.equal(result.compatibility.reason,'required_modules_missing');
    assert.equal(result.compatibility.modules[key].sha256,null);safeReport(result.compatibility,runtime);
  }
});
check('invalid UTF-8 is rejected before native-marker inspection',()=>{
  const source=Buffer.concat([Buffer.from([0xff]),Buffer.from('class X { withRequestMessages(){} step(decision){return this.withRequestMessages(decision.requestMessages);} }')]);
  const runtime=fixture(source),result=probeRuntime(runtime);
  assert.equal(result.supported,false);assert.equal(result.compatibility.reason,'loop_source_invalid_utf8');
});
check('unsafe package metadata cannot enter copyable diagnostics',()=>{
  const runtime=fixture();fs.writeFileSync(packageFile(runtime,'session'),JSON.stringify({version:'PRIVATE SECRET\nC:/private/token'}));
  const result=probeRuntime(runtime);assert.equal(result.compatibility.modules.session.version,null);safeReport(result.compatibility,runtime);
});
check('probe performs only declared package and built-file reads, without executing modules',()=>{
  const runtime=fixture(unknown+'throw new Error("LOOP_MUST_NOT_EXECUTE");\n');
  const allowed=new Set(Object.keys(modules).flatMap(key=>[moduleFile(runtime,key),packageFile(runtime,key)]));
  allowed.add(path.join(repository,'package.json'));
  const readFile=fs.readFileSync,reads=[];
  fs.readFileSync=function(file,...args){reads.push(String(file));return readFile.call(this,file,...args);};
  let result;
  try{result=probeRuntime(runtime);}finally{fs.readFileSync=readFile;}
  assert.equal(result.supported,false);assert.ok(reads.length>0);
  for(const file of reads)assert.equal(allowed.has(file),true,'unexpected file read: '+file);
  safeReport(result.compatibility,runtime);
});
check('bad paths are refused and a missing absolute runtime is reportable',()=>{
  for(const value of ['relative',null,42,{},path.join(temporary,'nul')+'\0suffix'])
    assert.throws(()=>probeRuntime(value),/dsh_runtime_absolute_path_required/);
  const result=probeRuntime(path.join(temporary,'missing'));
  assert.equal(result.supported,false);assert.equal(result.runtime,null);assert.equal(result.compatibility.reason,'runtime_unavailable');
});
check('public diagnostic CLI returns safe JSON and never scans without an explicit runtime',()=>{
  const runtime=fixture(),script=path.join(repository,'scripts','dsh-compat-report.mjs');
  const report=spawnSync(process.execPath,[script,'--runtime',runtime],{encoding:'utf8',windowsHide:true});
  assert.equal(report.status,0,report.stderr);assert.equal(report.stderr,'');
  const value=JSON.parse(report.stdout);assert.equal(value.reason,'unknown_loop_fingerprint');safeReport(value,runtime);
  const help=spawnSync(process.execPath,[script],{encoding:'utf8',windowsHide:true});
  assert.equal(help.status,0);assert.equal(JSON.parse(help.stdout).read_only,true);
  const bad=spawnSync(process.execPath,[script,'--runtime','relative'],{encoding:'utf8',windowsHide:true});
  assert.equal(bad.status,2);assert.equal(JSON.parse(bad.stdout).error,'dsh_runtime_absolute_path_required');
});

const realRuntime=process.env.DSH_COMPAT_RUNTIME;
let realBefore=null;
if(realRuntime){
  assert.ok(path.isAbsolute(realRuntime),'DSH_COMPAT_RUNTIME must be an absolute runtime root');
  const bytes=fs.readFileSync(moduleFile(realRuntime,'loop')),source=canonical(bytes),fingerprint=hash(source);
  realBefore=Object.fromEntries(Object.keys(modules).flatMap(key=>[moduleFile(realRuntime,key),packageFile(realRuntime,key)])
    .filter(file=>fs.existsSync(file)).map(file=>[file,hash(fs.readFileSync(file))]));
  check('the actual runtime is admitted only by an explicitly verified complete-source fingerprint',()=>{
    assert.ok(Object.hasOwn(VERIFIED_LOOP_BUILDS,fingerprint),'real loop fingerprint has not been explicitly reviewed');
    const result=probeRuntime(realRuntime);assert.equal(result.supported,true);
    assert.equal(result.injectionMode,'scoped_agent_bridge');assert.equal(result.compatibility.loop_canonical_sha256,fingerprint);
    safeReport(result.compatibility,realRuntime);
  });
  check('the verified real source tolerates only initial BOM and CRLF representation',()=>{
    for(const text of [source,'\uFEFF'+source,source.replace(/\n/g,'\r\n'),'\uFEFF'+source.replace(/\n/g,'\r\n')]){
      const runtime=fixture(text),result=probeRuntime(runtime);assert.equal(result.supported,true);
      assert.equal(result.injectionMode,'scoped_agent_bridge');assert.equal(result.compatibility.loop_canonical_sha256,fingerprint);
      assert.equal(result.compatibility.reason,hash(Buffer.from(text))===fingerprint?'verified_stock_source':'verified_stock_source_formatting');
    }
  });
  check('verified source identity is authoritative; package-version labels do not grant or revoke it',()=>{
    const runtime=fixture(source);fs.writeFileSync(packageFile(runtime,'loop'),JSON.stringify({version:'99.0.0-test-label'}));
    assert.equal(probeRuntime(runtime).supported,true);
    fs.writeFileSync(moduleFile(runtime,'loop'),unknown);assert.equal(probeRuntime(runtime).supported,false);
  });
  check('changed verified source remains closed even when version labels still match',()=>{
    for(const changed of [source+' ',source+'\n// altered\n',source.replace('\n','\r'),source.replace('buildRequest','unreviewedRequest')]){
      assert.notEqual(changed,source);const result=probeRuntime(fixture(changed));
      assert.equal(result.supported,false);assert.equal(result.compatibility.reason,'unknown_loop_fingerprint');
    }
  });
  check('missing optional connection metadata does not block an otherwise verified source',()=>{
    const runtime=fixture(source);fs.unlinkSync(moduleFile(runtime,'connection'));
    const result=probeRuntime(runtime);assert.equal(result.supported,true);assert.equal(result.compatibility.modules.connection.sha256,null);
  });
  check('real installed runtime files remain byte-for-byte unchanged',()=>{
    for(const [file,before] of Object.entries(realBefore))assert.equal(hash(fs.readFileSync(file)),before);
  });
}else console.log('SKIP real runtime admission: set DSH_COMPAT_RUNTIME to an existing reviewed runtime.');

let failed=0;
try{
  for(const {name,run} of checks){try{await run();console.log('PASS '+name);}catch(error){failed++;console.error('FAIL '+name+'\n'+error.stack);}}
  console.log(JSON.stringify({passed:checks.length-failed,failed,total:checks.length,real_runtime_checked:Boolean(realRuntime),host_mutations:0,model_calls:0}));
  if(failed)process.exitCode=1;
}finally{
  const resolved=path.resolve(temporary),tempRoot=path.resolve(os.tmpdir())+path.sep;
  assert.ok(resolved.startsWith(tempRoot)&&path.basename(resolved).startsWith('memory-brain-host-compat-'));
  fs.rmSync(resolved,{recursive:true,force:true});
}
