/** Synthetic modules plus optional read-only known stock source. Never bundle
 * the complete DSH source into this test or mutate an installed host.
 * DSH_STOCK_LOOP_FILE=<verified stock index.js> node tests/test_host_compat.mjs
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {probeRuntime} from '../adapters/dsh/host-compat.mjs';
import {STOCK_LOOP_SHA256} from '../adapters/dsh/request-bridge.mjs';

const repository=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..'),root=fs.mkdtempSync(path.join(os.tmpdir(),'memory-brain-compat-'));
const names={loop:'dsh-agent-loop',session:'dsh-session',llm:'dsh-llm',tools:'dsh-tools',connection:'dsh-client-connection'};
const tests=[];const test=(name,fn)=>tests.push({name,fn});
const native='class Native { withRequestMessages() {} step(decision) { return this.withRequestMessages(decision.requestMessages); } }\n';
const hash=bytes=>crypto.createHash('sha256').update(bytes).digest('hex');
function runtime(code=native){
  const directory=fs.mkdtempSync(path.join(root,'runtime-'));
  for(const [key,name] of Object.entries(names)){
    const mod=path.join(directory,'node_modules','@deepseek-ai',name);fs.mkdirSync(path.join(mod,'lib'),{recursive:true});
    fs.writeFileSync(path.join(mod,'package.json'),JSON.stringify({version:'0.2.1-rc.2',private_label:'PRIVATE_METADATA_NEVER_RETURN'}));
    fs.writeFileSync(path.join(mod,'lib','index.js'),key==='loop'?code:'export const synthetic = true;\n');
  }
  fs.mkdirSync(path.join(directory,'private'));fs.writeFileSync(path.join(directory,'private','config.json'),'SYNTHETIC_PRIVATE_TOKEN_SHOULD_NEVER_BE_READ');return directory;
}
const modulePath=(directory,key)=>path.join(directory,'node_modules','@deepseek-ai',names[key],'lib','index.js');
test('native three-marker path is preserved and reports only safe public module fields',()=>{
  const directory=runtime(),result=probeRuntime(directory),report=result.compatibility;
  assert.equal(result.supported,true);assert.equal(result.injectionMode,'native_snapshot');assert.equal(report.mode,'native_snapshot');
  assert.ok(result.sessionModule.startsWith('file:'));assert.equal(report.node_version,process.versions.node);assert.equal(report.platform,process.platform);
  assert.deepEqual(Object.keys(report.modules),['loop','session','llm','tools','connection']);
  for(const item of Object.values(report.modules)){assert.equal(item.version,'0.2.1-rc.2');assert.match(item.sha256,/^[a-f0-9]{64}$/);}
  const serialized=JSON.stringify(report);assert.ok(!serialized.includes(directory));assert.ok(!serialized.includes('PRIVATE_METADATA'));assert.ok(!serialized.includes('SYNTHETIC_PRIVATE_TOKEN'));
});
test('unknown loop becomes diagnostic-only rather than throwing or adopting module version',()=>{
  const result=probeRuntime(runtime('export const versionLabel="0.2.1-rc.2";\n'));
  assert.equal(result.supported,false);assert.equal(result.injectionMode,'diagnostic_only');assert.equal(result.compatibility.reason,'unknown_loop_fingerprint');
  assert.equal(result.compatibility.mode,'diagnostic_only');
});
test('each native marker is required',()=>{
  for(const code of ['class X { withRequestMessages(){} step(d){return d.requestMessages;} }',
    'class X { withRequestMessages(){} step(decision){return decision.requestMessages;} }',
    'class X { withRequestMessages(){} step(d){return this.withRequestMessages(d.other);} }'])assert.equal(probeRuntime(runtime(code)).supported,false);
});
test('missing required module yields safe diagnostic data',()=>{
  const directory=runtime();fs.unlinkSync(modulePath(directory,'session'));const result=probeRuntime(directory);
  assert.equal(result.supported,false);assert.equal(result.compatibility.reason,'required_modules_missing');assert.equal(result.compatibility.modules.session.sha256,null);
  assert.equal(result.sessionModule,null);assert.ok(!JSON.stringify(result.compatibility).includes(directory));
});
test('optional Connection metadata does not add a new a17 compatibility gate',()=>{
  const directory=runtime();fs.unlinkSync(modulePath(directory,'connection'));const result=probeRuntime(directory);
  assert.equal(result.supported,true);assert.equal(result.compatibility.modules.connection.sha256,null);
});
test('nonexistent absolute runtime is reportable but nonabsolute arguments are rejected',()=>{
  const result=probeRuntime(path.join(root,'missing'));
  assert.equal(result.supported,false);assert.equal(result.runtime,null);assert.equal(result.compatibility.reason,'runtime_unavailable');
  for(const value of ['relative',null,42,{}])assert.throws(()=>probeRuntime(value),/dsh_runtime_absolute_path_required/);
});
test('malformed package versions and invalid UTF-8 never leak arbitrary data or enable native markers',()=>{
  const directory=runtime();const manifest=path.join(directory,'node_modules','@deepseek-ai',names.session,'package.json');
  fs.writeFileSync(manifest,JSON.stringify({version:'SYNTHETIC PRIVATE\nTOKEN /absolute/path'}));
  assert.equal(probeRuntime(directory).compatibility.modules.session.version,null);
  fs.writeFileSync(modulePath(directory,'loop'),Buffer.concat([Buffer.from([0xff]),Buffer.from(native)]));
  assert.equal(probeRuntime(directory).compatibility.reason,'loop_source_invalid_utf8');assert.equal(probeRuntime(directory).supported,false);
});
test('probe does not read private config or execute a synthetic host module',()=>{
  const directory=runtime(native+'\nthrow new Error("THIS_MUST_NOT_EXECUTE");\n'),before=fs.readFileSync(modulePath(directory,'loop'));
  const original=fs.readFileSync;const read=[];
  fs.readFileSync=function(file,...args){read.push(String(file));return original.call(this,file,...args);};
  try{assert.equal(probeRuntime(directory).supported,true);}finally{fs.readFileSync=original;}
  assert.ok(!read.some(file=>file.includes(path.sep+'private'+path.sep)));assert.ok(fs.readFileSync(modulePath(directory,'loop')).equals(before));
});
test('CLI emits only compatibility and has explicit no-scan usage',()=>{
  const directory=runtime('export const unknown=true;\n'),script=path.join(repository,'scripts','dsh-compat-report.mjs');
  const result=spawnSync(process.execPath,[script,'--runtime',directory],{encoding:'utf8',windowsHide:true});assert.equal(result.status,0);
  const report=JSON.parse(result.stdout);assert.equal(report.supported,false);assert.equal(report.reason,'unknown_loop_fingerprint');assert.ok(!result.stdout.includes(directory));assert.equal(result.stderr,'');
  const help=spawnSync(process.execPath,[script],{encoding:'utf8',windowsHide:true});assert.equal(help.status,0);assert.equal(JSON.parse(help.stdout).read_only,true);
  const invalid=spawnSync(process.execPath,[script,'--runtime','relative'],{encoding:'utf8',windowsHide:true});assert.equal(invalid.status,2);assert.equal(JSON.parse(invalid.stdout).error,'dsh_runtime_absolute_path_required');
});
if(process.env.DSH_STOCK_LOOP_FILE){
  const stock=fs.readFileSync(process.env.DSH_STOCK_LOOP_FILE);assert.equal(hash(stock),STOCK_LOOP_SHA256,'environment stock file is not the verified source');
  test('actual known stock fingerprint supports the scoped bridge without relying on a version label',()=>{
    const result=probeRuntime(runtime(stock));assert.equal(result.supported,true);assert.equal(result.injectionMode,'scoped_agent_bridge');
    assert.equal(result.compatibility.loop_sha256,STOCK_LOOP_SHA256);assert.equal(result.compatibility.modules.loop.canonical_sha256,STOCK_LOOP_SHA256);
  });
  test('only initial UTF-8 BOM and CRLF formatting differences are accepted for stock',()=>{
    for(const code of ['\uFEFF'+stock.toString('utf8'),stock.toString('utf8').replace(/\n/g,'\r\n'),'\uFEFF'+stock.toString('utf8').replace(/\n/g,'\r\n')]){
      const result=probeRuntime(runtime(code));assert.equal(result.supported,true);assert.equal(result.compatibility.reason,'verified_stock_source_formatting');
      assert.notEqual(result.compatibility.loop_sha256,STOCK_LOOP_SHA256);assert.equal(result.compatibility.loop_canonical_sha256,STOCK_LOOP_SHA256);
    }
  });
  test('other stock modifications including whitespace comments and bare CR are refused',()=>{
    for(const code of [stock.toString('utf8')+' ',stock.toString('utf8')+'\n// changed\n',stock.toString('utf8').replace('\n','\r'),stock.toString('utf8').replace('buildRequest','differentRequest')]){
      const result=probeRuntime(runtime(code));assert.equal(result.supported,false);assert.equal(result.compatibility.reason,'unknown_loop_fingerprint');
    }
  });
}else console.log('SKIP real stock variants: provide DSH_STOCK_LOOP_FILE to an existing verified source.');
let failed=0;
try{for(const {name,fn} of tests){try{await fn();console.log('PASS '+name);}catch(error){failed++;console.error('FAIL '+name+'\n'+error.stack);}}
  console.log(JSON.stringify({passed:tests.length-failed,failed,total:tests.length}));if(failed)process.exitCode=1;
}finally{fs.rmSync(root,{recursive:true,force:true});}
