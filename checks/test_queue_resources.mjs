/** Local synthetic queue resource/recovery checks; no model or live service. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { DurableQueue } from '../adapters/dsh/durable-queue.mjs';
import { BoundedDurableIndex, forEachJsonLine, appendJsonLine } from '../adapters/dsh/bounded-durable-index.mjs';

const root = fs.mkdtempSync(path.join(os.tmpdir(), 'memory-brain-queue-resources-'));
const moduleUrl = new URL('../adapters/dsh/durable-queue.mjs', import.meta.url).href;
const hash = value => crypto.createHash('sha256').update(value).digest('hex');
const payload = (id, text = 'Synthetic original.', conversation = 'synthetic-session') => ({
  conversation_id: conversation, message_id: id, role: 'user', text,
  recorded_at: '2026-01-01T00:00:00.000Z', metadata: {},
});
const key = row => JSON.stringify([row.conversation_id, row.message_id, row.role]);
const ack = duplicate => ({ ok: true, status: 200,
  json: async () => ({ ok: true, data: duplicate ? { duplicate: true } : { accepted: true } }) });
const make = (name, options = {}) => new DurableQueue({ dir: path.join(root, name),
  baseUrl: 'http://127.0.0.1:1', fetchImpl: async () => ack(false),
  indexCacheEntries: 8, indexCacheBytes: 1024, ...options }).init();
const checks = [], resources = {};
async function check(name, fn) {
  try { await fn(); checks.push({ name, passed: true }); console.log('PASS ' + name); }
  catch (error) { checks.push({ name, passed: false, error: error.message }); console.error('FAIL ' + name + ': ' + error.message); }
}
function childCrash(name, point) {
  const dir = path.join(root, name);
  const code = `
    import fs from 'node:fs';
    import {DurableQueue} from ${JSON.stringify(moduleUrl)};
    const row=${JSON.stringify(payload('crash-message'))};
    const q=new DurableQueue({dir:${JSON.stringify(dir)},baseUrl:'http://127.0.0.1:1',
      fetchImpl:async()=>({ok:true,status:200,json:async()=>({ok:true,data:{accepted:true}})})}).init();
    const crash=()=>{fs.writeFileSync(${JSON.stringify(path.join(dir, 'crash-boundary'))},${JSON.stringify(point)});process.kill(process.pid,'SIGKILL');};
    if(${JSON.stringify(point)}==='payload')q.seen.set=crash;
    else q.acknowledged.set=crash;
    q.enqueue(row);await q.flush();process.exit(90);
  `;
  const result = spawnSync(process.execPath, ['--input-type=module', '-e', code], { timeout: 15000, windowsHide: true });
  assert(!result.error, 'child process must finish');
  assert.notEqual(result.status, 90, 'fixture must terminate at the intended crash boundary');
  assert.equal(fs.readFileSync(path.join(dir, 'crash-boundary'), 'utf8'), point);
  assert(fs.existsSync(path.join(dir, 'writer.lock')), 'killed writer must leave its lock');
  return dir;
}

await check('immutable disk index bounds entries and bytes, survives hot-cache eviction', () => {
  const dir = path.join(root, 'index');
  const index = new BoundedDurableIndex({ dir, maxCacheEntries: 4, maxCacheBytes: 120 });
  for (let i = 0; i < 80; i++) index.set('key-' + i, 'v'.repeat(30));
  assert(index.cacheStats().entries <= 4); assert(index.cacheStats().bytes <= 120);
  assert.equal(index.get('key-0'), 'v'.repeat(30));
  assert.throws(() => index.set('key-0', 'different'), /durable_index_conflict/);
  index.clearCache(); assert.equal(index.cacheStats().entries, 0);
  assert.equal(new BoundedDurableIndex({ dir }).get('key-0'), 'v'.repeat(30));
});

await check('80 delivered messages retain durable dedup while RAM receipt caches stay bounded', async () => {
  const q = make('bounded');
  try {
    for (let i = 0; i < 80; i++) q.enqueue(payload('message-' + i));
    await q.flush({ budgetMs: 15000 });
    assert.equal(q.stats().pending, 0); assert.equal(q.stats().delivered, 80);
    resources.after80 = q.stats().index_cache;
    for (const cache of Object.values(resources.after80)) {
      assert(cache.entries <= 8); assert(cache.bytes <= 1024);
    }
    q.sweep(Date.now() + 30 * 86400000);
    assert.equal(fs.readdirSync(path.join(q.dir, 'delivered')).length, 0);
    assert.equal(q.enqueue(payload('message-0')).duplicate, true);
    assert.throws(() => q.enqueue(payload('message-0', 'Changed original.')), /body_changed/);
    assert.equal(await q.drainUntil(payload('message-0'), 0), true);
  } finally { q.close(); }
  const reopened = make('bounded');
  try { assert.equal(reopened.enqueue(payload('message-0')).duplicate, true);
    assert.equal(await reopened.drainUntil(payload('message-0'), 0), true); }
  finally { reopened.close(); }
});

await check('legacy UTF-8 journals migrate in chunks without whole-log read and remain byte-identical', async () => {
  const dir = path.join(root, 'legacy'); fs.mkdirSync(dir);
  const conversation = 'Synthetic-' + '\u6e90'.repeat(1000);
  const rows = Array.from({ length: 30 }, (_, i) => payload('legacy-' + i, 'Synthetic.', conversation));
  const seen = rows.map(row => JSON.stringify({ key: key(row), bodyHash: hash(JSON.stringify(row)) })).join('\n') + '\n';
  const acked = rows.map(row => JSON.stringify({ key: key(row) })).join('\n') + '\n';
  fs.writeFileSync(path.join(dir, 'seen.jsonl'), seen); fs.writeFileSync(path.join(dir, 'acked.jsonl'), acked);
  const before = [hash(seen), hash(acked)], originalRead = fs.readFileSync;
  let q;
  try {
    fs.readFileSync = function(file, ...args) {
      if (['seen.jsonl', 'acked.jsonl'].includes(path.basename(String(file)))) throw new Error('whole_log_read_forbidden');
      return originalRead.call(this, file, ...args);
    };
    q = make('legacy');
  } finally { fs.readFileSync = originalRead; }
  try {
    assert.equal(q.enqueue(rows[0]).duplicate, true); assert.equal(await q.drainUntil(rows[29], 0), true);
    assert(q.stats().index_cache.seen.bytes <= 1024);
    assert.deepEqual([hash(fs.readFileSync(path.join(dir, 'seen.jsonl'))), hash(fs.readFileSync(path.join(dir, 'acked.jsonl')))], before);
  } finally { q?.close(); }
  const again = make('legacy'); again.close();
});

await check('legacy conflicting body receipts fail visibly and never replace the first receipt', () => {
  const dir = path.join(root, 'conflicting-legacy'); fs.mkdirSync(dir);
  const row = payload('legacy-conflict'), first = hash(JSON.stringify(row));
  fs.writeFileSync(path.join(dir, 'seen.jsonl'), [first, hash('changed')].map(bodyHash =>
    JSON.stringify({ key: key(row), bodyHash })).join('\n') + '\n');
  assert.throws(() => make('conflicting-legacy'), /durable_index_conflict/);
  assert.equal(fs.existsSync(path.join(dir, 'writer.lock')), false);
  const index = new BoundedDurableIndex({ dir: path.join(dir, 'identity-index') });
  assert.equal(index.get(key(row)), first);
});

await check('malformed legacy journals stop migration instead of silently dropping dedup evidence', () => {
  const dir = path.join(root, 'corrupt-legacy'); fs.mkdirSync(dir);
  fs.writeFileSync(path.join(dir, 'seen.jsonl'), '{broken\n');
  assert.throws(() => make('corrupt-legacy'), /jsonl_record_invalid/);
  assert.equal(fs.existsSync(path.join(dir, 'writer.lock')), false);
  assert.equal(fs.readFileSync(path.join(dir, 'seen.jsonl'), 'utf8'), '{broken\n');
});

await check('payload persisted before identity-index failure is recovered without a second queued original', async () => {
  const q = make('index-failure'), row = payload('safe-before-index');
  const originalSet = q.seen.set.bind(q.seen);
  q.seen.set = () => { throw new Error('synthetic_index_unavailable'); };
  assert.throws(() => q.enqueue(row), /synthetic_index_unavailable/);
  assert.equal(q.stats().pending, 1);
  q.seen.set = originalSet;
  try { assert.equal(q.enqueue(row).duplicate, true); assert.equal(q.stats().pending, 1);
    await q.flush(); assert.equal(q.stats().pending, 0); }
  finally { q.close(); }
});

await check('actual killed process after durable payload recovers pending identity and replays once', async () => {
  childCrash('crash-payload', 'payload');
  const q = make('crash-payload'); let sent = 0;
  q.fetch = async () => { sent++; return ack(false); };
  try { assert.equal(q.stats().pending, 1); assert.equal(q.enqueue(payload('crash-message')).duplicate, true);
    await q.flush(); assert.equal(sent, 1); assert.equal(q.stats().pending, 0); }
  finally { q.close(); }
});

await check('actual killed process after durable ACK preserves confirmation and safely resends duplicate', async () => {
  childCrash('crash-ack', 'ack');
  const q = make('crash-ack'); let sent = 0;
  q.fetch = async () => { sent++; return ack(true); };
  try { assert.equal(q.stats().pending, 1); assert.equal(await q.drainUntil(payload('crash-message'), 0), true);
    await q.flush(); assert.equal(sent, 1); assert.equal(q.stats().pending, 0);
    assert.equal(q.enqueue(payload('crash-message')).duplicate, true); }
  finally { q.close(); }
});

await check('unconfirmed upstream replies keep pending; permanent rejection and pending survive sweep', async () => {
  const q = make('offline', { fetchImpl: async () => ({ ok: true, status: 200, json: async () => ({ ok: true }) }) });
  try {
    const first = payload('unconfirmed'); q.enqueue(first); await q.flush();
    assert.equal(q.stats().pending, 1); assert.equal(await q.drainUntil(first, 0), false);
    q.fetch = async () => ({ ok: false, status: 422, json: async () => ({ ok: false }) });
    await q.flush(); q.enqueue(payload('still-pending'));
    q.sweep(Date.now() + 365 * 86400000);
    assert.equal(q.stats().failed_files, 1); assert.equal(q.stats().pending, 1);
  } finally { q.close(); }
  const recovered = make('offline');
  try { assert.equal(recovered.stats().failed_files, 1); assert.equal(recovered.stats().pending, 1);
    assert.throws(() => recovered.enqueue(payload('unconfirmed', 'changed')), /body_changed/); }
  finally { recovered.close(); }
});

await check('second writer cannot acquire or remove the active queue lock', () => {
  const q = make('writer');
  try { assert.throws(() => make('writer'), /already_in_use/);
    const other = new DurableQueue({ dir: q.dir, baseUrl: q.baseUrl });
    assert.throws(() => other.init(), /already_in_use/); other.close();
    assert(fs.existsSync(path.join(q.dir, 'writer.lock'))); }
  finally { q.close(); }
});

await check('streaming JSONL reader preserves UTF-8 across chunks and enforces a finite line limit', () => {
  const file = path.join(root, 'lines.jsonl'), rows = [{ text: '\u6e90'.repeat(25000) }, { valid: true }];
  fs.writeFileSync(file, rows.map(JSON.stringify).join('\n'));
  const read = []; forEachJsonLine(file, value => read.push(value)); assert.deepEqual(read, rows);
  assert.throws(() => forEachJsonLine(file, () => {}, { maxLineBytes: 100 }), /line_too_large/);
});

await check('index returns isolated JSON values and ignores incomplete uncommitted temp files', () => {
  const dir = path.join(root, 'index-isolation'), index = new BoundedDurableIndex({ dir });
  index.set('object', { nested: { preserved: true } });
  const obtained = index.get('object'); obtained.nested.preserved = false;
  assert.equal(index.get('object').nested.preserved, true);
  const orphan = index.fileFor('not-committed') + '.dead.tmp';
  fs.mkdirSync(path.dirname(orphan), { recursive: true }); fs.writeFileSync(orphan, '{incomplete');
  assert.equal(index.get('not-committed'), undefined);
});

await check('unsupported metadata hard links use complete atomic rename under the writer lock', async () => {
  const originalLink = fs.linkSync;
  resources.renameFallbackCodes = [];
  try {
    for (const code of ['ENOTSUP', 'EPERM', 'EXDEV']) {
      fs.linkSync = function(from, to, ...args) {
        const parts = String(to).split(path.sep);
        if (parts.includes('identity-index') || parts.includes('ack-index'))
          throw Object.assign(new Error('synthetic_link_unavailable'), { code });
        return originalLink.call(this, from, to, ...args);
      };
      const name = 'rename-' + code, row = payload('fallback-original'), q = make(name);
      try {
        assert.equal(q.enqueue(row).accepted, true);
        await q.flush(); assert.equal(q.stats().pending, 0);
        q.sweep(Date.now() + 30 * 86400000);
      } finally { q.close(); }
      const recovered = make(name);
      try {
        assert.equal(recovered.enqueue(row).duplicate, true);
        assert.equal(await recovered.drainUntil(row, 0), true);
        assert.throws(() => recovered.enqueue(payload('fallback-original', 'Conflicting body.')), /body_changed/);
      } finally { recovered.close(); }
      resources.renameFallbackCodes.push(code);
    }
  } finally { fs.linkSync = originalLink; }
});

await check('rename fallback rechecks a newly present conflicting receipt without overwriting it', () => {
  const index = new BoundedDurableIndex({ dir: path.join(root, 'fallback-conflict') });
  const originalLink = fs.linkSync, identity = 'synthetic-conflicting-receipt';
  try {
    fs.linkSync = function(from, to) {
      fs.writeFileSync(to, JSON.stringify({ schema_version: 1, key: identity, value: 'original-body-hash' }));
      throw Object.assign(new Error('synthetic_link_unavailable'), { code: 'ENOTSUP' });
    };
    assert.throws(() => index.set(identity, 'different-body-hash'), /durable_index_conflict/);
  } finally { fs.linkSync = originalLink; }
  index.clearCache(); assert.equal(index.get(identity), 'original-body-hash');
});

await check('torn seen tail is quarantined before migration and durable pending still recovers', () => {
  const q = make('torn-seen'), row = payload('surviving-original'); q.enqueue(row); q.close();
  const journal = path.join(q.dir, 'seen.jsonl');
  const prefix = fs.readFileSync(journal), original = Buffer.concat([prefix, Buffer.from('{"key":"unfinished')]);
  fs.writeFileSync(journal, original);
  fs.rmSync(path.join(q.dir, 'identity-index'), { recursive: true });
  const recovered = make('torn-seen');
  try {
    assert.equal(recovered.enqueue(row).duplicate, true); assert.equal(recovered.stats().pending, 1);
    assert.deepEqual(fs.readFileSync(journal), prefix);
    const evidence = fs.readdirSync(q.dir).filter(n => n.startsWith('seen.jsonl.torn-tail-'));
    assert.equal(evidence.length, 1); assert.deepEqual(fs.readFileSync(path.join(q.dir, evidence[0])), original);
    assert.equal(recovered.stats().last_error, 'queue_journal_torn_tail_quarantined');
  } finally { recovered.close(); }
});

await check('torn ACK tail preserves earlier confirmations and never acknowledges unfinished receipt', async () => {
  const q = make('torn-ack'), confirmed = payload('confirmed'), waiting = payload('waiting');
  q.enqueue(confirmed); await q.flush(); q.enqueue(waiting); q.close();
  const journal = path.join(q.dir, 'acked.jsonl');
  const prefix = fs.readFileSync(journal), original = Buffer.concat([prefix, Buffer.from('{"key":')]);
  fs.writeFileSync(journal, original); fs.rmSync(path.join(q.dir, 'ack-index'), { recursive: true });
  const recovered = make('torn-ack');
  try {
    assert.equal(await recovered.drainUntil(confirmed, 0), true);
    assert.equal(await recovered.drainUntil(waiting, 0), false);
    const evidence = fs.readdirSync(q.dir).filter(n => n.startsWith('acked.jsonl.torn-tail-'));
    assert.equal(evidence.length, 1); assert.deepEqual(fs.readFileSync(path.join(q.dir, evidence[0])), original);
    await recovered.flush(); assert.equal(await recovered.drainUntil(waiting, 0), true);
  } finally { recovered.close(); }
});

await check('repair failure leaves original plus full evidence intact and invokes no record callbacks', () => {
  const journal = path.join(root, 'repair-failure.jsonl'), original = '{"valid":true}\n{"unfinished":';
  fs.writeFileSync(journal, original); const rename = fs.renameSync; let callbacks = 0;
  try {
    fs.renameSync = (from, to) => {
      if (to === journal) throw Object.assign(new Error('synthetic_EIO'), { code: 'EIO' });
      return rename(from, to);
    };
    assert.throws(() => forEachJsonLine(journal, () => callbacks++, { repairTrailing: true }), /tail_repair_failed/);
  } finally { fs.renameSync = rename; }
  assert.equal(callbacks, 0); assert.equal(fs.readFileSync(journal, 'utf8'), original);
  const evidence = fs.readdirSync(root).filter(n => n.startsWith('repair-failure.jsonl.torn-tail-'));
  assert.equal(evidence.length, 1); assert.equal(fs.readFileSync(path.join(root, evidence[0]), 'utf8'), original);
  assert.equal(fs.readdirSync(root).some(n => n.includes('.repair.tmp')), false);
});

await check('complete final JSON without newline remains valid when another receipt is appended', () => {
  const journal = path.join(root, 'complete-tail.jsonl'); fs.writeFileSync(journal, '{"first":true}');
  const before = []; const result = forEachJsonLine(journal, row => before.push(row), { repairTrailing: true });
  assert.equal(result.repaired, false); assert.deepEqual(before, [{ first: true }]);
  appendJsonLine(journal, { second: true });
  const after = []; forEachJsonLine(journal, row => after.push(row));
  assert.deepEqual(after, [{ first: true }, { second: true }]);
});

await check('index and payload temporary files are cleaned on both write and fsync EIO', () => {
  const remainingTemporary = dir => fs.readdirSync(dir, { recursive: true }).filter(n => n.endsWith('.tmp'));
  for (const failure of ['writeFileSync', 'fsyncSync']) {
    const dir = path.join(root, 'index-EIO-' + failure), index = new BoundedDurableIndex({ dir });
    const original = fs[failure];
    try {
      fs[failure] = () => { throw Object.assign(new Error('synthetic_EIO'), { code: 'EIO' }); };
      assert.throws(() => index.set('receipt', 'body-hash'), /synthetic_EIO/);
    } finally { fs[failure] = original; }
    assert.equal(remainingTemporary(dir).length, 0); assert.equal(index.has('receipt'), false);
    const q = make('payload-EIO-' + failure);
    try {
      fs[failure] = () => { throw Object.assign(new Error('synthetic_EIO'), { code: 'EIO' }); };
      assert.throws(() => q.enqueue(payload('retryable-original')), /synthetic_EIO/);
    } finally { fs[failure] = original; }
    try {
      assert.equal(remainingTemporary(q.dir).length, 0); assert.equal(q.stats().pending, 0);
      assert.equal(q.enqueue(payload('retryable-original')).accepted, true);
    } finally { q.close(); }
  }
});

const result = { schema_version: 1, passed: checks.filter(x => x.passed).length,
  failed: checks.filter(x => !x.passed).length, checks, resources,
  source_sha256: Object.fromEntries(['durable-queue.mjs', 'bounded-durable-index.mjs'].map(name =>
    [name, hash(fs.readFileSync(new URL('../adapters/dsh/' + name, import.meta.url)))])),
  boundary: { synthetic_only: true, model_calls: 0, production_mutations: 0,
    process_crashes_executed: 2, os_reboot_executed: false } };
if (process.env.RESOURCE_RESULTS_FILE) {
  const target = path.resolve(process.env.RESOURCE_RESULTS_FILE);
  fs.mkdirSync(path.dirname(target), { recursive: true }); fs.writeFileSync(target, JSON.stringify(result, null, 2) + '\n');
}
console.log(JSON.stringify(result));
if (path.dirname(path.resolve(root)) === path.resolve(os.tmpdir()) && path.basename(root).startsWith('memory-brain-queue-resources-'))
  fs.rmSync(root, { recursive: true, force: true });
if (result.failed) process.exitCode = 1;
