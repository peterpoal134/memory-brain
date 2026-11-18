/** Browser diagnostic helpers only: no UI, kernel, network, or private config. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import {fileURLToPath} from 'node:url';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const source=fs.readFileSync(path.join(root,'client','client.js'),'utf8');
const start=source.indexOf('    function diagnosticOnly('),end=source.indexOf('    function SetupCard(',start);
assert(start>=0&&end>start,'client diagnostic helper boundaries changed');
const context=vm.createContext({fetch(){throw Error('diagnostics_must_not_call_network');},window:{},document:{}});
const helpers=vm.runInContext(source.slice(start,end)+'\n({diagnosticOnly,compatibilityMessage,safeDiagnostic});',context,{timeout:1000});
const clean=value=>JSON.parse(JSON.stringify(helpers.safeDiagnostic(value)));
const digest='a'.repeat(64),privateText='PRIVATE_VALUE_MUST_NOT_BE_COPIED';
const basic=()=>({supported:false,mode:'diagnostic_only',reason:'dsh_runtime_not_resolved',plugin_version:'0.2.7',
  node_version:'24.21.0',platform:'win32',arch:'x64',modules:{loop:{version:'0.2.2-rc.3',sha256:digest,canonical_sha256:digest}},
  discovery:{source:'host_module_anchor',layout:'asar',candidate_count:3,electron_version:'44.7.0',asar_enabled:true,attempts:[]}});
const tests=[];
const test=(name,run)=>tests.push({name,run});
test('public module versions, hashes and discovery facts remain copyable',()=>{
  const result=clean(basic());
  assert.equal(result.plugin_version,'0.2.7');assert.equal(result.node_version,'24.21.0');assert.equal(result.reason,'dsh_runtime_not_resolved');
  assert.deepEqual(result.modules.loop,{version:'0.2.2-rc.3',sha256:digest,canonical_sha256:digest});
  assert.deepEqual(result.discovery,{source:'host_module_anchor',layout:'asar',candidate_count:3,electron_version:'44.7.0',asar_enabled:true,attempts:[]});
});
test('whole-status credentials, private paths and transcripts are never serialized',()=>{
  const input=basic();Object.assign(input,{csrfToken:privateText,token:privateText,api_token:privateText,
    config:{embed_key:privateText,llm_key:privateText},home:'C:/private/'+privateText,raw:[{text:privateText}],runtimePath:'C:/'+privateText});
  input.modules.loop.path='C:/'+privateText;input.modules.loop.token=privateText;
  input.modules.unknown={version:privateText,sha256:privateText};
  input.discovery.path='C:/'+privateText;input.discovery.error={message:privateText,stack:privateText};
  const result=clean(input),serialized=JSON.stringify(result);
  assert.equal(serialized.includes(privateText),false);assert.equal(serialized.includes('C:/'),false);
  for(const field of ['csrfToken','token','api_token','config','home','raw','runtimePath'])assert.equal(Object.hasOwn(result,field),false);
  assert.deepEqual(Object.keys(result.modules),['loop']);
});
test('read errors preserve only declared module, stage and standard error code',()=>{
  const input=basic();input.read_errors=[{module:'runtime',stage:'realpath',code:'ENOENT',path:privateText,message:privateText,stack:privateText},
    {module:'llm',stage:'read',code:'EACCES',token:privateText},{module:'session',stage:'stat',code:'EPERM'}];
  const result=clean(input);
  assert.deepEqual(result.read_errors,[{module:'runtime',stage:'realpath',code:'ENOENT'},{module:'llm',stage:'read',code:'EACCES'},
    {module:'session',stage:'stat',code:'EPERM'}]);
  assert.equal(JSON.stringify(result).includes(privateText),false);
});
test('read errors reject unknown modules and stages and replace arbitrary codes',()=>{
  const input=basic();input.read_errors=[{module:privateText,stage:'read',code:'EIO'},
    {module:'loop',stage:privateText,code:'EIO'},null,'string',{module:'tools',stage:'read',code:privateText}];
  assert.deepEqual(clean(input).read_errors,[{module:'tools',stage:'read',code:'unknown'}]);
});
test('read-error inspection and output are bounded to the first twenty-four records',()=>{
  const input=basic();input.read_errors=Array.from({length:80},()=>({module:'loop',stage:'read',code:'ENOENT'}));
  assert.equal(clean(input).read_errors.length,24);
  input.read_errors={0:{module:'loop',stage:'read',code:privateText},length:1};assert.equal(Object.hasOwn(clean(input),'read_errors'),false);
});
test('discovery attempts retain all declared sources and reconstruct exact public fields',()=>{
  const input=basic(),sources=['environment','desktop_host_argument','host_module_anchor','asar_host_entry','electron_resources',
    'electron_executable','plugin_module_anchor','working_directory_anchor','host_module_anchor_search','plugin_module_anchor_search',
    'working_directory_anchor_search','unresolved'];
  input.discovery.attempts=sources.map(source=>({source,layout:'asar',stage:'candidate',code:'resolved',path:privateText,message:privateText}));
  const attempts=clean(input).discovery.attempts;
  assert.equal(attempts.length,sources.length);assert.deepEqual(attempts.map(item=>item.source),sources);
  for(const attempt of attempts)assert.deepEqual(Object.keys(attempt),['source','layout','stage','code']);
  assert.equal(JSON.stringify(attempts).includes(privateText),false);
});
test('discovery attempts reject forged sources and stages, sanitize layout and arbitrary errors',()=>{
  const input=basic();input.discovery.attempts=[{source:privateText,layout:'asar',stage:'resolve',code:'ENOENT'},
    {source:'environment',layout:'asar',stage:privateText,code:'ENOENT'},null,42,
    {source:'host_module_anchor_search',layout:privateText,stage:'identity',code:privateText}];
  assert.deepEqual(clean(input).discovery.attempts,[{source:'host_module_anchor_search',layout:'unknown',stage:'identity',code:'unknown'}]);
});
test('discovery attempt inspection and output are bounded to thirty-two records',()=>{
  const input=basic();input.discovery.attempts=Array.from({length:80},()=>({source:'host_module_anchor',layout:'directory',stage:'resolve',code:'resolved'}));
  assert.equal(clean(input).discovery.attempts.length,32);
  input.discovery.attempts={0:{token:privateText},length:1};assert.deepEqual(clean(input).discovery.attempts,[]);
});
test('declared discovery codes, compatibility reasons and OS errors survive validation',()=>{
  const codes=['resolved','not_directory','modules_not_found','same_module_set','module_identity_mismatch','module_resolution_failed','candidate_limit','unknown',
    'runtime_unavailable','asar_runtime_unavailable','runtime_access_denied','required_modules_missing','required_modules_unreadable','loop_source_invalid_utf8',
    'node_22_or_later_required','native_request_messages_markers','verified_stock_source','verified_stock_source_formatting','unknown_loop_fingerprint',
    'host_module_identity_mismatch','dsh_runtime_not_resolved','ENOENT','ENOTDIR','EACCES','EPERM','EIO','MODULE_NOT_FOUND',
    'ERR_MODULE_NOT_FOUND','ERR_PACKAGE_PATH_NOT_EXPORTED'];
  for(const code of codes){const input=basic();input.discovery.attempts=[{source:'host_module_anchor',layout:'asar',stage:'resolve',code}];
    assert.equal(clean(input).discovery.attempts[0].code,code);}
});
test('asar_enabled accepts only booleans or null and never echoes arbitrary values',()=>{
  for(const value of [true,false,null,undefined,'false',privateText,{},1]){
    const input=basic();input.discovery.asar_enabled=value;
    assert.equal(clean(input).discovery.asar_enabled,typeof value==='boolean'?value:null);
  }
});
test('reason, source, platform and architecture do not accept token-shaped strings',()=>{
  const input=basic();input.reason=input.platform=input.arch=input.discovery.source=privateText;
  const result=clean(input);assert.equal(result.reason,'host_build_unverified');assert.equal(result.platform,null);assert.equal(result.arch,null);
  assert.equal(result.discovery.source,null);assert.equal(JSON.stringify(result).includes(privateText),false);
});
test('malformed versions, digests and candidate counts are omitted or null',()=>{
  const input=basic();input.plugin_version='1.2.3-'+ 'a'.repeat(100);input.node_version='C:/private/key';
  input.modules.loop.version=privateText;input.modules.loop.sha256='x'.repeat(64);input.modules.loop.canonical_sha256='short';
  input.loop_sha256='invalid';input.discovery.electron_version=privateText;input.discovery.candidate_count=1000000;
  const result=clean(input);assert.equal(result.plugin_version,null);assert.equal(result.node_version,null);
  assert.deepEqual(result.modules.loop,{version:null,sha256:null});assert.equal(Object.hasOwn(result,'loop_sha256'),false);
  assert.equal(result.discovery.electron_version,null);assert.equal(result.discovery.candidate_count,null);
});
test('non-object inputs and an accidental whole-status wrapper cannot expose status fields',()=>{
  for(const input of [null,undefined,42,'private',[],{compatibility:basic(),csrfToken:privateText,home:privateText}]){
    const result=clean(input);assert.equal(result.reason,'host_build_unverified');assert.deepEqual(result.modules,{});
    assert.equal(JSON.stringify(result).includes(privateText),false);
  }
});
test('diagnostic output remains read-only even if supplied status claims supported or active',()=>{
  const input=basic();input.supported=true;input.mode='active';input.enabled=true;const result=clean(input);
  assert.equal(result.supported,false);assert.equal(result.mode,'diagnostic_only');assert.equal(Object.hasOwn(result,'enabled'),false);
});
test('packaged access, permission, missing modules and identity mismatch have distinct explanations',()=>{
  const reasons=['asar_runtime_unavailable','runtime_access_denied','required_modules_missing','required_modules_unreadable','host_module_identity_mismatch'];
  const messages=reasons.map(reason=>helpers.compatibilityMessage({reason}));assert.equal(new Set(messages).size,reasons.length);
  assert.match(messages[0],/不一定是权限问题/);assert.match(messages[1],/系统拒绝读取/);assert.match(messages[2],/不完整/);
  assert.match(messages[3],/读取失败/);assert.match(messages[4],/不同来源/);
  for(const message of messages)assert.match(message,/未启用/);
});
test('untrusted reasons cannot appear in user-facing explanations',()=>{
  const message=helpers.compatibilityMessage({reason:privateText,token:privateText});assert.equal(message.includes(privateText),false);
  assert.match(message,/构建尚未完成核验/);
});
test('diagnostic UI recommends a quiet reload and copying evidence while preserving existing memory',()=>{
  assert(source.includes('请先完成当前任务，再重新加载插件，并复制下方安全诊断交给维护者；已有记忆保持不变。'));
  const branch=source.slice(source.indexOf('      if (readOnlyDiagnostic) {'),source.indexOf("      return h('div', { style: styles.root },",source.indexOf('      if (readOnlyDiagnostic) {')+100));
  assert.equal(/perform\(|openPanel\(/.test(branch),false);
});

let failed=0;
for(const {name,run} of tests){try{run();console.log('PASS '+name);}catch(error){failed++;console.error('FAIL '+name+'\n'+error.stack);}}
console.log(JSON.stringify({passed:tests.length-failed,failed,total:tests.length,network_calls:0,model_calls:0,private_files_read:0}));
if(failed)process.exitCode=1;
