/** Offline contract checks for the DSH client. The hook model below is not a
 * browser renderer and does not establish real Desktop/UI installation success.
 * No kernel, network, credentials, dependency installation or DOM is used.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const source = fs.readFileSync(new URL('../client/client.js', import.meta.url), 'utf8');
const plain = value => JSON.parse(JSON.stringify(value));
const settle = () => new Promise(resolve => setImmediate(resolve));
const sameDeps = (left, right) => Array.isArray(left) && Array.isArray(right) &&
  left.length === right.length && left.every((value, index) => Object.is(value, right[index]));

function statusFixture(overrides = {}) {
  return {
    phase: 'setup_required', operation: null, busy: false, error: null,
    csrfToken: 'synthetic-csrf-not-a-real-secret', home: 'D:/SyntheticMemoryBrain/profile-a',
    runtime: { state: 'not_initialized', installed_version: null, expected_version: 'synthetic-version',
      python: { available: true, version: '3.11' }, models: {}, running: false, restart_required: false },
    adapter: { enabled: false }, compatibility: 'native_snapshot', ...overrides,
  };
}

function readyFixture(overrides = {}) {
  const fixture = statusFixture();
  fixture.runtime = { ...fixture.runtime, state: 'ready', installed_version: 'synthetic-version', ...overrides };
  return fixture;
}

function nodes(value) {
  if (Array.isArray(value)) return value.flatMap(nodes);
  if (!value || typeof value !== 'object') return [];
  return [value, ...(value.children || []).flatMap(nodes)];
}

function visibleText(value) {
  if (Array.isArray(value)) return value.map(visibleText).join('');
  if (value == null || typeof value === 'boolean') return '';
  if (typeof value !== 'object') return String(value);
  return (value.children || []).map(visibleText).join('');
}

function harness({ status = statusFixture(), baseURI = 'dsh-app://app/', reply, open, clipboard } = {}) {
  const hooks = [], effectQueue = [], registrations = [], calls = [], required = [];
  const timers = new Map();
  let cursor = 0, timerId = 0, entry, tree;
  const React = {
    createElement(type, props, ...children) { return { type, props: props || {}, children }; },
    useState(initial) {
      const index = cursor++;
      if (!(index in hooks)) hooks[index] = { value: typeof initial === 'function' ? initial() : initial };
      return [hooks[index].value, value => {
        hooks[index].value = typeof value === 'function' ? value(hooks[index].value) : value;
      }];
    },
    useRef(initial) {
      const index = cursor++;
      if (!(index in hooks)) hooks[index] = { current: initial };
      return hooks[index];
    },
    useCallback(callback, deps) {
      const index = cursor++;
      if (!(index in hooks) || !sameDeps(hooks[index].deps, deps)) hooks[index] = { callback, deps };
      return hooks[index].callback;
    },
    useEffect(effect, deps) {
      const index = cursor++;
      if (!(index in hooks) || !sameDeps(hooks[index].deps, deps)) {
        const old = hooks[index];
        hooks[index] = { deps, cleanup: old?.cleanup };
        effectQueue.push(() => { hooks[index].cleanup?.(); hooks[index].cleanup = effect(); });
      }
    },
  };
  const addTimer = (callback, milliseconds, repeating) => {
    const id = ++timerId; timers.set(id, { callback, milliseconds, repeating }); return id;
  };
  vm.runInNewContext(source, {
    window: { __ModuleLoader__: { load(value) { assert.equal(entry, undefined); entry = value; } }, open: open || (() => null) },
    document: { baseURI }, URL, AbortController,
    navigator: { clipboard },
    setTimeout: (fn, ms) => addTimer(fn, ms, false), clearTimeout: id => timers.delete(id),
    setInterval: (fn, ms) => addTimer(fn, ms, true), clearInterval: id => timers.delete(id),
    async fetch(url, options) {
      calls.push({ url, options });
      const supplied = reply ? await reply(url, options) : undefined;
      if (supplied) return supplied;
      const action = url.split('/').at(-1);
      const data = action === 'status' ? status : action === 'panel' ?
        { url: 'http://127.0.0.1:18881/brain#ticket=synthetic-once-ticket' } : { accepted: true };
      return { ok: true, status: 200, json: async () => ({ ok: true, data: structuredClone(data) }) };
    },
  }, { filename: 'client/client.js' });
  const plugin = entry.factory(name => { required.push(name); assert.equal(name, 'react'); return React; });
  plugin.apply({ slots: {
    inject(_name, callback) { return callback(); },
    register(options, component) { registrations.push({ options, component }); return () => {}; },
  } });
  function render(props = { view: 'page' }) {
    cursor = 0;
    tree = registrations[0].component(props);
    // Resolve only the top-level function component selected by the slot. The
    // summary branch must stop before SetupCard exists or any hooks run.
    while (tree && typeof tree.type === 'function') tree = tree.type({ ...tree.props, children: tree.children });
    return tree;
  }
  async function mount(props = { view: 'page' }) {
    render(props); while (effectQueue.length) effectQueue.shift()();
    await settle(); await settle(); return render(props);
  }
  function dispose() { for (const hook of hooks) hook?.cleanup?.(); }
  const find = predicate => nodes(tree).find(predicate);
  function button(label) {
    const result = find(node => node.type === 'button' && visibleText(node) === label);
    assert.ok(result, `button exists: ${label}`); return result;
  }
  function input(name) {
    const result = find(node => node.type === 'input' && node.props.name === name);
    assert.ok(result, `input exists: ${name}`); return result;
  }
  return { entry, plugin, required, registrations, calls, timers, render, mount, dispose, find, button, input,
    get tree() { return tree; }, get text() { return visibleText(tree); }, get hookCount() { return hooks.length; } };
}

test('native loader uses the package identity, host React and two package-keyed slots', () => {
  const env = harness();
  assert.equal(env.entry.id, 'dsh-memory-brain');
  assert.deepEqual(env.required, ['react']);
  assert.deepEqual(plain(env.plugin.inject), ['slots']);
  assert.deepEqual(env.registrations.map(item => plain(item.options)), [
    { name: 'plugins.bundle.config', key: 'dsh-memory-brain' },
    { name: 'plugins.bundle.activation', key: 'dsh-memory-brain' },
  ]);
});

test('the summary view mounts no hooks, reads or polling; activation opens this package details', async () => {
  const env = harness();
  assert.equal(await env.mount({ view: 'summary' }), null);
  assert.equal(env.hookCount, 0);
  assert.equal(env.calls.length, 0);
  assert.equal(env.timers.size, 0);
  let opened = 0;
  const activation = env.registrations[1].component({ onOpenDetails: () => opened++ });
  const button = nodes(activation).find(node => node.type === 'button');
  button.props.onClick(); assert.equal(opened, 1);
});

test('mount only reads status and never installs or starts the kernel automatically', async () => {
  const env = harness();
  try {
    await env.mount();
    assert.equal(env.calls.length, 1);
    const call = env.calls[0];
    assert.equal(call.url, '/api/memory-brain/setup/status');
    assert.equal(call.options.method, 'GET');
    assert.equal(call.options.credentials, 'same-origin');
    assert.equal(call.options.cache, 'no-store');
    assert.equal(call.options.body, undefined);
    assert.ok(env.text.includes('尚未开始保存或召回'));
    assert.ok(env.text.includes('D:/SyntheticMemoryBrain/profile-a'));
    assert.equal(env.button('启用忆核大脑').props.disabled, true);
  } finally { env.dispose(); }
});

test('only the explicit preparation click sends download consent with CSRF and same-origin', async () => {
  const env = harness({ baseURI: 'https://example.invalid/dsh/' });
  try {
    await env.mount();
    assert.equal(env.calls[0].url, '/dsh/api/memory-brain/setup/status');
    const button = env.button('准备运行环境'); assert.equal(button.props.disabled, false);
    await button.props.onClick();
    const posts = env.calls.filter(call => call.options.method === 'POST');
    assert.equal(posts.length, 1);
    assert.equal(posts[0].url, '/dsh/api/memory-brain/setup/initialize');
    assert.deepEqual(JSON.parse(posts[0].options.body), { allowDownloads: true });
    assert.equal(posts[0].options.headers['X-Memory-Brain-Setup'], 'synthetic-csrf-not-a-real-secret');
    assert.equal(posts[0].options.headers['Content-Type'], 'application/json');
    assert.equal(posts[0].options.credentials, 'same-origin');
  } finally { env.dispose(); }
});

test('in-flight action guard refuses a second click even before a UI rerender', async () => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const env = harness({ reply: async (_url, options) => {
    if (options.method === 'POST') { await pending; return { ok: true, status: 202, json: async () => ({ ok: true, data: { accepted: true } }) }; }
  } });
  try {
    await env.mount();
    const click = env.button('准备运行环境').props.onClick;
    const first = click(), second = click();
    assert.equal(env.calls.filter(call => call.options.method === 'POST').length, 1);
    release(); await Promise.all([first, second]);
  } finally { release(); env.dispose(); }
});

test('model form only sends filled fields and an empty new key cannot overwrite a saved key', async () => {
  const fixture = readyFixture({ models: { embed_key_saved: true, llm_key_saved: true } });
  const env = harness({ status: fixture });
  try {
    await env.mount();
    env.input('embed_url').props.onChange({ target: { value: '  http://127.0.0.1:8781/v1  ' } });
    env.input('llm_model').props.onChange({ target: { value: ' synthetic-model ' } });
    env.input('embed_key').props.onChange({ target: { value: '   ' } });
    env.render();
    let prevented = false;
    await env.find(node => node.type === 'form').props.onSubmit({ preventDefault() { prevented = true; } });
    assert.equal(prevented, true);
    const post = env.calls.find(call => call.url.endsWith('/models'));
    assert.deepEqual(JSON.parse(post.options.body), { embed_url: 'http://127.0.0.1:8781/v1', llm_model: 'synthetic-model' });
    assert.equal(post.options.headers['X-Memory-Brain-Setup'], fixture.csrfToken);
  } finally { env.dispose(); }
});

test('submitted new keys use password controls and are cleared after a successful save', async () => {
  const env = harness({ status: readyFixture() });
  try {
    await env.mount();
    assert.equal(env.input('embed_key').props.type, 'password');
    assert.equal(env.input('llm_key').props.autoComplete, 'new-password');
    const syntheticKey = 'synthetic-test-key-never-real';
    env.input('llm_key').props.onChange({ target: { value: syntheticKey } });
    env.render();
    assert.ok(!env.text.includes(syntheticKey));
    await env.find(node => node.type === 'form').props.onSubmit({ preventDefault() {} });
    assert.equal(JSON.parse(env.calls.find(call => call.url.endsWith('/models')).options.body).llm_key, syntheticKey);
    env.render();
    assert.equal(env.input('llm_key').props.value, '');
    assert.equal(env.input('embed_key').props.value, '');
    assert.ok(!JSON.stringify(env.tree).includes(syntheticKey));
  } finally { env.dispose(); }
});

test('status extra secrets are not rendered and HTML-like data remains a text child', async () => {
  const fixture = readyFixture();
  fixture.runtime.models = { embedding_configured: true, embed_key_saved: true, embed_key: 'synthetic-readback-secret' };
  fixture.api_token = 'synthetic-management-secret';
  fixture.home = '<img src=x onerror=synthetic()>literal-path';
  const env = harness({ status: fixture });
  try {
    await env.mount();
    const serialized = JSON.stringify(env.tree);
    assert.ok(!serialized.includes('synthetic-readback-secret'));
    assert.ok(!serialized.includes('synthetic-management-secret'));
    assert.ok(!serialized.includes(fixture.csrfToken));
    assert.ok(env.text.includes(fixture.home));
    assert.ok(nodes(env.tree).every(node => !('dangerouslySetInnerHTML' in node.props)));
    assert.ok(nodes(env.tree).every(node => node.type !== 'img' && node.type !== 'script'));
  } finally { env.dispose(); }
});

test('an empty model form does not post empty model fields', async () => {
  const env = harness({ status: readyFixture() });
  try {
    await env.mount();
    await env.find(node => node.type === 'form').props.onSubmit({ preventDefault() {} });
    assert.equal(env.calls.filter(call => call.options.method === 'POST').length, 0);
    env.render(); assert.ok(env.text.includes('已有配置保持不变'));
  } finally { env.dispose(); }
});

test('legacy setup is read-only for initialization and model configuration', async () => {
  const fixture = readyFixture({ state: 'existing_local', installed_version: 'existing', running: true });
  fixture.legacy = true;
  const env = harness({ status: fixture });
  try {
    await env.mount();
    assert.equal(env.find(node => node.type === 'form'), undefined);
    assert.equal(env.find(node => node.type === 'input'), undefined);
    assert.ok(!nodes(env.tree).some(node => node.type === 'button' && visibleText(node).includes('准备环境')));
    assert.ok(env.text.includes('原配置和记忆位置保持不变'));
    assert.equal(env.button('打开 3D 大脑与控制面板').props.disabled, false);
  } finally { env.dispose(); }
});

test('missing Python disables preparation and links only to the official manual download', async () => {
  const fixture = statusFixture(); fixture.runtime.python = { available: false };
  const env = harness({ status: fixture });
  try {
    await env.mount();
    assert.equal(env.button('准备运行环境').props.disabled, true);
    const link = env.find(node => node.type === 'a');
    assert.equal(link.props.href, 'https://www.python.org/downloads/');
    assert.equal(link.props.rel, 'noopener noreferrer');
    assert.equal(env.calls.length, 1);
  } finally { env.dispose(); }
});

test('an existing active kernel shows an explicit apply-and-reconnect action for model changes', async () => {
  const fixture = readyFixture({ running: true, restart_required: true }); fixture.adapter.enabled = true;
  const env = harness({ status: fixture });
  try {
    await env.mount();
    assert.ok(env.text.includes('先结束当前对话'));
    assert.ok(env.text.includes('不重启 DSH'));
    await env.button('应用配置并重新连接').props.onClick();
    const post = env.calls.find(call => call.url.endsWith('/start'));
    assert.deepEqual(JSON.parse(post.options.body), {});
    assert.equal(post.options.method, 'POST');
  } finally { env.dispose(); }
});

test('successful panel response provides a local one-time ticket link when popups are unavailable', async () => {
  const env = harness({ status: readyFixture({ running: true }) });
  try {
    await env.mount(); await env.button('打开 3D 大脑与控制面板').props.onClick(); env.render();
    const link = env.find(node => node.type === 'a' && node.props.href?.includes('#ticket='));
    assert.ok(link);
    assert.equal(new URL(link.props.href).hostname, '127.0.0.1');
    assert.equal(new URL(link.props.href).hash, '#ticket=synthetic-once-ticket');
    assert.equal(link.props.rel, 'noopener noreferrer');
    assert.ok(!env.text.includes('synthetic-once-ticket'));
    const post = env.calls.find(call => call.url.endsWith('/panel'));
    assert.equal(post.options.credentials, 'same-origin');
    assert.deepEqual(JSON.parse(post.options.body), {});
  } finally { env.dispose(); }
});

test('a non-local panel response is rejected instead of turned into a navigation link', async () => {
  const env = harness({ status: readyFixture({ running: true }), reply: async url => {
    if (url.endsWith('/panel')) return { ok: true, status: 200, json: async () => ({ ok: true, data: { url: 'https://example.invalid/brain#ticket=synthetic' } }) };
  } });
  try {
    await env.mount(); await env.button('打开 3D 大脑与控制面板').props.onClick(); env.render();
    assert.ok(env.text.includes('面板地址不是本机服务'));
    assert.equal(env.find(node => node.type === 'a'), undefined);
  } finally { env.dispose(); }
});

test('plain 401 and 404 replies produce useful entry errors without requiring JSON', async () => {
  for (const code of [401, 404]) {
    let jsonRead = false;
    const env = harness({ reply: async () => ({ ok: false, status: code, json: async () => { jsonRead = true; throw new Error('plain host reply'); } }) });
    try {
      await env.mount();
      assert.equal(jsonRead, false);
      assert.ok(env.text.includes(code === 401 ? 'DSH 登录已失效' : '首次设置入口尚未加载'));
      assert.equal(env.calls.filter(call => call.options.method === 'POST').length, 0);
    } finally { env.dispose(); }
  }
});

test('unmount clears the polling timer and aborts the outstanding status request', async () => {
  let capturedSignal, release;
  const blocked = new Promise(resolve => { release = resolve; });
  const env = harness({ reply: async (_url, options) => {
    capturedSignal = options.signal; await blocked;
  } });
  env.render();
  // Commit this mount without waiting for its deliberately blocked fetch.
  const mounting = env.mount();
  await settle(); env.dispose();
  assert.equal(capturedSignal.aborted, true);
  assert.ok([...env.timers.values()].every(timer => !timer.repeating));
  release(); await mounting;
  assert.equal(env.timers.size, 0);
});

function diagnosticFixture() {
  const fixture = readyFixture({ running: true });
  fixture.adapter.enabled = true; // Unsupported compatibility must override this stale state.
  fixture.compatibility = { supported: false, mode: 'diagnostic_only', reason: 'unknown_loop_fingerprint',
    plugin_version: '0.2.0-alpha.18', node_version: '24.19.0', platform: 'android', arch: 'arm64',
    modules: { loop: { version: '0.2.1-rc.2', sha256: 'a'.repeat(64), canonical_sha256: 'b'.repeat(64),
      path: '/synthetic/private/loop.js', token: 'synthetic-module-secret' },
      connection: { version: '0.2.1-rc.2', sha256: 'c'.repeat(64) },
      '/synthetic/private/other': { version: '0.2.1-rc.2', sha256: 'd'.repeat(64) } },
    path: '/synthetic/private/host', home: '/synthetic/private/compatibility-home', token: 'synthetic-compat-secret',
    loop_sha256: 'a'.repeat(64), loop_canonical_sha256: 'b'.repeat(64) };
  return fixture;
}

test('unsupported host is diagnostic-only with no install, model, start or panel actions', async () => {
  const env = harness({ status: diagnosticFixture() });
  try {
    await env.mount();
    assert.ok(env.text.includes('当前构建尚未完成核验，采集和召回未启用'));
    assert.ok(env.text.includes('手机独立 App'));
    assert.ok(env.text.includes('不能仅凭'));
    assert.equal(env.find(node => node.type === 'form'), undefined);
    assert.equal(env.find(node => node.type === 'input'), undefined);
    assert.deepEqual(nodes(env.tree).filter(node => node.type === 'button').map(visibleText), ['重新检查', '复制安全诊断']);
    await env.button('重新检查').props.onClick();
    assert.equal(env.calls.length, 2);
    assert.ok(env.calls.every(call => call.options.method === 'GET'));
    assert.ok(!env.text.includes('D:/SyntheticMemoryBrain/profile-a'));
  } finally { env.dispose(); }
});

test('diagnostic clipboard receives only whitelisted compatibility metadata, never paths or status credentials', async () => {
  const fixture = diagnosticFixture();
  fixture.api_token = 'synthetic-status-token';
  let copied;
  const env = harness({ status: fixture, clipboard: { async writeText(text) { copied = text; } } });
  try {
    await env.mount(); await env.button('复制安全诊断').props.onClick(); env.render();
    const data = JSON.parse(copied);
    assert.deepEqual(data.modules.loop, { version: '0.2.1-rc.2', sha256: 'a'.repeat(64), canonical_sha256: 'b'.repeat(64) });
    assert.deepEqual(Object.keys(data.modules), ['loop', 'connection']);
    assert.equal(data.plugin_version, '0.2.0-alpha.18');
    assert.equal(data.platform, 'android');
    assert.equal(data.supported, false);
    for (const excluded of [fixture.home, fixture.api_token, fixture.csrfToken, '/synthetic/private', 'synthetic-module-secret', 'synthetic-compat-secret']) {
      assert.ok(!copied.includes(excluded));
      assert.ok(!JSON.stringify(env.tree).includes(excluded));
    }
    assert.ok(env.text.includes('安全诊断已复制'));
    assert.equal(env.find(node => node.type === 'textarea'), undefined);
    assert.equal(env.calls.length, 1);
  } finally { env.dispose(); }
});

test('clipboard failure offers a read-only manual diagnostic copy without external requests', async () => {
  const env = harness({ status: diagnosticFixture(), clipboard: { async writeText() { throw new Error('permission denied'); } } });
  try {
    await env.mount(); await env.button('复制安全诊断').props.onClick(); env.render();
    const textarea = env.find(node => node.type === 'textarea');
    assert.ok(textarea); assert.equal(textarea.props.readOnly, true);
    assert.equal(JSON.parse(textarea.props.value).mode, 'diagnostic_only');
    assert.ok(env.text.includes('手动复制'));
    assert.equal(env.calls.length, 1);
  } finally { env.dispose(); }
});

test('a diagnostic-only status also blocks handlers retained from the preceding supported render', async () => {
  const fixture = readyFixture();
  const env = harness({ status: fixture });
  try {
    await env.mount(); const staleStart = env.button('启用忆核大脑').props.onClick;
    fixture.compatibility = diagnosticFixture().compatibility;
    await env.button('重新检查').props.onClick(); env.render();
    await staleStart();
    assert.equal(env.calls.filter(call => call.options.method === 'POST').length, 0);
    assert.ok(env.text.includes('运行环境诊断'));
  } finally { env.dispose(); }
});

test('a supported compatibility object preserves the existing setup and 404 requests never fall back anonymously', async () => {
  const fixture = readyFixture(); fixture.compatibility = { supported: true, mode: 'scoped_agent_bridge' };
  const env = harness({ status: fixture });
  try {
    await env.mount(); assert.equal(env.button('启用忆核大脑').props.disabled, false);
    assert.ok(env.find(node => node.type === 'form'));
  } finally { env.dispose(); }
  const missing = harness({ reply: async () => ({ ok: false, status: 404 }) });
  try {
    await missing.mount();
    assert.ok(missing.text.includes('当前宿主可能不支持诊断入口'));
    assert.ok(missing.text.includes('宿主作者提供运行环境信息'));
    assert.equal(missing.calls.length, 1);
    assert.equal(missing.calls[0].url, '/api/memory-brain/setup/status');
    assert.equal(missing.calls[0].options.credentials, 'same-origin');
  } finally { missing.dispose(); }
});
