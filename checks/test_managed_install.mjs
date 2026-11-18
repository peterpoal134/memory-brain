/** Managed first-run regressions with a synthetic installer and loopback. */
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import { EventEmitter } from 'node:events';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { pathToFileURL } from 'node:url';
import { createOnboarding } from '../adapters/dsh/onboarding.mjs';

const entry = process.env.MEMORY_BRAIN_INSTALL_ENTRY;
const { createRuntimeManager } = await import(entry
  ? pathToFileURL(path.resolve(entry)).href : '../adapters/dsh/runtime-home.mjs');
const version = '0.2.8';
const currentWheel = 'xiaojiu_brain-0.2.8-py3-none-any.whl';
const olderWheel = 'xiaojiu_brain-0.2.7-py3-none-any.whl';

function fixture(t, wheels = [currentWheel]) {
  const base = fs.realpathSync(os.tmpdir());
  const dir = fs.mkdtempSync(path.join(base, 'brain-managed-install-'));
  t.after(() => {
    const resolved = fs.realpathSync(dir);
    assert.equal(path.dirname(resolved), base);
    assert.ok(path.basename(resolved).startsWith('brain-managed-install-'));
    fs.rmSync(resolved, { recursive: true, force: true });
  });
  const packageRoot = path.join(dir, 'package');
  fs.mkdirSync(path.join(packageRoot, 'compat'), { recursive: true });
  fs.mkdirSync(path.join(packageRoot, 'wheels'));
  fs.writeFileSync(path.join(packageRoot, 'pyproject.toml'), '[project]\nversion = "' + version + '"\n');
  const dependency = { filename: 'tzdata-2026.6-py2.py3-none-any.whl', license: 'Apache-2.0',
    source: 'https://files.pythonhosted.org/synthetic-test',
    sha256: crypto.createHash('sha256').update('fixture').digest('hex') };
  fs.writeFileSync(path.join(packageRoot, 'compat', 'runtime-dependencies.json'),
    JSON.stringify({ format: 1, files: [dependency] }));
  for (const name of [dependency.filename, ...wheels]) {
    fs.writeFileSync(path.join(packageRoot, 'wheels', name), 'fixture');
  }

  const listeners = new Map();
  t.mock.method(net, 'createServer', () => {
    const server = new EventEmitter();
    server.listen = ({ port }, ready) => queueMicrotask(() => {
      if (listeners.has(port)) server.emit('error', new Error('synthetic_port_in_use'));
      else ready();
    });
    server.close = callback => callback();
    return server;
  });
  t.mock.method(net, 'createConnection', ({ port }) => {
    const socket = new EventEmitter();
    socket.setTimeout = () => {};
    socket.destroy = () => {};
    queueMicrotask(() => socket.emit(listeners.has(port) ? 'connect' : 'error'));
    return socket;
  });
  t.mock.method(globalThis, 'fetch', async (address, options) => {
    const url = new URL(address);
    assert.equal(url.hostname, '127.0.0.1');
    assert.equal(url.pathname, '/v1/status', 'first-run startup must not stop a listener');
    const token = listeners.get(Number(url.port));
    if (token && options.headers.Authorization === 'Bearer ' + token) {
      return Response.json({ ok: true, data: { version } });
    }
    return Response.json({ ok: false }, { status: 401 });
  });

  function profile(name, { failInit = false, failStart = false } = {}) {
    const home = path.join(dir, name);
    const configFile = path.join(home, 'private', 'config.json');
    const readConfig = () => JSON.parse(fs.readFileSync(configFile, 'utf8'));
    const calls = { install: [], start: 0, venv: 0 };
    let installed = null;
    async function execute(command, args) {
      if (command === 'py') return { code: 0, stdout: '' };
      if (args.includes('venv')) {
        calls.venv++;
        const executable = path.join(home, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
        fs.mkdirSync(path.dirname(executable), { recursive: true });
        fs.writeFileSync(executable, 'synthetic');
        return { code: 0 };
      }
      if (args.includes('pip')) {
        calls.install.push(path.basename(args.at(-1)));
        installed = version;
        return { code: 0 };
      }
      if (args.includes('-c')) {
        const text = args.at(-1);
        return { code: 0, stdout: JSON.stringify(text.includes('sys.version_info')
          ? { major: 3, minor: 11, version: '3.11.9' }
          : { version: installed, prefix: path.join(home, '.venv') }) };
      }
      if (args.includes('init')) {
        if (failInit) return { code: 1 };
        const arg = flag => args[args.indexOf(flag) + 1];
        const cfg = { host: '127.0.0.1', port: Number(arg('--port')), owner: arg('--owner'),
          api_token: 'synthetic-only-runtime-management-' + name, data_dir: arg('--data-dir') };
        fs.writeFileSync(configFile, JSON.stringify(cfg));
        return { code: 0 };
      }
      if (args.includes('start')) {
        calls.start++;
        const cfg = readConfig();
        if (failStart) return { code: 1 };
        listeners.set(cfg.port, cfg.api_token);
        return { code: 0 };
      }
      throw new Error('unexpected synthetic process command');
    }
    const manager = createRuntimeManager({ packageRoot, home, execute });
    return { manager, calls, readConfig, home };
  }
  return { profile, listeners };
}

test('a full git package chooses its own wheel over historical versions', async t => {
  const { profile } = fixture(t, [olderWheel, currentWheel]);
  const fresh = profile('fresh');
  const status = await fresh.manager.initialize({ allowDownloads: true });
  assert.equal(status.installed_version, version);
  assert.equal(status.prepared, true);
  assert.deepEqual(fresh.calls.install, [currentWheel]);
});

test('an installed wheel alone does not mark a failed initial configuration as ready', async t => {
  const { profile } = fixture(t);
  const fresh = profile('failed-config', { failInit: true });
  await assert.rejects(fresh.manager.initialize({ allowDownloads: true }), /owned_process_failed/);
  const status = await createOnboarding({ manager: fresh.manager }).status();
  assert.equal(status.runtime.installed_version, version);
  assert.equal(status.runtime.prepared, false);
  assert.equal(status.phase, 'not_initialized');
});

test('refuses an older-only wheel set or two matching candidates before creating a venv', async t => {
  await t.test('older only', async subtest => {
    const { profile } = fixture(subtest, [olderWheel]);
    const fresh = profile('fresh');
    await assert.rejects(fresh.manager.initialize({ allowDownloads: true }), /kernel_wheel_version_mismatch/);
    assert.equal(fresh.calls.venv, 0);
  });
  await t.test('two at the expected version', async subtest => {
    const { profile } = fixture(subtest, [currentWheel, 'xiaojiu_brain-0.2.8-1-py3-none-any.whl']);
    const fresh = profile('fresh');
    await assert.rejects(fresh.manager.initialize({ allowDownloads: true }), /ambiguous_kernel_wheel/);
    assert.equal(fresh.calls.venv, 0);
  });
});

test('two prepared profiles start separately even if they initially chose the same port', async t => {
  const { profile, listeners } = fixture(t);
  const first = profile('first'), second = profile('second');
  await first.manager.initialize({ allowDownloads: true });
  await second.manager.initialize({ allowDownloads: true });
  const initial = second.readConfig();
  assert.equal(first.readConfig().port, initial.port);
  const data = path.join(second.home, 'data', 'fixture');
  fs.writeFileSync(data, 'keep me');
  await first.manager.start();
  await second.manager.start();
  const selected = second.readConfig();
  assert.notEqual(selected.port, initial.port);
  assert.deepEqual(selected, { ...initial, port: selected.port });
  assert.equal(fs.readFileSync(data, 'utf8'), 'keep me');
  assert.equal((await second.manager.connectionOptions()).baseUrl, 'http://127.0.0.1:' + selected.port);
  assert.equal(listeners.get(initial.port), first.readConfig().api_token);
});

test('a prior start attempt never reassigns a port, including after failure', async t => {
  const { profile, listeners } = fixture(t);
  const existing = profile('existing', { failStart: true });
  await existing.manager.initialize({ allowDownloads: true });
  const initial = existing.readConfig();
  await assert.rejects(existing.manager.start(), /owned_process_failed/);
  listeners.set(initial.port, 'unrelated-listener');
  await assert.rejects(existing.manager.start(), /kernel_identity_not_confirmed/);
  assert.equal(existing.calls.start, 1);
  assert.deepEqual(existing.readConfig(), initial);
  assert.equal(listeners.get(initial.port), 'unrelated-listener');
});

test('a fresh profile never moves a listener when CLI startup history exists', async t => {
  const { profile, listeners } = fixture(t);
  const existing = profile('existing');
  await existing.manager.initialize({ allowDownloads: true });
  const initial = existing.readConfig();
  fs.writeFileSync(path.join(existing.home, 'data', 'instance.lock'), 'synthetic startup history');
  listeners.set(initial.port, 'unresponsive-listener');
  await assert.rejects(existing.manager.start(), /kernel_identity_not_confirmed/);
  assert.equal(existing.calls.start, 0);
  assert.deepEqual(existing.readConfig(), initial);
});
