/** Synthetic public-plugin preflight checks; never opens a real data directory. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { resolveConfiguration, inspectRuntime, apply } from '../adapters/dsh/memory-brain.mjs';
const root = fs.mkdtempSync(path.join(os.tmpdir(), 'memory-brain-public-entry-'));
let passed = 0;
const check = (name, fn) => { fn(); passed++; console.log('PASS ' + name); };
const runtime = path.join(root, 'runtime');
for (const name of ['dsh-agent-loop','dsh-session','dsh-tools','dsh-llm']) {
  const file = path.join(runtime,'node_modules','@deepseek-ai',name,'lib','index.js');
  fs.mkdirSync(path.dirname(file),{recursive:true});
  fs.writeFileSync(file, name === 'dsh-agent-loop' ? 'class Fixture { withRequestMessages(request) {} step(decision) { return this.withRequestMessages({}, decision.requestMessages); } }' : 'export {};');
}
const file = path.join(root,'private','config.json');fs.mkdirSync(path.dirname(file));
const local = {host:'127.0.0.1',port:18880,api_token:'synthetic-public-token-0123456789',data_dir:'../data'};
fs.writeFileSync(file,JSON.stringify(local));
const config = {enabled:true,runtimePath:runtime,brainConfigFile:file,queueNamespace:'profile-a'};
try {
  check('disabled plugin needs no local configuration',()=>assert.deepEqual(resolveConfiguration({enabled:false,brainConfigFile:'missing'}),{enabled:false}));
  await apply({}, {enabled:false,brainConfigFile:'missing'});passed++;
  const resolved=resolveConfiguration(config);
  check('local token is read from local configuration only',()=>assert.equal(resolved.token,local.api_token));
  check('data and queue are portable and profile-scoped',()=>assert.equal(resolved.queueDir,path.join(root,'data','dsh-queue','profile-a')));
  check('session and tool modules belong to the selected host',()=>assert(resolved.sessionModule.includes('dsh-session')&&resolved.toolsModule.includes('dsh-tools')));
  check('native snapshot hosts select their own LLM module without installing a bridge',()=>assert(resolved.injectionMode==='native_snapshot'&&resolved.llmModule.includes('dsh-llm')&&resolved.requestBridge===undefined));
  check('the verified entry owns its compatibility bridge',()=>assert.throws(()=>resolveConfiguration({...config,requestBridge:{}}),/request_bridge_is_owned/));
  check('inline credentials are refused',()=>assert.throws(()=>resolveConfiguration({...config,token:'inline'}),/inline_token/));
  check('queue traversal is refused',()=>assert.throws(()=>resolveConfiguration({...config,queueNamespace:'../../escape'}),/namespace/));
  const before=fs.readFileSync(path.join(runtime,'node_modules/@deepseek-ai/dsh-agent-loop/lib/index.js'));
  inspectRuntime(runtime);
  check('compatibility inspection never patches the host',()=>assert.deepEqual(fs.readFileSync(path.join(runtime,'node_modules/@deepseek-ai/dsh-agent-loop/lib/index.js')),before));
  fs.writeFileSync(path.join(runtime,'node_modules/@deepseek-ai/dsh-agent-loop/lib/index.js'),'/* synthetic unsupported host */');
  check('unsupported host is explicitly refused',()=>assert.throws(()=>inspectRuntime(runtime),/request_messages_hook_required/));
  fs.writeFileSync(file,JSON.stringify({...local,host:'0.0.0.0'}));
  check('public-network kernel config is refused',()=>assert.throws(()=>resolveConfiguration(config),/address_invalid/));
  console.log(JSON.stringify({passed,failed:0,production_mutations:0}));
} finally { fs.rmSync(root,{recursive:true,force:true}); }
