/** Unknown DSH builds remain authenticated, read-only diagnostics.
 * DSH_RUNTIME=<real read-only DSH> node tests/test_unknown_host.mjs
 * The fake rc2 version below is deliberately NOT a compatibility grant.
 */
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { createRequire, syncBuiltinESMExports } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';

const installed = process.env.DSH_RUNTIME;
if (!installed || !path.isAbsolute(installed)) throw new Error('Set DSH_RUNTIME to a read-only installed DSH runtime.');
const modfile = name => path.join(installed, 'node_modules', '@deepseek-ai', name, 'lib', 'index.js');
const actualFiles = ['cordis', 'dsh-client-connection', 'dsh-session'].map(modfile);
const sha = value => crypto.createHash('sha256').update(value).digest('hex');
const hashes = () => actualFiles.map(file => sha(fs.readFileSync(file)));
const actualBefore = hashes();
const [{ Context, Service }, { HostConnectionService }, { SessionStore }] = await Promise.all(actualFiles.map(file => import(pathToFileURL(file).href)));
const prepareBefore = SessionStore.prototype.prepare;
const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'memory-brain-unknown-host-'));
const runtime = path.join(scratch, 'synthetic-claimed-rc2-runtime');
const home = path.join(scratch, 'managed-home-must-not-exist');
const otherHome = path.join(scratch, 'legacy-transport-home-must-not-exist');
const secret = 'SYNTHETIC_PRIVATE_TOKEN_MUST_NOT_APPEAR';
const bodyMarker = 'SYNTHETIC_CHAT_BODY_MUST_NOT_APPEAR';
const privateConfig = path.join(scratch, 'synthetic-private', 'config.json');
const privateBytes = Buffer.from(JSON.stringify({ api_token: secret, private_body: bodyMarker }));
fs.mkdirSync(path.dirname(privateConfig), { recursive: true }); fs.writeFileSync(privateConfig, privateBytes);
fs.mkdirSync(runtime);
fs.writeFileSync(path.join(runtime, 'package.json'), JSON.stringify({ version: '0.2.1-rc.2' }));
const fakeFiles = [];
for (const name of ['dsh-agent-loop', 'dsh-session', 'dsh-tools', 'dsh-llm']) {
  const directory = path.join(runtime, 'node_modules', '@deepseek-ai', name);
  fs.mkdirSync(path.join(directory, 'lib'), { recursive: true });
  fs.writeFileSync(path.join(directory, 'package.json'), JSON.stringify({ name: '@deepseek-ai/' + name, version: '0.2.1-rc.2' }));
  const file = path.join(directory, 'lib', 'index.js'); fs.writeFileSync(file, 'export {};\n'); fakeFiles.push(file);
}
const fakeBefore = fakeFiles.map(file => sha(fs.readFileSync(file)));
const packageRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const repoPrivate = path.join(packageRoot, 'private');
const normalized = value => process.platform === 'win32' ? path.resolve(value).toLowerCase() : path.resolve(value);
const protectedRoots = [normalized(repoPrivate), normalized(path.dirname(privateConfig))];
const originalRead = fs.readFileSync, originalExists = fs.existsSync, originalFetch = globalThis.fetch;
const originalInterval = globalThis.setInterval, originalClear = globalThis.clearInterval, originalWarn = console.warn;
const childProcess = createRequire(import.meta.url)('node:child_process');
const originalSpawn = childProcess.spawn, originalExec = childProcess.execFile;
let privateReads = 0, networkCalls = 0, processCalls = 0, passed = 0;
const intervals = new Set(), warnings = [], contexts = [];
const existingServers = new Set(process._getActiveHandles().filter(handle => handle instanceof net.Server));
const check = (name, value) => { assert(value, name); passed++; console.log('PASS ' + name); };
function rejectPrivate(filename) {
  let candidate;
  try { candidate = normalized(filename instanceof URL ? fileURLToPath(filename) : Buffer.isBuffer(filename) ? filename.toString() : filename); }
  catch { return; }
  if (protectedRoots.some(root => candidate === root || candidate.startsWith(root + path.sep))) {
    privateReads++; throw new Error('unknown_host_must_not_read_private_configuration');
  }
}
fs.readFileSync = function(filename, ...rest) { rejectPrivate(filename); return Reflect.apply(originalRead, this, [filename, ...rest]); };
fs.existsSync = function(filename, ...rest) { rejectPrivate(filename); return Reflect.apply(originalExists, this, [filename, ...rest]); };
globalThis.fetch = async () => { networkCalls++; throw new Error('unknown_host_must_not_issue_network_requests'); };
childProcess.spawn = function() { processCalls++; throw new Error('unknown_host_must_not_spawn'); };
childProcess.execFile = function() { processCalls++; throw new Error('unknown_host_must_not_execute'); };
syncBuiltinESMExports();
globalThis.setInterval = function(...args) { const handle = Reflect.apply(originalInterval, this, args); intervals.add(handle); return handle; };
globalThis.clearInterval = function(handle) { intervals.delete(handle); return Reflect.apply(originalClear, this, [handle]); };
console.warn = (...args) => warnings.push(args.map(String).join(' '));
const origin = 'http://127.0.0.1:19423';
let fiber, otherFiber;
try {
  // Import after process guards so even an accidental legacy launcher cannot
  // reach an installed service. Both configs below are synthetic or unread.
  const entry = await import('../adapters/dsh/memory-brain.mjs');
  const ctx = new Context(); contexts.push(ctx);
  const connection = new HostConnectionService(ctx, [], {
    isAuthenticated: request => request.headers.get('cookie') === 'synthetic_owner=authenticated',
    authenticateApi: () => undefined,
  });
  const shared = connection.createSharedFetchHandler('/api');
  fiber = ctx.plugin(entry, { enabled: true, runtimePath: runtime, runtimeHome: home, brainConfigFile: privateConfig });
  await fiber.await();
  check('an unknown build does not make the public plugin fail to load', fiber.state === 2);
  const setup = entry.getOnboarding(fiber.ctx);
  check('unknown-build setup is available without opening any existing configuration', Boolean(setup) && privateReads === 0);
  async function request(action = 'status', { body = {}, csrf, anonymous = false, crossOrigin = false } = {}) {
    const method = action === 'status' ? 'GET' : 'POST';
    const headers = new Headers({ Host: '127.0.0.1:19423', Origin: crossOrigin ? 'https://untrusted.example' : origin });
    if (!anonymous) headers.set('Cookie', 'synthetic_owner=authenticated');
    if (csrf) headers.set('X-Memory-Brain-Setup', csrf);
    if (method === 'POST') headers.set('Content-Type', 'application/json');
    const incoming = new Request(origin + '/api/memory-brain/setup/' + action,
      { method, headers, ...(method === 'POST' ? { body: JSON.stringify(body) } : {}) });
    const admission = connection.admit(incoming);
    if ('rejection' in admission) return new Response('rejected', { status: admission.rejection });
    return shared.fetch(incoming, admission.peer);
  }
  const first = await request(); assert.equal(first.status, 200);
  const data = (await first.json()).data;
  check('native owner GET clearly reports unverified integration and a disabled adapter', data.phase === 'compatibility_required' &&
    data.adapter.enabled === false && data.compatibility.supported === false && data.compatibility.mode === 'diagnostic_only');
  check('a claimed rc2 package version is diagnostic text rather than permission to activate',
    data.compatibility.modules.loop.version === '0.2.1-rc.2' && data.compatibility.reason === 'unknown_loop_fingerprint' && !entry.getAdapter(fiber.ctx));
  const report = JSON.stringify(data.compatibility);
  check('copyable compatibility report contains hashes but no paths bodies credentials or configuration',
    /^[a-f0-9]{64}$/.test(data.compatibility.loop_sha256) && !report.includes(scratch) && !report.includes(packageRoot) &&
    !/[A-Za-z]:[\\/]|file:\/\/|(?:token|password|private_body|source_path)/i.test(report) && !report.includes(secret) && !report.includes(bodyMarker));
  for (const action of ['initialize', 'models', 'start', 'panel']) {
    const body = action === 'initialize' ? { allowDownloads: true } : action === 'models' ? { llm_key: secret } : {};
    const response = await request(action, { body, csrf: data.csrfToken });
    assert.equal(response.status, 409);
    const value = await response.json(); assert.equal(value.ok, false); assert.equal(value.error, 'host_build_unverified');
    assert(!JSON.stringify(value).includes(secret));
  }
  check('even valid owner confirmation cannot initialize configure start or open an unverified host',
    !fs.existsSync(home) && networkCalls === 0 && processCalls === 0 && privateReads === 0);
  check('diagnostic GET remains behind native browser authentication and Origin fencing',
    (await request('status', { anonymous: true })).status === 401 && (await request('status', { crossOrigin: true })).status === 403);
  for (let index = 0; index < 3; index++) {
    const response = await request(); assert.equal(response.status, 200);
    const status = (await response.json()).data; assert.equal(status.busy, false); assert.equal(status.adapter.enabled, false);
  }
  check('repeated diagnostics create no raw capture queue home process HTTP call or interval poller',
    !fs.existsSync(home) && !entry.getAdapter(fiber.ctx) && intervals.size === 0 && processCalls === 0 && networkCalls === 0 &&
    SessionStore.prototype.prepare === prepareBefore);
  await fiber.dispose(); await fiber.await();
  check('disposing diagnostic mode removes native routes without attaching an adapter', connection.fetchRoutes.size === 0 &&
    (await request()).status === 404 && (await setup.status()).phase === 'closed' && !entry.getAdapter(fiber.ctx));

  class ConnectionWithoutFetch extends Service { constructor(scope) { super(scope, 'connection'); } }
  const oldContext = new Context(); contexts.push(oldContext); new ConnectionWithoutFetch(oldContext);
  otherFiber = oldContext.plugin(entry, { enabled: true, runtimePath: runtime, runtimeHome: otherHome, brainConfigFile: privateConfig });
  await otherFiber.await();
  const oldSetup = entry.getOnboarding(otherFiber.ctx), oldStatus = await oldSetup.status();
  check('a host without the Fetch registration API stays loaded with an actionable safe warning',
    otherFiber.state === 2 && oldStatus.phase === 'compatibility_required' && oldStatus.error === 'dsh_setup_connection_routes_required' &&
    warnings.some(value => value.includes('dsh_setup_connection_routes_required')) && !entry.getAdapter(otherFiber.ctx));
  const newServers = process._getActiveHandles().filter(handle => handle instanceof net.Server && !existingServers.has(handle));
  check('missing native transport never creates an anonymous fallback listener or mutable state',
    newServers.length === 0 && !fs.existsSync(otherHome) && intervals.size === 0 && networkCalls === 0 && processCalls === 0 && privateReads === 0);
  const warningText = warnings.join('\n');
  check('warnings and host bytes remain safe and unchanged', !warningText.includes(secret) && !warningText.includes(bodyMarker) &&
    !warningText.includes(scratch) && !warningText.includes(packageRoot) && JSON.stringify(hashes()) === JSON.stringify(actualBefore) &&
    JSON.stringify(fakeFiles.map(file => sha(fs.readFileSync(file)))) === JSON.stringify(fakeBefore));
  console.log(JSON.stringify({ passed, failed: 0, existing_private_reads: privateReads, process_calls: processCalls, network_calls: networkCalls,
    real_model_calls: 0, production_mutations: 0, mobile_application_verified: false }));
} finally {
  try {
    for (const current of [fiber, otherFiber]) if (current) { await current.dispose(); await current.await(); }
    for (const ctx of contexts) { await ctx.fiber.dispose(); await ctx.fiber.await(); }
  } finally {
    for (const handle of intervals) originalClear(handle);
    fs.readFileSync = originalRead; fs.existsSync = originalExists; globalThis.fetch = originalFetch;
    childProcess.spawn = originalSpawn; childProcess.execFile = originalExec; syncBuiltinESMExports();
    globalThis.setInterval = originalInterval; globalThis.clearInterval = originalClear; console.warn = originalWarn;
    assert.equal(sha(originalRead(privateConfig)), sha(privateBytes), 'synthetic private configuration was changed');
    const resolved = path.resolve(scratch), temp = path.resolve(os.tmpdir());
    if (!resolved.startsWith(temp + path.sep) || !path.basename(resolved).startsWith('memory-brain-unknown-host-'))
      throw new Error('refusing_cleanup_outside_the_synthetic_temp_directory');
    fs.rmSync(resolved, { recursive: true, force: true });
  }
}
