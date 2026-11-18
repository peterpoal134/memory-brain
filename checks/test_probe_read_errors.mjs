/** Fault-injected read-only host probe diagnostics. Every fs stub is restored. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {probeRuntime} from '../adapters/dsh/host-compat.mjs';

const names={loop:'dsh-agent-loop',session:'dsh-session',llm:'dsh-llm',tools:'dsh-tools',connection:'dsh-client-connection',
  compaction:'dsh-compaction',basicCompaction:'dsh-compaction-basic',toolResultPruner:'dsh-compaction-tool-result-pruner',tokenMeter:'dsh-token-meter',cordis:'cordis'};
const nativeSource='class NativeFixture { withRequestMessages(){} step(decision){ return this.withRequestMessages(decision.requestMessages); } }\n';
const privateText='PRIVATE_PATH_TOKEN_AND_RAW_TEXT_MUST_NOT_LEAK';
const normalRoot=path.join(os.tmpdir(),'memory-brain-read-probe-synthetic');
const asarRoot=path.join(os.tmpdir(),'memory-brain-read-probe-synthetic','resources','app.asar','dsh');
function failure(code){return Object.assign(new Error('C:/private/'+privateText+' '+privateText),{code,path:privateText,stack:privateText});}
function probe({root=normalRoot,realpathError=null,statError=null,directory=true,readErrors={},malformedLoop=false}={}){
  const original={readFileSync:fs.readFileSync,realpathSync:fs.realpathSync,statSync:fs.statSync},reads=[];
  const moduleAt=file=>Object.entries(names).find(([,name])=>file===path.join(root,'node_modules','@deepseek-ai',name,'lib','index.js'))?.[0];
  const packageAt=file=>Object.entries(names).find(([,name])=>file===path.join(root,'node_modules','@deepseek-ai',name,'package.json'))?.[0];
  fs.realpathSync=function(file,...args){if(String(file)===root){if(realpathError)throw failure(realpathError);return root;}return Reflect.apply(original.realpathSync,this,[file,...args]);};
  fs.statSync=function(file,...args){if(String(file)===root){if(statError)throw failure(statError);return {isDirectory:()=>directory};}return Reflect.apply(original.statSync,this,[file,...args]);};
  fs.readFileSync=function(file,...args){
    const filename=String(file),key=moduleAt(filename),manifest=packageAt(filename);
    if(key){reads.push(filename);if(readErrors[key])throw failure(readErrors[key]);
      return key==='loop'?(malformedLoop?Buffer.from([0xff]):Buffer.from(nativeSource)):Buffer.from('export const synthetic=true;\n');}
    if(manifest){reads.push(filename);return JSON.stringify({version:'0.2.2-rc.3',private_note:privateText});}
    return Reflect.apply(original.readFileSync,this,[file,...args]);
  };
  try{
    const result=probeRuntime(root),serialized=JSON.stringify(result.compatibility);
    assert.equal(serialized.includes(privateText),false);assert.equal(serialized.includes(root),false);assert.equal(serialized.includes('file:///'),false);
    assert.ok(result.compatibility.read_errors.length<=24);
    for(const item of result.compatibility.read_errors)assert.deepEqual(Object.keys(item),['module','stage','code']);
    assert.equal(reads.some(file=>/private[\\/]config|brain\.db|session\.v4|credentials/i.test(file)),false);
    return result;
  }finally{Object.assign(fs,original);}
}
const tests=[];const test=(name,run)=>tests.push({name,run});
test('complete readable fixture has no read errors and preserves native compatibility',()=>{
  const result=probe();assert.equal(result.supported,true);assert.deepEqual(result.compatibility.read_errors,[]);
});
for(const code of ['EACCES','EPERM']){
  test(code+' at runtime realpath is a permission refusal with no path or exception text',()=>{
    const result=probe({realpathError:code});assert.equal(result.supported,false);assert.equal(result.compatibility.reason,'runtime_access_denied');
    assert.deepEqual(result.compatibility.read_errors,[{module:'runtime',stage:'realpath',code}]);
  });
  test(code+' at runtime stat retains the precise failed stage',()=>{
    const result=probe({statError:code});assert.equal(result.compatibility.reason,'runtime_access_denied');
    assert.deepEqual(result.compatibility.read_errors,[{module:'runtime',stage:'stat',code}]);
  });
  test(code+' in a required module is reported as unreadable rather than missing',()=>{
    const result=probe({readErrors:{session:code}});assert.equal(result.supported,false);
    assert.equal(result.compatibility.reason,'required_modules_unreadable');
    assert.deepEqual(result.compatibility.read_errors,[{module:'session',stage:'read',code}]);
  });
}
test('a nondirectory runtime records ENOTDIR without inventing a permission failure',()=>{
  const result=probe({directory:false});assert.equal(result.compatibility.reason,'runtime_unavailable');
  assert.deepEqual(result.compatibility.read_errors,[{module:'runtime',stage:'stat',code:'ENOTDIR'}]);
});
test('an ASAR root that is not readable is diagnosed without asserting a permissions cause',()=>{
  const result=probe({root:asarRoot,realpathError:'ENOTDIR'});assert.equal(result.compatibility.reason,'asar_runtime_unavailable');
  assert.deepEqual(result.compatibility.read_errors,[{module:'runtime',stage:'realpath',code:'ENOTDIR'}]);
});
test('an ASAR permission denial remains an explicit access error',()=>{
  const result=probe({root:asarRoot,realpathError:'EACCES'});assert.equal(result.compatibility.reason,'runtime_access_denied');
});
test('unknown exception codes cannot copy token-shaped strings into diagnostics',()=>{
  const result=probe({realpathError:privateText});assert.equal(result.compatibility.reason,'runtime_unavailable');
  assert.deepEqual(result.compatibility.read_errors,[{module:'runtime',stage:'realpath',code:'unknown'}]);
});
test('a missing loop records its required-module read failure',()=>{
  const result=probe({readErrors:{loop:'ENOENT'}});assert.equal(result.compatibility.reason,'required_modules_missing');
  assert.deepEqual(result.compatibility.read_errors,[{module:'loop',stage:'read',code:'ENOENT'}]);
});
test('multiple required read failures remain separate and preserve permission evidence',()=>{
  const result=probe({readErrors:{loop:'ENOENT',session:'EACCES',llm:'EPERM',tools:'ENOTDIR'}});
  assert.equal(result.compatibility.reason,'required_modules_unreadable');
  assert.deepEqual(result.compatibility.read_errors,[{module:'loop',stage:'read',code:'ENOENT'},
    {module:'session',stage:'read',code:'EACCES'},{module:'llm',stage:'read',code:'EPERM'},{module:'tools',stage:'read',code:'ENOTDIR'}]);
});
test('I/O and resource read failures are unreadable rather than falsely missing modules',()=>{
  for(const code of ['EIO','EMFILE','ENFILE','ENOMEM','ENOSYS','ENOTSUP',privateText]){
    const result=probe({readErrors:{tools:code}});assert.equal(result.supported,false);
    assert.equal(result.compatibility.reason,'required_modules_unreadable');
    assert.equal(result.compatibility.read_errors[0].code,code===privateText?'unknown':code);
  }
});
test('optional module failures do not invent missing required dependencies',()=>{
  const result=probe({readErrors:{connection:'EACCES',compaction:'ENOENT',cordis:'EPERM'}});
  assert.equal(result.supported,true);assert.deepEqual(result.compatibility.read_errors,[]);
  assert.equal(result.compatibility.modules.connection.sha256,null);
});
test('invalid UTF-8 still fails its original compatibility gate',()=>{
  const result=probe({malformedLoop:true});assert.equal(result.supported,false);assert.equal(result.compatibility.reason,'loop_source_invalid_utf8');
});
test('all replaced filesystem functions are restored after fault injection',()=>{
  const original={readFileSync:fs.readFileSync,realpathSync:fs.realpathSync,statSync:fs.statSync};
  probe({readErrors:{loop:'EIO'}});
  for(const [name,fn] of Object.entries(original))assert.equal(fs[name],fn);
});

let failed=0;
for(const {name,run} of tests){try{run();console.log('PASS '+name);}catch(error){failed++;console.error('FAIL '+name+'\n'+error.stack);}}
console.log(JSON.stringify({passed:tests.length-failed,failed,total:tests.length,network_calls:0,model_calls:0,private_files_read:0,filesystem_stubs_restored:true}));
if(failed)process.exitCode=1;
