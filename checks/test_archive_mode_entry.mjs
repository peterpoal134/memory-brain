/** Public setup opt-in and authorization regression, with synthetic localhost data. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createServer} from 'node:http';
import {setTimeout as delay} from 'node:timers/promises';
import {apply, getAdapter, getOnboarding} from '../adapters/dsh/memory-brain.mjs';
import {SETUP_PREFIX} from '../adapters/dsh/onboarding.mjs';

const root = fs.mkdtempSync(path.join(os.tmpdir(), 'memory-archive-entry-'));
const runtimePath = path.join(root, 'unverified-host');
const brainConfigFile = path.join(root, 'private', 'config.json');
const calls = [], accepted = [];
const token = 'synthetic-archive-mode-entry-token';
for (const name of ['dsh-agent-loop', 'dsh-session', 'dsh-llm', 'dsh-tools']) {
  const directory = path.join(runtimePath, 'node_modules', '@deepseek-ai', name, 'lib');
  fs.mkdirSync(directory, {recursive: true});
  fs.writeFileSync(path.join(directory, 'index.js'), 'throw new Error("private host module must not be loaded");');
}
const server = createServer(async(req, res) => {
  calls.push(req.url);
  assert.equal(req.headers.authorization, 'Bearer ' + token);
  let data;
  if (req.url === '/v1/status') data = {version: '0.2.9', build_id: 'synthetic'};
  else if (req.url === '/v1/ingest') {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    accepted.push(JSON.parse(Buffer.concat(chunks).toString('utf8')));
    data = {accepted: true};
  } else assert.fail('unexpected request route: ' + req.url);
  res.writeHead(200, {'Content-Type': 'application/json'});
  res.end(JSON.stringify({ok: true, data}));
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));

const cleanup = [];
function context(hasSessions=true) {
  const hooks = new Map();
  const session = {id: 'synthetic-session', log: []};
  const dispositions = [];
  cleanup.push(async()=>{ for (const dispose of dispositions.reverse()) await dispose(); });
  const ctx = {
    get(name) {
      if (hasSessions && name === 'sessions') {
        return {list: () => [session], get: id => id === session.id ? session : undefined};
      }
    },
    // The real service object is stable across capability and identity checks.
    on(name, fn) {
      hooks.set(name, fn);
      return () => hooks.delete(name);
    },
    effect(register) { dispositions.push(register()); },
  };
  const service = ctx.get('sessions');
  ctx.get = name => name === 'sessions' ? service : undefined;
  return {ctx, hooks, session};
}
function request(setup, action, csrf) {
  return setup.handle(new Request('http://isolated.invalid' + SETUP_PREFIX + action, {
    method: 'POST', headers: {'content-type': 'application/json', 'x-memory-brain-setup': csrf ?? ''}, body: '{}',
  }));
}
async function until(fn) {
  for (let index=0; index<120; index++) {
    const result = await fn();
    if (result) return result;
    await delay(25);
  }
  throw new Error('archive_mode_timeout');
}
function append(fixture, type, data, time=Date.now()) {
  const event = {seq: fixture.session.log.length, type, data, time};
  fixture.session.log.push(event);
  fixture.hooks.get('session/event')?.(fixture.session, event);
}
const msg = (id, text, source={kind: 'user'}) => ({id, text, content: text, source, role: 'user'});

try {
  const diagnostic = context();
  await apply(diagnostic.ctx, {enabled: true, runtimePath, brainConfigFile});
  const diagnosticSetup = getOnboarding(diagnostic.ctx);
  const rejected = await diagnosticSetup.status();
  assert.equal(rejected.compatibility.reason, 'unknown_loop_fingerprint');
  assert.equal(rejected.phase, 'compatibility_required');
  assert.equal((await request(diagnosticSetup, 'start', rejected.csrfToken)).status, 409);
  assert.equal(getAdapter(diagnostic.ctx), undefined);
  assert.equal(calls.length, 0);
  console.log('PASS unknown fingerprint defaults to read-only diagnosis');

  fs.mkdirSync(path.dirname(brainConfigFile), {recursive: true});
  fs.writeFileSync(brainConfigFile, JSON.stringify({host: '127.0.0.1', port: server.address().port,
    api_token: token, data_dir: '../fixture-data'}));
  const noSessions = context(false);
  await apply(noSessions.ctx, {enabled: true, archiveOnly: true, runtimePath, brainConfigFile});
  const noSessionSetup = getOnboarding(noSessions.ctx);
  const noSessionStatus = await noSessionSetup.status();
  await request(noSessionSetup, 'start', noSessionStatus.csrfToken);
  await until(async()=> (await noSessionSetup.status()).error === 'archive_only_session_contract_required');
  assert.equal(getAdapter(noSessions.ctx), undefined);
  console.log('PASS missing public Sessions API cannot activate archive observer');

  const archive = context();
  append(archive, 'user/message', msg('historical', 'earlier text'), Date.now()-1000);
  await apply(archive.ctx, {enabled: true, archiveOnly: true, runtimePath, brainConfigFile, intervalMs: 50});
  const setup = getOnboarding(archive.ctx);
  const waiting = await setup.status();
  assert.equal(waiting.archive_only, true);
  assert.equal(waiting.adapter.enabled, false);
  assert.equal(archive.hooks.size, 0);
  assert.equal((await request(setup, 'start')).status, 403);
  assert.equal((await request(setup, 'start', waiting.csrfToken)).status, 202);
  const active = await until(async()=>{
    const state = await setup.status();
    if (state.error) throw new Error(state.error);
    return state.phase === 'archive_only' && state;
  });
  assert.equal(active.compatibility.supported, false);
  assert.equal(active.context, null);
  assert.equal(archive.hooks.has('agent/pre-step'), false);
  assert.deepEqual(getAdapter(archive.ctx).status().request_injection, {mode: 'archive_only', active: false});
  assert.equal((await request(setup, 'context', waiting.csrfToken)).status, 400);
  assert.equal(fs.existsSync(path.join(root, 'fixture-data', 'dsh-archive-queue', 'default')), true);
  assert.equal(fs.existsSync(path.join(root, 'fixture-data', 'dsh-queue')), false);
  console.log('PASS explicit authorized start enables only archive and uses separate disk queue');

  append(archive, 'tool/result', {text: 'ignore tool material'});
  append(archive, 'user/message', msg('injected', 'ignore injected context', {kind: 'plugin', form: 'snapshot'}));
  append(archive, 'user/message', msg('current', 'visible human original'));
  await until(()=> accepted.length === 1);
  assert.equal(accepted[0].message_id, 'current');
  assert.equal(accepted[0].text, 'visible human original');
  assert.equal(archive.session.log.length, 4);
  assert(calls.every(requestPath => ['/v1/status', '/v1/ingest'].includes(requestPath)));
  console.log('PASS respects activation time and captures original without injecting or rewriting history');

  const missing = context();
  await apply(missing.ctx, {enabled: true, archiveOnly: true, runtimePath: path.join(root, 'missing'), brainConfigFile});
  assert.equal((await getOnboarding(missing.ctx).status()).phase, 'compatibility_required');
  console.log('PASS module discovery failure remains diagnostic even when archive was requested');
  console.log(JSON.stringify({passed: 5, failed: 0, model_calls: 0, production_mutations: 0}));
} finally {
  try { for (const finish of cleanup.reverse()) await finish(); }
  finally { await new Promise(resolve => server.close(resolve)); }
}
