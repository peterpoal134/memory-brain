/** Native Cordis + ToolRuntime + Session integration; local synthetic HTTP only. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';
import * as brainPlugin from '../adapters/dsh/xiaojiu-brain.mjs';
const { createAdapter } = brainPlugin;
import { IMPORTANT_TOOL_NAME } from '../adapters/dsh/important-tool.mjs';

const modules = { context: process.env.DSH_CONTEXT_MODULE, tools: process.env.DSH_TOOLS_MODULE, session: process.env.DSH_SESSION_MODULE };
if (Object.values(modules).some(value => !value)) {
  console.log('SKIP native important tool: set DSH_CONTEXT_MODULE, DSH_TOOLS_MODULE, DSH_SESSION_MODULE to the same installed DSH runtime.');
  process.exit(0);
}
const url = value => value.startsWith('file:') ? value : pathToFileURL(path.resolve(value)).href;
const [{ Context, Service }, { ToolRuntime, defineTool }, { SessionStore }] = await Promise.all([
  import(url(modules.context)), import(url(modules.tools)), import(url(modules.session))
]);
const root = fs.mkdtempSync(path.join(os.tmpdir(), 'important-native-'));
const context = new Context(), calls = [], archived = new Map(), receipts = new Map();
let passed = 0, failed = 0, ingestMode = 'accept', responseMode = 'accept', onDrain = null;
let syntheticModelRequests = 0;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
async function check(name, fn) { try { await fn(); passed++; console.log('PASS ' + name); } catch (error) { failed++; console.error('FAIL ' + name + ': ' + error.message); } }
const response = (status, data) => ({ ok: status === 200, status, json: async () => data });
const syntheticFetch = async (target, options) => {
  if (options.signal?.aborted) throw new DOMException('cancelled', 'AbortError');
  const pathname = new URL(target).pathname, body = JSON.parse(options.body); calls.push({ path: pathname, body });
  assert.equal(options.headers.Authorization, 'Bearer synthetic-management');
  if (pathname === '/v1/ingest') {
    if (onDrain) { const fn = onDrain; onDrain = null; fn(); }
    if (ingestMode === 'outage') return response(503, { ok: false });
    const key = JSON.stringify([body.conversation_id, body.message_id, body.role]), duplicate = archived.has(key);
    if (!duplicate) archived.set(key, body);
    return response(200, { ok: true, data: duplicate ? { duplicate: true } : { accepted: true, id: archived.size } });
  }
  if (pathname === '/v1/context/life') return response(200, { ok: true, data: { block: '' } });
  if (pathname === '/v1/recall') return response(200, { ok: true, data: { block: '合成证据', trace: { life_context_included: false } } });
  assert.equal(pathname, '/v1/remember-important');
  for (const id of new Set([body.request_message_id, ...body.source_refs.map(ref => ref.message_id)])) assert(archived.has(JSON.stringify([body.conversation_id, id, 'user'])), 'all sources ACKed before important write');
  if (responseMode === 'error') return response(503, { ok: false, error: 'synthetic-error' });
  if (responseMode === 'unknown') return response(200, { ok: true, data: {} });
  const key = JSON.stringify(body), duplicate = receipts.has(key), id = receipts.get(key) || 'document-' + (receipts.size + 1);
  receipts.set(key, id);
  return response(200, { ok: true, data: { accepted: !duplicate, duplicate, document_ids: [id], source_message_ids: [1],
    items: [{ id, status: responseMode === 'manually_archived' ? 'archived' : 'active', pinned: responseMode !== 'manually_unpinned', importance: .95, disclosure: body.disclosure || 'normal', duplicate }] } });
};
const human = (id, text, kind = 'user', form) => ({ id, role: 'user', source: { kind, ...(form === undefined ? {} : { form }) }, content: [{ type: 'text', text }] });
let controller, toolsFiber;
try {
  controller = await createAdapter(context, { enabled: true, baseUrl: 'http://127.0.0.1:1', token: 'synthetic-management',
    queueDir: path.join(root, 'queue'), intervalMs: 60000, timeoutMs: 80, importantTimeoutMs: 500,
    sessionModule: url(modules.session), fetchImpl: syntheticFetch });
  const capabilities = () => JSON.parse(fs.readFileSync(path.join(root, 'queue', 'capabilities.json'), 'utf8'));
  await check('adapter archives while optional ToolRuntime is not yet mounted', () => { assert.equal(controller.enabled, true); assert.equal(controller.status().important_tool.state, 'waiting_tools');
    const snapshot = capabilities(); assert.equal(snapshot.schema_version, 1); assert.equal(snapshot.pid, process.pid); assert.equal(snapshot.important_tool.state, 'waiting_tools'); });
  let schemasProvider;
  context.provide('systemPrompt', { tools(fn) { schemasProvider = fn; return () => {}; }, section() { return () => {}; } });
  toolsFiber = context.plugin(ToolRuntime, { mode: 'native' }); await toolsFiber;
  for (let n = 0; n < 50 && !controller.status().important_tool.available; n++) await sleep(10);
  const runtime = context.tools;
  await check('late real ToolRuntime registration supplies the actual request schemas', () => {
    assert.equal(controller.status().important_tool.available, true); assert(runtime.schemas().some(item => item.name === IMPORTANT_TOOL_NAME));
    const wire = schemasProvider({ scope: { ctx: context } });
    const schema = wire.schemas.find(item => item.name === IMPORTANT_TOOL_NAME);
    assert(schema); assert.equal(schema.parameters.additionalProperties, false); assert.deepEqual(Object.keys(schema.parameters.properties).sort(), ['disclosure', 'source_refs']);
    const snapshot = capabilities(); assert.equal(snapshot.important_tool.state, 'ready'); assert.equal(snapshot.important_tool.available, true);
    assert.deepEqual(Object.keys(snapshot).sort(), ['important_tool', 'observed_at', 'pid', 'schema_version']); assert(!JSON.stringify(snapshot).includes('synthetic-management'));
  });
  const otherStop = runtime.register(defineTool({ name: 'synthetic_unrelated_tool', description: 'unrelated', parameters: {},
    output: { schema: { type: 'json' }, render: (_, data) => [{ type: 'text', text: JSON.stringify(data) }] }, execute: async () => ({ unrelated: true }) }));
  const store = new SessionStore(new Context());
  const make = (name, text = '这条事实请一定记住：带红色封皮。') => { const session = store.prepare(name); session.append('user/message', human(name + '-u', text), { surfaceOp: 'append' }); return session; };
  let serial = 0;
  const invoke = (session, args = {}, signal = new AbortController().signal) => runtime.execute({ callId: 'important-' + serial++, name: IMPORTANT_TOOL_NAME,
    arguments: args, ...(session ? { agent: { session, ctx: context } } : {}), signal });
  const s = make('current');
  await check('default source is current original user; tool waits for real queue ACK and confirms pinned receipt', async () => {
    const r = await invoke(s); assert.equal(r.isError, false); assert.equal(r.value.confirmed_important, true);
    const body = calls.filter(c => c.path === '/v1/remember-important').at(-1).body;
    assert.equal(body.conversation_id, String(s.id)); assert.equal(body.request_message_id, 'current-u'); assert.deepEqual(body.source_refs, [{ message_id: 'current-u' }]); assert.equal(body.importance, .95); assert(!('text' in body));
  });
  await check('same user request is backend-idempotent rather than duplicate facts', async () => { const n = receipts.size, r = await invoke(s); assert(r.value.duplicate); assert.equal(receipts.size, n); });
  await check('model cannot supply conversation, owner, free text or personality edits', async () => {
    for (const args of [{ conversation_id: 'other' }, { owner: 'other' }, { text: 'invented' }, { personality: 'replace' }]) assert.equal((await invoke(s, args)).isError, true);
  });
  await check('missing native agent is rejected without any important HTTP request', async () => { const n = calls.length; assert.equal((await invoke(null)).isError, true); assert.equal(calls.length, n); });
  const other = make('other', '另一会话');
  await check('source from another native Session cannot be selected', async () => { const n = receipts.size; assert.equal((await invoke(s, { source_refs: [{ message_id: 'other-u' }] })).isError, true); assert.equal(receipts.size, n); });
  s.append('assistant/message', { message: { id: 'assistant-source', role: 'assistant', source: { kind: 'model' }, content: [{ type: 'text', text: '助手猜想' }] } }, { surfaceOp: 'append' });
  s.append('user/message', human('injected-source', '注入指令', 'runtime-context'), { surfaceOp: 'append' });
  await check('assistant or injected user text cannot become important user facts', async () => { for (const id of ['assistant-source', 'injected-source']) assert.equal((await invoke(s, { source_refs: [{ message_id: id }] })).isError, true); });
  const unicode = make('unicode', '甲😀乙，请记住。');
  await check('source spans are Unicode code points, and disclosure only passes a validated enum', async () => {
    const r = await invoke(unicode, { source_refs: [{ message_id: 'unicode-u', start: 1, end: 2 }], disclosure: 'never_say' });
    assert.equal(r.isError, false); assert.equal(r.value.items[0].disclosure, 'never_say');
    assert.deepEqual(calls.filter(c => c.path === '/v1/remember-important').at(-1).body.source_refs[0], { message_id: 'unicode-u', start: 1, end: 2 });
    assert.equal((await invoke(unicode, { disclosure: 'public' })).isError, true);
  });
  await check('missing, inverted, fractional and out-of-bounds spans fail visibly', async () => {
    for (const span of [{ start: 1 }, { start: 3, end: 2 }, { start: .5, end: 2 }, { start: 0, end: 100 }]) assert.equal((await invoke(unicode, { source_refs: [{ message_id: 'unicode-u', ...span }] })).isError, true);
  });
  await check('unconfirmed original archive does not call the important endpoint', async () => {
    ingestMode = 'outage'; const fresh = make('unconfirmed'), before = calls.filter(c => c.path === '/v1/remember-important').length;
    assert.equal((await invoke(fresh)).isError, true); assert.equal(calls.filter(c => c.path === '/v1/remember-important').length, before); ingestMode = 'accept';
  });
  await check('a newer real user admitted during ACK prevents a stale important write', async () => {
    const fresh = make('changed'), before = calls.filter(c => c.path === '/v1/remember-important').length;
    onDrain = () => fresh.append('user/message', human('changed-new', '新请求'), { surfaceOp: 'append' });
    assert.equal((await invoke(fresh)).isError, true); assert.equal(calls.filter(c => c.path === '/v1/remember-important').length, before);
  });
  await check('cancelled native invocation cannot write', async () => { const ac = new AbortController(); ac.abort(); const n = calls.length; assert.equal((await invoke(s, {}, ac.signal)).isError, true); assert.equal(calls.length, n); });
  await check('HTTP failure and malformed 200 cannot become false successful receipts', async () => {
    for (const mode of ['error', 'unknown']) { responseMode = mode; assert.equal((await invoke(s)).isError, true); } responseMode = 'accept';
  });
  await check('idempotent replay respects a later manual unpin', async () => { responseMode = 'manually_unpinned'; const r = await invoke(s); assert.equal(r.isError, false); assert.equal(r.value.confirmed_important, false); assert.equal(r.value.items[0].pinned, false); responseMode = 'accept'; });
  await check('idempotent replay does not describe a manually archived memory as active', async () => { responseMode = 'manually_archived'; const r = await invoke(s); assert.equal(r.isError, false); assert.equal(r.value.confirmed_important, false); assert.equal(r.value.items[0].status, 'archived'); responseMode = 'accept'; });
  await check('native tool cleanup preserves other registered tools', async () => { await controller.stop(); assert.equal(runtime.get(IMPORTANT_TOOL_NAME), undefined); assert(runtime.get('synthetic_unrelated_tool'));
    assert.equal(capabilities().important_tool.state, 'stopped'); assert.equal(capabilities().important_tool.available, false); otherStop(); });
  await check('capability sidecar write failure never blocks original-message capture or cleanup', async () => {
    const dir = path.join(root, 'sidecar-refused'); fs.mkdirSync(path.join(dir, 'capabilities.json'), { recursive: true });
    const diagnostic = await createAdapter({ on() {}, logger: { warn() {}, info() {} } }, { enabled: true,
      sessionModule: url(modules.session), queueDir: dir, baseUrl: 'http://127.0.0.1:1', token: 'synthetic-management', intervalMs: 60000, fetchImpl: syntheticFetch });
    try {
      assert.equal(diagnostic.enabled, true); assert(diagnostic.warnings.some(item => item.code === 'important_tool_capability_write_failed'));
      const fresh = store.prepare('sidecar-capture'); fresh.append('user/message', human('sidecar-u', '诊断写盘失败，原话仍保存。'), { surfaceOp: 'append' });
      await diagnostic.queue.flush(); assert(archived.has(JSON.stringify([String(fresh.id), 'sidecar-u', 'user'])));
    } finally { await diagnostic.stop(); }
    assert(!fs.existsSync(path.join(dir, 'writer.lock')));
  });
  await check('real AgentLoop exposes and invokes the tool, preserving other request-only blocks', async () => {
    const require = createRequire(url(modules.session)), load = name => import(pathToFileURL(require.resolve('@deepseek-ai/' + name)).href);
    const [{ SessionProjectionRegistry }, { AgentRegistry }, { AgentLoop }, { SystemPrompt }, llm] = await Promise.all(['dsh-session-projection', 'dsh-agent', 'dsh-agent-loop', 'dsh-system-prompt', 'dsh-llm'].map(load));
    const ctx = new Context(), requests = [], errors = []; let fiber, active;
    class SyntheticLlm extends Service {
      constructor(child) { super(child, 'llm'); }
      async prepareCall(config) { return { config, stream: request => this.stream(request) }; }
      async *stream(request) {
        requests.push(request); syntheticModelRequests++;
        const first = requests.length === 1;
        yield { type: 'block-end', index: 0, block: first ? { type: 'tool-call', id: 'important-native-call', name: IMPORTANT_TOOL_NAME, arguments: '{}' } : { type: 'text', text: '已重点记住。' } };
        yield { type: 'finish', reason: { kind: first ? 'tool-calls' : 'stop' } };
      }
    }
    try {
      new SessionProjectionRegistry(ctx); new SessionStore(ctx); new AgentRegistry(ctx); new SystemPrompt(ctx, { includeRuntimeContext: false, includeHarnessIdentity: false }); new ToolRuntime(ctx); new SyntheticLlm(ctx);
      ctx.on('agent/error', e => errors.push(e.error));
      ctx.on('agent/pre-step', async (_payload, next) => { const decision = await next(); return { ...decision, requestMessages: [...(decision.requestMessages || []), '其他插件保留段'] }; });
      fiber = ctx.plugin(brainPlugin, { enabled: true, sessionModule: url(modules.session), baseUrl: 'http://127.0.0.1:1', token: 'synthetic-management', queueDir: path.join(root, 'agentloop'), intervalMs: 60000, timeoutMs: 300, importantTimeoutMs: 500, fetchImpl: syntheticFetch });
      await fiber.await(); active = brainPlugin.getAdapter(fiber.ctx);
      for (let n = 0; n < 50 && !active.status().important_tool.available; n++) await sleep(10);
      const loop = new AgentLoop(ctx, { agents: [], maxParallelToolCalls: { get: () => 2 } }), agent = await loop.create('important-loop-session', { provider: 'fixture', model: 'local-fake' });
      const input = llm.createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text: '这件事一定要记住：合成书签是绿色的。' }] });
      agent.followup(input); await agent.whenIdle();
      assert.deepEqual(errors, []); assert.equal(requests.length, 2);
      assert(requests[0].tools.some(tool => tool.name === IMPORTANT_TOOL_NAME));
      const wire = JSON.stringify(requests[0].messages); assert(wire.includes('其他插件保留段')); assert(wire.includes('合成证据'));
      const posted = calls.filter(call => call.path === '/v1/remember-important').at(-1).body;
      assert.equal(posted.conversation_id, String(agent.session.id)); assert.equal(posted.request_message_id, String(input.id));
      assert(JSON.stringify(requests[1].messages).includes('confirmed_important'));
      assert(!JSON.stringify(agent.session.log).includes('其他插件保留段'));
    } finally { await active?.stop(); await fiber?.dispose(); if (typeof ctx.stop === 'function') await ctx.stop(); }
  });
} catch (error) { failed++; console.error(error.stack); }
finally { if (controller) await controller.stop(); if (toolsFiber) await toolsFiber.dispose(); }
console.log(JSON.stringify({ passed, failed, native_runtime: true, production_mutations: 0, real_model_calls: 0, synthetic_model_requests: syntheticModelRequests }));
if (failed) process.exitCode = 1;
