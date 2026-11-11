/** Synthetic adapter boundary tests; no live DSH, model or private data. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { createAdapter } from '../adapters/dsh/xiaojiu-brain.mjs';

const root = fs.mkdtempSync(path.join(os.tmpdir(), 'brain-life-adapter-'));
const fixture = path.join(root, 'session-fixture.mjs');
fs.writeFileSync(fixture, `export class SessionStore {
  prepare(id) { return { id, log: [], append(type, data) {
    const event = {type, data, seq:this.log.length, time:Date.now()}; this.log.push(event); return event;
  }, deriveMessages() { return this.log.filter(e=>e.type==='user/message').map(e=>e.data); } }; }
}`);
const sessionModule = pathToFileURL(fixture).href;
const { SessionStore } = await import(sessionModule);
const life = '生活规则模拟（非感受）；精力0.75/无聊0.31/社交0.34/好奇0.40（模拟）。';
const evidence = '证据：合成位置在书架。';
const token = 'synthetic-test-token-do-not-log';
const reply = (status, data) => ({ status, ok: status >= 200 && status < 300, json: async () => data });
const blockReply = (block, included = false) => reply(200, { ok: true, data: { block, degraded: [], trace: { life_context_included: included } } });
const untilAborted = signal => new Promise((resolve, reject) => {
  const abort = () => { const error = new Error('synthetic stalled response'); error.name = 'AbortError'; reject(error); };
  if (signal.aborted) abort(); else signal.addEventListener('abort', abort, { once: true });
});
let passed = 0, failed = 0, serial = 0;
async function test(name, run) {
  try { await run(); passed++; console.log('PASS ' + name); }
  catch (error) { failed++; console.error('FAIL ' + name + ': ' + error.stack); }
}
async function withAdapter(options, run) {
  const calls = [], warnings = [];
  const fetchImpl = async (url, init) => {
    const route = new URL(url).pathname, body = JSON.parse(init.body);
    calls.push({ route, body, at: Date.now() });
    if (route === '/v1/ingest') return options.ingest ? options.ingest(init, body) : reply(200, { ok: true, data: { accepted: true } });
    if (route === '/v1/context/life') return options.life ? options.life(init, body) : blockReply(life, true);
    if (route === '/v1/context/profile') return options.profile ? options.profile(init, body) : reply(404,{ok:false});
    if (route === '/v1/recall') return options.recall ? options.recall(init, body) : blockReply(evidence);
    throw new Error('unexpected_route');
  };
  const hooks = new Map();
  const ctx = { on: (name, fn) => hooks.set(name, fn), logger: { warn: code => warnings.push(code), info() {} } };
  const adapter = await createAdapter(ctx, { enabled: true, token, baseUrl: 'http://127.0.0.1:1', fetchImpl,
    sessionModule, queueDir: path.join(root, String(++serial)), intervalMs: 60000, timeoutMs: 200,
    lifeTimeoutMs: 100, ...(options.config || {}) });
  assert(adapter.enabled);
  const session = new SessionStore().prepare('synthetic-' + serial);
  const append = id => session.append('user/message', { id, role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text: '普通合成提问。' }] });
  append('user-1');
  const request = (decision = {}) => hooks.get('agent/pre-step')({ agent: { session }, messages: session.deriveMessages() }, async () => decision);
  try { await run({ adapter, calls, warnings, session, request, append }); }
  finally { await adapter.stop(); }
}

await test('a real three-second recall timeout retains the independently fetched life state', async () => {
  await withAdapter({ config: { timeoutMs: 3000 }, recall: init => untilAborted(init.signal) }, async ({ request, warnings, calls, session }) => {
    const before = JSON.stringify(session.log), start = Date.now();
    const result = await request({ keep: true, requestMessages: ['另一插件的段落'] });
    assert(result.keep); assert.deepEqual(result.requestMessages, ['另一插件的段落', life]);
    assert(Date.now() - start >= 2800 && Date.now() - start < 4200);
    assert(warnings.some(code => code.endsWith('recall_timeout')));
    assert.equal(calls.find(call => call.route === '/v1/recall').body.include_life_context, false);
    assert.equal(JSON.stringify(session.log), before);
    assert(!JSON.stringify(warnings).includes(token)); assert(!JSON.stringify(warnings).includes('普通合成提问'));
  });
});

await test('failed current-message ACK cannot suppress life or cause an unscoped recall', async () => {
  await withAdapter({ ingest: () => reply(503, { ok: false }) }, async ({ request, calls, warnings, adapter }) => {
    const result = await request();
    assert.deepEqual(result.requestMessages, [life]); assert.equal(adapter.queue.stats().pending, 1);
    assert(!calls.some(call => call.route === '/v1/recall'));
    assert(calls.some(call => call.route === '/v1/context/life'));
    assert(warnings.some(code => code.includes('current_message_not_confirmed')));
  });
});

await test('zero evidence still injects life and a disabled recall route does not disable life', async () => {
  await withAdapter({ recall: () => blockReply('') }, async ({ request }) => assert.deepEqual((await request()).requestMessages, [life]));
  await withAdapter({ config: { recallEnabled: false } }, async ({ request, calls }) => {
    assert.deepEqual((await request()).requestMessages, [life]); assert(!calls.some(call => call.route === '/v1/recall'));
  });
});

await test('disabled life remains empty while ordinary evidence and other decisions survive', async () => {
  await withAdapter({ life: () => blockReply('') }, async ({ request }) => {
    assert.deepEqual(await request({ budget: 7, requestMessages: ['其他段落'] }), { budget: 7, requestMessages: ['其他段落', evidence] });
  });
});

await test('tool steps reuse both channels once per real user message without history writes', async () => {
  await withAdapter({}, async ({ request, calls, append, session }) => {
    const before = JSON.stringify(session.log), first = await request();
    assert.deepEqual(first.requestMessages, [evidence, life]);
    const second = await request(first);
    assert.deepEqual(second.requestMessages, [evidence, life]);
    assert.equal(calls.filter(call => call.route === '/v1/context/life').length, 1);
    assert.equal(calls.filter(call => call.route === '/v1/recall').length, 1);
    assert.equal(JSON.stringify(session.log), before);
    append('user-2'); await request();
    assert.equal(calls.filter(call => call.route === '/v1/context/life').length, 2);
    assert.equal(calls.filter(call => call.route === '/v1/recall').length, 2);
  });
});

await test('an old server 404 preserves its legacy combined block without text surgery', async () => {
  const legacy = evidence + '\n\n' + life;
  await withAdapter({ life: () => reply(404, { ok: false }), recall: () => blockReply(legacy, true) }, async ({ request, warnings }) => {
    assert.deepEqual((await request()).requestMessages, [legacy]);
    assert(warnings.some(code => code.endsWith('life_context_legacy_backend')));
  });
});

await test('a recall that already reports life is preserved once even if the new endpoint also responds', async () => {
  const combined = evidence + '\n\n' + life;
  await withAdapter({ recall: () => blockReply(combined, true) }, async ({ request }) => assert.deepEqual((await request()).requestMessages, [combined]));
});

await test('the life deadline is clamped to at most 800ms and failures expose only a category', async () => {
  await withAdapter({ config: { lifeTimeoutMs: 2000 }, life: init => untilAborted(init.signal) }, async ({ request, warnings }) => {
    const start = Date.now(), result = await request();
    assert.deepEqual(result.requestMessages, [evidence]); assert(Date.now() - start < 1300);
    assert(warnings.some(code => code.endsWith('life_context_timeout')));
    assert(!JSON.stringify(warnings).includes('synthetic stalled response'));
  });
});

await test('an aborted turn does not inject cached state into an abandoned request', async () => {
  await withAdapter({}, async ({ adapter, request, session }) => {
    await request(); const ac = new AbortController(); ac.abort();
    const result = await adapter.recall({ agent: { session }, messages: session.deriveMessages(), signal: ac.signal }, async () => ({ unchanged: true }));
    assert.deepEqual(result, { unchanged: true });
  });
});

await test('profile survives retrieval failure and is request only',async()=>{
  const profile='用户明确设置：用简短中文回答。';
  await withAdapter({life:()=>blockReply(''),recall:init=>untilAborted(init.signal),profile:()=>blockReply(profile)},async({request,session,adapter})=>{
    const before=JSON.stringify(session.log),result=await request();
    assert.deepEqual(result.requestMessages,[profile]);assert.equal(JSON.stringify(session.log),before);
    assert.equal(adapter.status().profile_context.included,true);
  });
});
await test('profile does not depend on raw ACK or recall enablement',async()=>{
  const profile='用户明确设置：不确定时说明。';
  await withAdapter({config:{recallEnabled:false},life:()=>blockReply(''),profile:()=>blockReply(profile)},async({request,calls})=>{
    assert.deepEqual((await request()).requestMessages,[profile]);assert.equal(calls.filter(c=>c.route==='/v1/recall').length,0);
  });
});
await test('profile is frozen within a tool series but refreshed for a new user message',async()=>{
  let version=0;
  await withAdapter({life:()=>blockReply(''),profile:()=>blockReply('合成角色版本 '+(++version))},async({request,calls,append})=>{
    const one=await request(),two=await request();assert.deepEqual(one,two);
    assert.equal(calls.filter(c=>c.route==='/v1/context/profile').length,1);
    assert(calls.find(c=>c.route==='/v1/recall').body.include_profile_context===false);
    append('new-user');assert.notDeepEqual((await request()).requestMessages,one.requestMessages);
    assert.equal(version,2);
  });
});
await test('an unavailable or oversized profile never blocks ordinary evidence',async()=>{
  for(const profile of [init=>untilAborted(init.signal),()=>blockReply('x'.repeat(2000))]){
    await withAdapter({config:{profileTimeoutMs:80},life:()=>blockReply(''),profile},async({request,warnings})=>{
      assert.deepEqual((await request()).requestMessages,[evidence]);assert(warnings.some(w=>w.includes('profile_context_')));
    });
  }
});
console.log(JSON.stringify({ passed, failed, isolated_directory: root, production_mutations: 0 }));
if (failed) process.exitCode = 1;
