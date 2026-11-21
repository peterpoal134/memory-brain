/** Verify the release's DSH request wiring with no network or provider. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const source = path.resolve(process.argv[2] || '.');
const entry = path.join(source, 'adapters/dsh/xiaojiu-brain.mjs');
const { createAdapter } = await import(pathToFileURL(entry).href);
const { CONTEXT_DEFAULTS } = await import(pathToFileURL(path.join(source, 'adapters/dsh/context-settings.mjs')).href);
assert.equal(CONTEXT_DEFAULTS.contextEnabled, true);
assert.equal(CONTEXT_DEFAULTS.contextTriggerTurns, 12);
assert.equal(CONTEXT_DEFAULTS.contextKeepTurns, 4);

const fixtureSource = `
class Session {
  constructor(id) { this.id = id; this.log = []; }
  append(type, data) {
    const seq = this.log.length + 1;
    const event = { type, data, seq, time: Date.UTC(2026, 0, 1) + seq };
    this.log.push(event);
    return event;
  }
}
export class SessionStore { prepare(id) { return new Session(id); } }
`;
const sessionModule = 'data:text/javascript,' + encodeURIComponent(fixtureSource);
const { SessionStore } = await import(sessionModule);
const tempRoot = fs.realpathSync(os.tmpdir());
const root = fs.mkdtempSync(path.join(tempRoot, 'memory-brain-recall-test-'));
const calls = [], archived = new Map();
const hooks = new Map();
const ctx = { on: (name, handler) => { hooks.set(name, handler); }, logger: { warn() {} } };
const evidence = '合成记忆：归档存放在加密盘。';
const literalText = '加密盘检查通过，校验码一致。';
const matchingHistory = {
  seq: 1, message_id: 'literal-match', source_message_id: 1, role: 'user',
  recorded_at_iso: '2026-01-01T00:00:00.001Z', start: 0, end: [...literalText].length,
  text: literalText, truncated: false,
};
const respond = (data, status = 200) => new Response(JSON.stringify(data), { status });
const fetchImpl = async (input, init) => {
  const endpoint = new URL(input).pathname, request = JSON.parse(init.body);
  calls.push({ endpoint, request });
  if (endpoint === '/v1/ingest') {
    archived.set(request.message_id, request.text);
    return respond({ ok: true, data: { accepted: true, id: archived.size } });
  }
  if (endpoint === '/v1/recall') {
    return respond({ ok: true, data: {
      block: request.current_message_id === 'topic' ? evidence : '', evidence: [], trace: {},
    } });
  }
  if (endpoint === '/v1/history/search') {
    return respond({ ok: true, data: {
      scope: 'current_conversation', state: 'matched',
      results: [{ match: matchingHistory, previous: null, next: null }],
    } });
  }
  if (endpoint.startsWith('/v1/context/')) return respond({ ok: false }, 404);
  throw new Error(`Unexpected synthetic endpoint ${endpoint}`);
};

let controller;
const originalFetch = globalThis.fetch;
globalThis.fetch = async () => { throw new Error('Network is disabled in this fixture'); };
try {
  controller = await createAdapter(ctx, {
    enabled: true, sessionModule, fetchImpl, token: 'synthetic-token', baseUrl: 'http://127.0.0.1:1',
    queueDir: path.join(root, 'queue'), contextEnabled: false, importantToolEnabled: false,
    chatUsageEnabled: false, timeoutMs: 1000,
  });
  assert.equal(controller.enabled, true, JSON.stringify(controller.status().warnings));
  const session = new SessionStore().prepare('synthetic-session');
  const message = (id, text) => ({ id, role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text }] });
  session.append('user/message', message('literal-match', literalText));
  session.append('user/message', message('other-topic', '茶壶的把手有些松。'));
  const original = { kind: 'enter', requestMessages: ['既有独立请求材料。'] };
  async function request(id, text, turn) {
    const current = message(id, text);
    session.append('user/message', current);
    const payload = { agent: { session }, messages: [current], turn, step: 1 };
    return hooks.get('agent/pre-step')(payload, async () => original);
  }
  const prompt = await request('noise', '嗯嗯，对的对的。', 1);
  assert.strictEqual(prompt, original);
  assert.equal(archived.get('noise'), '嗯嗯，对的对的。');
  assert.equal(calls.find(({ endpoint }) => endpoint === '/v1/recall').request.automatic, true);
  assert.equal(calls.some(({ endpoint }) => endpoint === '/v1/history/search'), false);

  const relevant = await request('topic', '离线备份存在哪里？', 2);
  assert.deepEqual(relevant.requestMessages, [...original.requestMessages, evidence]);
  assert.ok(!session.log.some(event => JSON.stringify(event).includes(evidence)));

  const literal = await request('literal', '你之前说过「加密盘」的原话是什么？', 3);
  const historyRequest = calls.find(({ endpoint }) => endpoint === '/v1/history/search')?.request;
  assert.ok(historyRequest, 'Automatic literal fallback must reach the kernel');
  assert.equal(historyRequest.automatic, true);
  assert.deepEqual(historyRequest.sources.map(row => row.message_id), ['literal-match']);
  assert.ok(literal.requestMessages.at(-1).includes(literalText));
  assert.ok(!literal.requestMessages.at(-1).includes('茶壶'));
  console.log('PASS automatic marker, empty request, raw archive, isolated positive injection, literal-history match only, and default 12/4');
} finally {
  try { await controller?.stop(); }
  finally {
    globalThis.fetch = originalFetch;
    assert.equal(path.dirname(fs.realpathSync(root)), tempRoot, 'Fixture cleanup target moved');
    fs.rmSync(root, { recursive: true, force: true });
  }
}
