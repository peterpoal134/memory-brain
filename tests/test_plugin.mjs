/** node tests/test_plugin.mjs; optional real DSH types through explicit env paths. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import { pathToFileURL } from 'node:url';
import { DurableQueue } from '../adapters/dsh/durable-queue.mjs';
import { createAdapter, captureMessage, catchUp } from '../adapters/dsh/xiaojiu-brain.mjs';

const root = fs.mkdtempSync(path.join(os.tmpdir(), 'xiaojiu-plugin-'));
const sleep = n => new Promise(r => setTimeout(r, n));
const records = [], identities = new Map(), calls = [];
let mode = 'accept', recallDelay = 0, failures = 0, passed = 0;
const good = (id, text = '原话。', role = 'user', conversation_id = 'test-session') => ({ conversation_id, message_id: id, role, text, recorded_at: '2026-01-01T00:00:00.000Z', metadata: {} });
function check(name, fn) { try { fn(); passed++; console.log('PASS ' + name); } catch (e) { failures++; console.error('FAIL ' + name + ': ' + e.message); } }
const server = http.createServer(async (req, res) => {
  let text = ''; for await (const part of req) text += part;
  const body = text ? JSON.parse(text) : null;
  calls.push({ path: req.url, body });
  const send = (status, data) => { if (res.destroyed) return; res.writeHead(status, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(data)); };
  if (req.headers.authorization !== 'Bearer local-test-management-token') return send(401, { ok: false, error: 'unauthorized' });
  if (req.url === '/v1/context/life') return send(404, { ok: false, error: 'legacy_fixture' });
  if (req.url === '/v1/context/profile') return send(404, { ok: false, error: 'legacy_fixture' });
  if (req.url === '/v1/recall') {
    if (recallDelay) await sleep(recallDelay);
    return send(200, { ok: true, data: { block: '证据：用户明确说周日。', evidence: [], degraded: [] } });
  }
  if (mode === 'outage') return send(503, { ok: false });
  if (mode === 'unknown') return send(200, {});
  if (mode === 'reject') return send(422, { ok: false, error: 'invalid' });
  const key = JSON.stringify([body.conversation_id, body.message_id, body.role]);
  const duplicate = identities.has(key);
  if (!duplicate) { identities.set(key, body.text); records.push(body); }
  return send(200, { ok: true, data: duplicate ? { accepted: false, duplicate: true } : { accepted: true, id: records.length } });
});
await new Promise(r => server.listen(0, '127.0.0.1', r));
const baseUrl = 'http://127.0.0.1:' + server.address().port, token = 'local-test-management-token';
const makeQueue = dir => new DurableQueue({ dir: path.join(root, dir), baseUrl, token, timeoutMs: 250 }).init();
let controller, q;
try {
  q = makeQueue('strict'); q.enqueue(good('u1')); mode = 'unknown'; await q.flush();
  check('200 but unknown reply remains pending', () => assert.equal(q.stats().pending, 1));
  mode = 'outage'; await q.flush(); check('503 remains pending', () => assert.equal(q.stats().pending, 1));
  mode = 'accept'; await q.flush(); check('explicit ACK delivers', () => assert.equal(q.stats().pending, 0));
  check('stable key replay deduplicates', () => assert.equal(q.enqueue(good('u1')).duplicate, true));
  check('same identity changed body is refused', () => assert.throws(() => q.enqueue(good('u1', 'changed')), /body_changed/));
  q.close(); q = makeQueue('strict'); check('seen and ACK survive restart', () => assert(q.enqueue(good('u1')).duplicate));
  check('single writer lock blocks second queue', () => assert.throws(() => makeQueue('strict'), /already_in_use/));
  const refused = new DurableQueue({ dir: path.join(root, 'strict'), baseUrl, token });
  try { refused.init(); } catch { refused.close(); }
  check('failed second initialization cannot release the first writer lock', () => assert.throws(() => makeQueue('strict'), /already_in_use/));
  q.enqueue(good('u2')); mode = 'reject'; await q.flush();
  check('permanent rejection retained in failed', () => assert.equal(q.stats().failed_files, 1));
  q.enqueue(good('u3')); mode = 'outage'; q.sweep(Date.now() + 100 * 86400000);
  check('sweep never removes pending or failed', () => { assert.equal(q.stats().pending, 1); assert.equal(q.stats().failed_files, 1); });
  q.close(); mode = 'accept'; q = makeQueue('strict');
  const names = q.pendingFiles(); check('disk sequence does not restart at one', () => assert(Number(names[0].slice(0, 16)) >= 3));
  await q.flush(); q.close(); q = null;
  const event = (type, message, more = {}) => ({ type, time: 0, data: type === 'user/message' ? message : { message, ...more } });
  const msg = (id, role, kind, content, form) => ({ id, role, source: { kind, ...(kind === 'model' ? { provider: 'test', model: 'test-fixture' } : {}), ...(form === undefined ? {} : { form }) }, content });
  const cutoff = Date.parse('2026-11-08T00:00:00.000Z');
  const deferred = msg('deferred', 'user', 'user', [{ type: 'text', text: '切换前入箱、之后认领' }]);
  const cutoverLog = [
    { type: 'user/message', data: msg('old-history', 'user', 'user', [{ type: 'text', text: '已迁移旧历史' }]), time: cutoff - 500 },
    { type: 'agent/inbox/spliced', data: { target: 'next-turn', start: 0, inserted: [deferred] }, time: cutoff - 100 },
    { type: 'agent/inbox/spliced', data: { target: 'next-turn', start: 0, removedCount: 1, inserted: [] }, time: cutoff + 100 },
    { type: 'user/message', data: deferred, time: cutoff + 110 },
    { type: 'user/message', data: msg('new-after-cutover', 'user', 'user', [{ type: 'text', text: '新话正常保存' }]), time: cutoff + 120 },
  ].map((r, seq) => ({ ...r, seq }));
  q = new DurableQueue({ dir: path.join(root, 'cutover'), baseUrl, token, maxPending: 1 }).init();
  const cutoverSession = { id: 'cutover-session', log: cutoverLog };
  catchUp(q, cutoverSession, { captureAfter: cutoff });
  const stoppedAtGap = q.cursor(cutoverSession.id).index;
  await q.flush(); catchUp(q, cutoverSession, { captureAfter: cutoff }); await q.flush();
  check('cutover skips imported history, preserves new input and old-inbox new-claim without passing a gap', () => {
    const rows = records.filter(r => r.conversation_id === cutoverSession.id);
    assert.equal(stoppedAtGap, 3); assert.deepEqual(rows.map(r => r.message_id), ['deferred', 'new-after-cutover']);
    assert.equal(rows[0].recorded_at, new Date(cutoff - 100).toISOString()); assert.equal(q.cursor(cutoverSession.id).index, 4);
  });
  q.close(); q = null;
  check('only text blocks are captured, preserving real assistant promise', () => {
    const r = captureMessage(event('assistant/message', msg('a', 'assistant', 'model', [{ type: 'thinking', text: '推理秘密' }, { type: 'text', text: '我答应先备份。' }, { type: 'tool_call', name: 'read', arguments: { secret: 'tool' } }])));
    assert.equal(r.text, '我答应先备份。'); assert.equal(r.recorded_at, '1970-01-01T00:00:00.000Z');
  });
  check('injected user, tool, missing source and interrupted text are rejected', () => {
    assert.equal(captureMessage(event('user/message', msg('ctx', 'user', 'runtime-context', [{ type: 'text', text: 'injected' }]))), null);
    assert.equal(captureMessage(event('user/message', msg('ctx', 'user', 'user', [{ type: 'text', text: 'injected' }], 'notice'))), null);
    assert.equal(captureMessage(event('tool/result', msg('t', 'tool', 'model', [{ type: 'text', text: 'tool' }]))), null);
    assert.equal(captureMessage(event('assistant/message', msg('a', 'assistant', 'model', [{ type: 'text', text: 'partial' }]), { interrupted: true })), null);
  });
  const source = process.env.DSH_SESSION_MODULE, contextSource = process.env.DSH_CONTEXT_MODULE;
  if (!source || !contextSource) console.log('SKIP real DSH integration: set DSH_SESSION_MODULE and DSH_CONTEXT_MODULE (no machine path embedded).');
  else {
    const asURL = s => s.startsWith('file:') ? s : pathToFileURL(path.resolve(s)).href;
    const { SessionStore } = await import(asURL(source)); const { Context } = await import(asURL(contextSource));
    const hooks = new Map(), warnings = [];
    const ctx = { on: (name, fn) => hooks.set(name, fn), logger: { warn: s => warnings.push(s), info: () => {} } };
    controller = await createAdapter(ctx, { enabled: true, sessionModule: asURL(source), baseUrl, token, queueDir: path.join(root, 'dsh'), intervalMs: 50, timeoutMs: 300, maxPending: 2, catchupLimit: 2 });
    check('real SessionStore prepare hook is installed', () => assert(controller.enabled));
    const store = new SessionStore(new Context()), session = store.prepare('real-session');
    const human = (id, text) => msg(id, 'user', 'user', [{ type: 'text', text }]);
    mode = 'outage';
    for (let i = 0; i < 3; i++) session.append('user/message', human('p' + i, '原话' + i), { surfaceOp: 'append' });
    check('capacity leaves an old gap instead of evicting pending', () => { assert.equal(controller.queue.stats().pending, 2); assert(controller.queue.stats().rejected > 0); });
    mode = 'accept';
    await controller.queue.flush();
    session.append('user/message', human('new', '新消息'), { surfaceOp: 'append' });
    await sleep(200);
    const got = records.filter(r => r.conversation_id === 'real-session');
    check('new append cannot overtake old capacity gap', () => assert.deepEqual(got.map(r => r.message_id), ['p0', 'p1', 'p2', 'new']));
    session.append('assistant/message', { turn: 0, step: 0, stream: [], message: msg('promise', 'assistant', 'model', [{ type: 'thinking', text: 'dont-store' }, { type: 'text', text: '我答应先备份。' }]) }, { surfaceOp: 'append' });
    session.append('user/message', msg('injected', 'user', 'runtime-context', [{ type: 'text', text: 'dont-store' }]), { surfaceOp: 'append' });
    session.append('user/message', human('question', '家长会什么时候？'), { surfaceOp: 'append' });
    await sleep(150);
    const payload = { agent: { session }, messages: session.deriveMessages() };
    const before = JSON.stringify(session.log);
    const decision = await hooks.get('agent/pre-step')(payload, async () => ({ keep: true, requestMessages: ['其他插件段'] }));
    check('pre-step calls real HTTP recall after current message ACK', () => { assert(decision.keep); assert.deepEqual(decision.requestMessages, ['其他插件段', '证据：用户明确说周日。']); const r = calls.filter(r => r.path === '/v1/recall').at(-1); assert.equal(r.body.current_message_id, 'question'); assert(identities.has(JSON.stringify(['real-session', 'question', 'user']))); });
    check('request evidence never enters session history', () => assert.equal(JSON.stringify(session.log), before));
    const again = await hooks.get('agent/pre-step')({ ...payload, messages: [] }, async () => decision);
    check('tool-step reuse does not duplicate memory block', () => assert.equal(again.requestMessages.length, 2));
    check('real assistant promise stored without reasoning or injected body', () => { assert(records.some(r => r.message_id === 'promise' && r.text === '我答应先备份。')); assert(!records.some(r => r.text.includes('dont-store'))); });
    const legacy = await hooks.get('agent/pre-step')(payload, async () => ({ requestMessages: ['### 本轮可用原话（旧插件）'] }));
    check('legacy injection is not duplicated', () => assert.equal(legacy.requestMessages.length, 1));
    await controller.stop(); controller = null;
    // Restore a genuine Session from its persisted full event log. No live events
    // or explicit enqueue calls are used to make restored messages arrive.
    const seed = JSON.parse(before);
    const ctx2 = { on: (name, fn) => hooks.set(name, fn), logger: ctx.logger };
    controller = await createAdapter(ctx2, { enabled: true, sessionModule: asURL(source), baseUrl, token, queueDir: path.join(root, 'restored'), intervalMs: 50, timeoutMs: 300 });
    const restoredStore = new SessionStore(new Context()); restoredStore.prepare('restored-session', { seed });
    await sleep(200);
    check('durable full-log restore is caught up without a new user turn', () => assert.equal(records.filter(r => r.conversation_id === 'restored-session').length, 6));
    const s = restoredStore.prepare('timeout-session'); s.append('user/message', human('timeout-q', '回想一下'), { surfaceOp: 'append' }); await sleep(100);
    recallDelay = 1000; const started = Date.now();
    const degraded = await hooks.get('agent/pre-step')({ agent: { session: s }, messages: s.deriveMessages() }, async () => ({ unchanged: true }));
    check('recall failure respects total budget and leaves conversation running', () => { assert(degraded.unchanged); assert(Date.now() - started < 800); assert(warnings.some(x => x.includes('recall_timeout'))); });
    recallDelay = 0; await controller.stop(); controller = null;
  }
} catch (e) { failures++; console.error(e.stack); }
finally { if (controller) await controller.stop(); if (q) q.close(); await new Promise(r => server.close(r)); }
console.log(JSON.stringify({ passed, failed: failures, isolated_directory: root, production_mutations: 0 }));
if (failures) process.exitCode = 1;
