/** Fresh managed-home entry through real Cordis + DSH Connection.
 * DSH_RUNTIME=<read-only installed runtime> node tests/test_onboarding_entry.mjs
 * No initialize POST, installer, model, existing configuration or service.
 */
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import * as entry from '../adapters/dsh/memory-brain.mjs';

const runtime = process.env.DSH_RUNTIME;
if (!runtime || !path.isAbsolute(runtime)) throw new Error('Set DSH_RUNTIME to a read-only installed DSH runtime.');
const packageRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const runtimeFiles = ['cordis', 'dsh-client-connection', 'dsh-agent-loop', 'dsh-session', 'dsh-tools', 'dsh-llm']
  .map(name => path.join(runtime, 'node_modules', '@deepseek-ai', name, 'lib', 'index.js'));
const fingerprint = () => runtimeFiles.map(file => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex'));
const before = fingerprint();
const [{ Context }, { HostConnectionService }] = await Promise.all(runtimeFiles.slice(0, 2).map(file => import(pathToFileURL(file).href)));
const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'memory-brain-entry-'));
const home = path.join(scratch, 'fresh-managed-home');
assert.equal(fs.existsSync(home), false);
const privateRoot = path.join(packageRoot, 'private');
const normalize = value => process.platform === 'win32' ? path.resolve(value).toLowerCase() : path.resolve(value);
const privateNormalized = normalize(privateRoot);
let protectedReads = 0, networkCalls = 0, passed = 0;
const check = (name, value) => { assert(value, name); passed++; console.log('PASS ' + name); };
const originalRead = fs.readFileSync, originalExists = fs.existsSync, originalFetch = globalThis.fetch;
const originalInterval = globalThis.setInterval, originalClearInterval = globalThis.clearInterval;
const intervals = new Set();
function checkPrivate(filename) {
  let candidate;
  try { candidate = normalize(filename instanceof URL ? fileURLToPath(filename) : Buffer.isBuffer(filename) ? filename.toString() : filename); }
  catch { return; }
  if (candidate === privateNormalized || candidate.startsWith(privateNormalized + path.sep)) {
    protectedReads++; throw new Error('test_refuses_existing_private_configuration_access');
  }
}
// If a regression selects the repo's legacy deployment, fail before even
// reading its token/configuration or reaching an automatic legacy launcher.
fs.readFileSync = function(filename, ...rest) { checkPrivate(filename); return Reflect.apply(originalRead, this, [filename, ...rest]); };
fs.existsSync = function(filename, ...rest) { checkPrivate(filename); return Reflect.apply(originalExists, this, [filename, ...rest]); };
globalThis.fetch = async () => { networkCalls++; throw new Error('unexpected_network_during_uninitialized_entry'); };
globalThis.setInterval = function(...args) { const handle = Reflect.apply(originalInterval, this, args); intervals.add(handle); return handle; };
globalThis.clearInterval = function(handle) { intervals.delete(handle); return Reflect.apply(originalClearInterval, this, [handle]); };

const ctx = new Context();
const connection = new HostConnectionService(ctx, [], {
  isAuthenticated: request => request.headers.get('cookie') === 'synthetic_owner=authenticated',
  authenticateApi: () => undefined,
});
const shared = connection.createSharedFetchHandler('/api');
let fiber;
const origin = 'http://127.0.0.1:19422';
async function ownerGet() {
  const request = new Request(origin + '/api/memory-brain/setup/status', { headers: {
    Host: '127.0.0.1:19422', Origin: origin, Cookie: 'synthetic_owner=authenticated',
  } });
  const admitted = connection.admit(request);
  assert(!('rejection' in admitted), 'synthetic owner was rejected by native admission');
  return shared.fetch(request, admitted.peer);
}
try {
  // Explicit runtimeHome MUST override the real repo's existing private/config.
  // Do not remove this option or replace it with the public default path.
  fiber = ctx.plugin(entry, { enabled: true, runtimePath: runtime, runtimeHome: home });
  await fiber.await();
  check('public plugin with an explicit fresh managed home reaches active setup state', fiber.state === 2);
  const setup = entry.getOnboarding(fiber.ctx);
  check('waiting onboarding controller is available without a private package configuration', Boolean(setup && typeof setup.status === 'function'));
  check('no adapter was attached before initialization', entry.getAdapter(fiber.ctx) === undefined);
  check('setup was registered through five native Connection routes', connection.fetchRoutes.size === 5);
  for (let index = 0; index < 3; index++) {
    const response = await ownerGet(); assert.equal(response.status, 200);
    const result = await response.json(); assert.equal(result.ok, true);
    const value = result.data;
    assert.equal(value.legacy, false); assert.equal(normalize(value.home), normalize(home));
    assert.equal(value.phase, 'not_initialized'); assert.equal(value.runtime.installed_version, null);
    assert.equal(value.runtime.running, false); assert.equal(value.adapter.enabled, false);
    assert.equal(value.busy, false); assert.equal(value.operation, null);
    assert.equal(typeof value.csrfToken, 'string'); assert(value.csrfToken.length >= 24);
  }
  check('repeated native authenticated GETs remain waiting and do not create a kernel home or queue', !fs.existsSync(home));
  check('explicit managed mode never reads or even probes the existing private deployment', protectedReads === 0);
  check('empty-home loading and status perform no HTTP or model requests', networkCalls === 0);
  check('no background poller was installed while waiting for initialization', intervals.size === 0);
  await fiber.dispose(); await fiber.await();
  check('native setup routes disappear when the public plugin is unloaded', connection.fetchRoutes.size === 0 && (await ownerGet()).status === 404);
  const closed = await setup.status();
  check('disposed onboarding is closed and never attaches an adapter', closed.phase === 'closed' && !closed.adapter.enabled && entry.getAdapter(fiber.ctx) === undefined);
  check('unloading leaves no data, queue, network activity or interval poller', !fs.existsSync(home) && networkCalls === 0 && intervals.size === 0);
  check('actual DSH runtime bytes remain unchanged', JSON.stringify(before) === JSON.stringify(fingerprint()));
  console.log(JSON.stringify({ passed, failed: 0, explicit_managed_home: true, existing_private_reads: protectedReads,
    network_calls: networkCalls, initialize_posts: 0, real_model_calls: 0, production_mutations: 0 }));
} finally {
  try { if (fiber) { await fiber.dispose(); await fiber.await(); } await ctx.fiber.dispose(); await ctx.fiber.await(); }
  finally {
    for (const handle of intervals) originalClearInterval(handle);
    globalThis.setInterval = originalInterval; globalThis.clearInterval = originalClearInterval;
    globalThis.fetch = originalFetch; fs.readFileSync = originalRead; fs.existsSync = originalExists;
    const temp = path.resolve(os.tmpdir()), resolved = path.resolve(scratch);
    if (!resolved.startsWith(temp + path.sep) || !path.basename(resolved).startsWith('memory-brain-entry-'))
      throw new Error('refusing_cleanup_outside_the_synthetic_temp_directory');
    fs.rmSync(resolved, { recursive: true, force: true });
  }
}
