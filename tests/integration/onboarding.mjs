/** Owner onboarding through the actual DSH Connection Fetch registry.
 * DSH_RUNTIME=<read-only installed DSH> node tests/integration/onboarding.mjs
 * No Python installer, network listener, paid model or existing private data.
 */
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { createOnboarding, mountOnboarding } from '../../adapters/dsh/onboarding.mjs';

const runtime = process.env.DSH_RUNTIME;
if (!runtime || !path.isAbsolute(runtime)) throw new Error('Set DSH_RUNTIME to a read-only installed DSH runtime.');
const hostFiles = ['cordis', 'dsh-client-connection'].map(name =>
  path.join(runtime, 'node_modules', '@deepseek-ai', name, 'lib', 'index.js'));
const fingerprint = () => hostFiles.map(file => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex'));
const hostBefore = fingerprint();
const [{ Context }, { HostConnectionService, Peer }] = await Promise.all(hostFiles.map(file => import(pathToFileURL(file).href)));
const root = fs.mkdtempSync(path.join(os.tmpdir(), 'memory-brain-onboarding-'));
const prefix = '/api/memory-brain/setup/';
const origin = 'http://127.0.0.1:19421';
const secret = 'SYNTHETIC_NOT_REAL_MODEL_SECRET';
const managerToken = 'SYNTHETIC_NOT_REAL_MANAGEMENT_SECRET';
let passed = 0, failed = 0, serial = 0;
const check = async (name, test) => {
  try { await test(); passed++; console.log('PASS ' + name); }
  catch (error) { failed++; console.error('FAIL ' + name + ': ' + error.stack); }
};
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const gate = () => { let release; const promise = new Promise(resolve => { release = resolve; }); return { promise, release }; };

async function withFixture(test) {
  const home = path.join(root, 'owner-' + (++serial)); fs.mkdirSync(home);
  const calls = { initialize: [], models: [], start: 0, onStart: 0, activate: 0, panel: 0, dispose: 0, stop: 0, handle: 0 };
  const control = { initializeGate: null, startGate: null, initializeFailures: 0, startFailures: 0 };
  const state = { installed: false, running: false, adapter: false, embedding: false, derivation: false, embedKey: false, llmKey: false };
  const manager = {
    async status() {
      return { state: state.installed ? 'ready' : 'not_initialized', expected_version: 'synthetic-1',
        installed_version: state.installed ? 'synthetic-1' : null, python: { available: true, version: '3.11.synthetic' },
        models: { embedding_configured: state.embedding, derivation_configured: state.derivation,
          embed_key_saved: state.embedKey, llm_key_saved: state.llmKey },
        running: state.running, restart_required: false, error: null,
        // Projection must never forward arbitrary manager/private fields.
        api_token: managerToken, llm_key: secret, private_configuration: { secret } };
    },
    async initialize(options) {
      calls.initialize.push(options);
      if (control.initializeGate) await control.initializeGate.promise;
      if (control.initializeFailures-- > 0) throw new Error('synthetic_initialization_failed');
      state.installed = true;
      fs.writeFileSync(path.join(home, 'initialized.json'), JSON.stringify({ initialized: true }));
      return this.status();
    },
    async saveModels(body) {
      calls.models.push(body);
      state.embedding = Boolean(body.embed_url || state.embedding);
      state.derivation = Boolean(body.llm_url || state.derivation);
      state.embedKey = Boolean(body.embed_key || state.embedKey);
      state.llmKey = Boolean(body.llm_key || state.llmKey);
      return this.status();
    },
    async start() {
      calls.start++;
      if (control.startGate) await control.startGate.promise;
      if (control.startFailures-- > 0) throw new Error('synthetic_start_failed');
      if (!state.installed) throw new Error('synthetic_not_initialized');
      state.running = true;
      return this.status();
    },
    async panelUrl() { calls.panel++; return 'http://127.0.0.1:18880/brain#ticket=SYNTHETIC_ONE_USE_TICKET'; },
    async dispose() { calls.dispose++; },
    async stop() { calls.stop++; state.running = false; },
  };
  // Production onStart owns one activation promise. A repeated explicit Start
  // action may call it again, but must not start another kernel or adapter.
  let activationPromise = null;
  const onStart = () => {
    calls.onStart++;
    if (state.adapter) return Promise.resolve();
    if (activationPromise) return activationPromise;
    activationPromise = (async () => { await manager.start(); state.adapter = true; calls.activate++; })()
      .finally(() => { activationPromise = null; });
    return activationPromise;
  };
  const controller = createOnboarding({ manager, onStart, home, compatibility: 'scoped_agent_bridge', legacy: false });
  const monitored = { ...controller, handle(request) { calls.handle++; return controller.handle(request); } };
  const ctx = new Context();
  let apiPeer, fiber, csrfToken;
  const header = (request, name) => request.headers instanceof Headers ? request.headers.get(name) : request.headers[name];
  const browserAuth = {
    isAuthenticated: request => header(request, 'cookie') === 'synthetic_dsh_owner=authenticated',
    authenticateApi: request => header(request, 'authorization') === 'Bearer synthetic_nonowner_api' ? apiPeer.id : undefined,
  };
  const connection = new HostConnectionService(ctx, [], browserAuth);
  apiPeer = new Peer(ctx, { principal: { kind: 'api', name: 'synthetic-nonowner' } });
  connection.peers.set(apiPeer.id, { peer: apiPeer, ownsSocket: false });
  const shared = connection.createSharedFetchHandler('/api');
  fiber = ctx.plugin({ name: 'synthetic-memory-brain-onboarding', apply(child) {
    const unmount = mountOnboarding(child, monitored);
    child.effect(() => unmount);
    child.effect(() => () => controller.dispose());
  } });
  await fiber.await();
  assert.equal(fiber.state, 2, 'actual Cordis child plugin did not become active');

  async function dispatch(action, options = {}) {
    const method = options.method || (action === 'status' ? 'GET' : 'POST');
    const headers = new Headers({ Host: '127.0.0.1:19421', Origin: origin });
    if (options.identity !== 'anonymous' && options.identity !== 'api') headers.set('Cookie', 'synthetic_dsh_owner=authenticated');
    if (options.identity === 'api') headers.set('Authorization', 'Bearer synthetic_nonowner_api');
    if (method !== 'GET') headers.set('Content-Type', 'application/json');
    if (options.csrf !== false && csrfToken) headers.set('X-Memory-Brain-Setup', typeof options.csrf === 'string' ? options.csrf : csrfToken);
    for (const [key, value] of Object.entries(options.headers || {})) {
      if (value === null) headers.delete(key); else headers.set(key, value);
    }
    const request = new Request(origin + prefix + action, { method, headers,
      ...(method === 'GET' ? {} : { body: options.rawBody ?? JSON.stringify(Object.hasOwn(options, 'body') ? options.body : {}) }) });
    // This is the native web carrier's admission sequence, not a direct call
    // that silently supplies an owner Peer to every request.
    const admission = connection.admit(request);
    if ('rejection' in admission) return new Response(admission.rejection === 401 ? 'unauthorized' : 'forbidden', { status: admission.rejection });
    return shared.fetch(request, admission.peer);
  }
  async function status() {
    const response = await dispatch('status'); assert.equal(response.status, 200);
    const result = await response.json(); assert.equal(result.ok, true);
    csrfToken = result.data.csrfToken; return result.data;
  }
  async function settled() {
    const end = Date.now() + 3000;
    while (Date.now() < end) { const value = await status(); if (!value.busy) return value; await delay(5); }
    throw new Error('synthetic_action_did_not_settle');
  }
  try { await test({ home, calls, control, state, manager, controller, ctx, connection, fiber, dispatch, status, settled }); }
  finally {
    control.initializeGate?.release(); control.startGate?.release();
    await fiber.dispose(); await fiber.await(); await controller.dispose();
    await ctx.fiber.dispose(); await ctx.fiber.await();
  }
}

try {
  await check('native owner status is read-only and projects no private manager fields', () => withFixture(async f => {
    const before = fs.readdirSync(f.home);
    const first = await f.status(); const second = await f.status();
    assert.equal(first.phase, 'not_initialized'); assert.equal(first.runtime.python.available, true);
    assert.equal(first.compatibility, 'scoped_agent_bridge'); assert.equal(first.adapter.enabled, false);
    assert.equal(typeof first.csrfToken, 'string'); assert(first.csrfToken.length >= 24);
    assert.equal(first.csrfToken, second.csrfToken);
    assert(!JSON.stringify(first).includes(secret)); assert(!JSON.stringify(first).includes(managerToken));
    assert.equal(f.calls.initialize.length, 0); assert.equal(f.calls.start, 0); assert.equal(f.calls.models.length, 0);
    assert.deepEqual(fs.readdirSync(f.home), before);
    assert.equal((await f.dispatch('unknown')).status, 404);
  }));
  await check('native carrier denies anonymous, foreign authority and cross-site callers before setup', () => withFixture(async f => {
    await f.status(); const handled = f.calls.handle;
    assert.equal((await f.dispatch('status', { identity: 'anonymous' })).status, 401);
    assert.equal((await f.dispatch('status', { headers: { Host: 'untrusted.example' } })).status, 403);
    assert.equal((await f.dispatch('initialize', { body: { allowDownloads: true }, headers: { Origin: 'https://other.example' } })).status, 403);
    assert.equal((await f.dispatch('initialize', { body: { allowDownloads: true }, headers: { 'Sec-Fetch-Site': 'cross-site' } })).status, 403);
    assert.equal(f.calls.handle, handled); assert.equal(f.calls.initialize.length, 0);
  }));
  await check('authenticated nonowner API Peer cannot read status or initialize through exact Fetch routes', () => withFixture(async f => {
    await f.status(); const handled = f.calls.handle;
    assert.equal((await f.dispatch('status', { identity: 'api' })).status, 403);
    assert.equal((await f.dispatch('initialize', { identity: 'api', body: { allowDownloads: true } })).status, 403);
    assert.equal(f.calls.handle, handled); assert.equal(f.calls.initialize.length, 0);
  }));
  await check('missing wrong and equal-character-length non-ASCII CSRF tokens fail without side effects', () => withFixture(async f => {
    const data = await f.status();
    for (const csrf of [false, 'wrong-synthetic-token', 'é'.repeat(data.csrfToken.length)]) {
      const response = await f.dispatch('initialize', { csrf, body: { allowDownloads: true } });
      assert.equal(response.status, 403);
    }
    assert.equal(f.calls.initialize.length, 0); assert.equal(f.calls.start, 0);
  }));
  await check('explicit consent object and a bounded JSON whitelist are required', () => withFixture(async f => {
    await f.status();
    for (const body of [{}, { allowDownloads: false }, { allowDownloads: true, payload: 'arbitrary' }, [], null]) {
      assert.equal((await f.dispatch('initialize', { body })).status, 400);
    }
    assert.equal((await f.dispatch('initialize', { rawBody: '{' })).status, 400);
    assert.equal((await f.dispatch('initialize', { body: { allowDownloads: true }, headers: { 'Content-Type': 'text/plain' } })).status, 400);
    assert.equal((await f.dispatch('initialize', { rawBody: JSON.stringify({ padding: 'x'.repeat(17000) }) })).status, 413);
    assert.equal((await f.dispatch('initialize', { method: 'GET' })).status, 404);
    assert.equal(f.calls.initialize.length, 0);
  }));
  await check('only an explicit initialize action starts one background operation and concurrent actions are refused', () => withFixture(async f => {
    await f.status(); f.control.initializeGate = gate();
    const accepted = await f.dispatch('initialize', { body: { allowDownloads: true } });
    assert.equal(accepted.status, 202); assert.equal((await accepted.json()).data.accepted, true);
    await delay(0); assert.equal(f.calls.initialize.length, 1);
    assert.deepEqual(f.calls.initialize[0], { allowDownloads: true });
    assert.equal((await f.status()).busy, true);
    assert.equal((await f.dispatch('initialize', { body: { allowDownloads: true } })).status, 409);
    assert.equal((await f.dispatch('start')).status, 409);
    assert.equal((await f.dispatch('models', { body: { llm_model: 'synthetic' } })).status, 409);
    f.control.initializeGate.release(); const done = await f.settled();
    assert.equal(done.error, null); assert.equal(done.runtime.state, 'ready');
    assert.equal(f.calls.initialize.length, 1); assert.equal(f.calls.onStart, 0); assert.equal(f.calls.start, 0);
    assert.deepEqual(JSON.parse(fs.readFileSync(path.join(f.home, 'initialized.json'), 'utf8')), { initialized: true });
  }));
  await check('model values go to the manager only after confirmation and secret values are never echoed', () => withFixture(async f => {
    await f.status();
    assert.equal((await f.dispatch('models', { body: { llm_url: 'https://synthetic.invalid/v1', llm_model: 'synthetic', llm_key: secret } })).status, 202);
    const done = await f.settled();
    assert.equal(f.calls.models.length, 1); assert.equal(f.calls.models[0].llm_key, secret);
    assert.equal(done.runtime.models.llm_key_saved, true); assert.equal(done.runtime.models.derivation_configured, true);
    assert(!JSON.stringify(done).includes(secret)); assert(!JSON.stringify(done).includes(managerToken));
    assert.equal((await f.dispatch('models', { body: { command: 'arbitrary executable' } })).status, 400);
    assert.equal((await f.dispatch('models', { body: { llm_model: 5 } })).status, 400);
    assert.equal(f.calls.models.length, 1); assert.equal(f.calls.start, 0);
  }));
  await check('explicit Start activates once while busy and repeated accepted Start remains idempotent', () => withFixture(async f => {
    await f.status(); f.state.installed = true; f.control.startGate = gate();
    assert.equal((await f.dispatch('start')).status, 202);
    await delay(0); assert.equal(f.calls.onStart, 1); assert.equal(f.calls.start, 1);
    assert.equal((await f.dispatch('start')).status, 409);
    f.control.startGate.release(); let done = await f.settled();
    assert.equal(done.adapter.enabled, true); assert.equal(done.runtime.running, true); assert.equal(f.calls.activate, 1);
    assert.equal((await f.dispatch('start')).status, 202); done = await f.settled();
    assert.equal(done.adapter.enabled, true); assert.equal(f.calls.activate, 1); assert.equal(f.calls.start, 1);
  }));
  await check('failed initialization can be retried without leaving the setup permanently busy', () => withFixture(async f => {
    await f.status(); f.control.initializeFailures = 1;
    assert.equal((await f.dispatch('initialize', { body: { allowDownloads: true } })).status, 202);
    let done = await f.settled(); assert.equal(done.error, 'synthetic_initialization_failed'); assert.equal(done.adapter.enabled, false);
    assert.equal(fs.readdirSync(f.home).length, 0);
    assert.equal((await f.dispatch('initialize', { body: { allowDownloads: true } })).status, 202);
    done = await f.settled(); assert.equal(done.error, null); assert.equal(done.runtime.state, 'ready');
    assert.equal(f.calls.initialize.length, 2); assert.equal(f.calls.start, 0);
  }));
  await check('failed adapter activation can be retried and a panel action returns only a local ticket URL', () => withFixture(async f => {
    await f.status(); f.state.installed = true; f.control.startFailures = 1;
    assert.equal((await f.dispatch('start')).status, 202);
    let done = await f.settled(); assert.equal(done.error, 'synthetic_start_failed'); assert.equal(done.adapter.enabled, false);
    assert.equal((await f.dispatch('start')).status, 202);
    done = await f.settled(); assert.equal(done.error, null); assert.equal(done.adapter.enabled, true);
    assert.equal(f.calls.activate, 1); assert.equal(f.calls.start, 2);
    const response = await f.dispatch('panel'); assert.equal(response.status, 200);
    const result = await response.json(), target = new URL(result.data.url);
    assert.equal(target.origin, 'http://127.0.0.1:18880'); assert.equal(target.pathname, '/brain');
    assert(target.hash.startsWith('#ticket=')); assert.equal(target.search, '');
    assert(!JSON.stringify(result).includes(managerToken)); assert(!JSON.stringify(result).includes(secret));
  }));
  await check('native plugin disposal removes all setup routes without stopping the already running kernel', () => withFixture(async f => {
    await f.status(); f.state.installed = true;
    assert.equal((await f.dispatch('start')).status, 202); await f.settled();
    assert.equal(f.connection.fetchRoutes.size, 5);
    await f.fiber.dispose(); await f.fiber.await();
    assert.equal(f.connection.fetchRoutes.size, 0);
    assert.equal((await f.dispatch('status')).status, 404);
    assert.equal((await f.dispatch('initialize', { body: { allowDownloads: true } })).status, 404);
    assert.equal(f.state.running, true); assert.equal(f.calls.stop, 0); assert.equal(f.calls.dispose, 1);
    assert.equal((await f.controller.handle(new Request(origin + prefix + 'status'))).status, 410);
  }));
  await check('matching DSH module bytes are unchanged', async () => assert.deepEqual(fingerprint(), hostBefore));
  console.log(JSON.stringify({ passed, failed, real_host_connection_service: true, network_installer_calls: 0,
    real_model_calls: 0, production_mutations: 0, actual_tcp_server: false }));
  if (failed) process.exitCode = 1;
} finally {
  const resolved = path.resolve(root), temp = path.resolve(os.tmpdir());
  if (!resolved.startsWith(temp + path.sep) || !path.basename(resolved).startsWith('memory-brain-onboarding-'))
    throw new Error('refusing_cleanup_outside_the_synthetic_temp_directory');
  fs.rmSync(resolved, { recursive: true, force: true });
}
