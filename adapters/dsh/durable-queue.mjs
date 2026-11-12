/**
 * Durable at-least-once queue for the standalone Xiaojiu Brain protocol.
 * Design provenance: the previously verified fish-rawqueue queue's invariants
 * (persist before seen; strict ACK; sequence recovery; no pending eviction).
 * This implementation is new and does not import that plugin or its data.
 */
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { BoundedDurableIndex, forEachJsonLine, appendJsonLine } from './bounded-durable-index.mjs';

const hash = value => crypto.createHash('sha256').update(value).digest('hex');
const keyOf = r => JSON.stringify([r.conversation_id, r.message_id, r.role]);
const json = file => JSON.parse(fs.readFileSync(file, 'utf8'));
function persist(file, value, exclusive = false) {
  const tmp = `${file}.${crypto.randomUUID()}.tmp`;
  try {
    const fd = fs.openSync(tmp, 'wx', 0o600);
    try { fs.writeFileSync(fd, JSON.stringify(value)); fs.fsyncSync(fd); }
    finally { fs.closeSync(fd); }
    if (exclusive) {
      try { fs.linkSync(tmp, file); }
      catch (e) {
        if (!['EPERM', 'ENOTSUP', 'EOPNOTSUPP', 'EXDEV'].includes(e.code)) throw e;
        const fd = fs.openSync(file, 'wx', 0o600);
        try { fs.writeFileSync(fd, JSON.stringify(value)); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
      }
    }
    else fs.renameSync(tmp, file);
  } finally { try { fs.unlinkSync(tmp); } catch { /* already renamed */ } }
}

export class DurableQueue {
  constructor({ dir, baseUrl, token = '', fetchImpl = globalThis.fetch, maxPending = 20000,
    timeoutMs = 3000, deliveredDays = 7, onAlert = () => {}, indexCacheEntries = 256,
    indexCacheBytes = 1024 * 1024 } = {}) {
    if (!dir || !baseUrl) throw new Error('queue_dir_and_backend_required');
    this.dir = path.resolve(dir); this.baseUrl = String(baseUrl).replace(/\/+$/, '');
    this.token = token; this.fetch = fetchImpl; this.maxPending = Math.max(1, maxPending);
    this.timeoutMs = Math.max(100, timeoutMs); this.deliveredDays = deliveredDays;
    this.onAlert = onAlert; this.indexOptions = { maxCacheEntries: indexCacheEntries, maxCacheBytes: indexCacheBytes };
    this.seen = null; this.acknowledged = null; this.indexRecoveryRequired = false;
    this.flushing = false; this.closed = false;
    this.state = { delivered: 0, failed: 0, alerts: 0, rejected: 0, last_error: null };
  }
  alert(code) {
    this.state.alerts++; this.state.last_error = code;
    this.alertTimes ||= new Map(); const now = Date.now();
    if (now - (this.alertTimes.get(code) || 0) >= 60000) {
      this.alertTimes.set(code, now);
      try { fs.appendFileSync(path.join(this.dir, 'alerts.jsonl'), JSON.stringify({ at: new Date().toISOString(), code }) + '\n', { mode: 0o600 }); } catch { /* caller also sees alert */ }
      try { this.onAlert(code); } catch { /* reporting must not affect chat */ }
    }
  }
  init() {
    fs.mkdirSync(this.dir, { recursive: true, mode: 0o700 });
    this.lock = path.join(this.dir, 'writer.lock');
    if (fs.existsSync(this.lock)) {
      let alive = true;
      try { const p = json(this.lock); try { process.kill(p.pid, 0); } catch (e) { alive = e.code !== 'ESRCH'; } }
      catch { throw new Error('queue_lock_unreadable'); }
      if (alive) throw new Error('queue_already_in_use');
      fs.unlinkSync(this.lock);
    }
    const fd = fs.openSync(this.lock, 'wx', 0o600);
    this.lockNonce = crypto.randomUUID();
    try { fs.writeFileSync(fd, JSON.stringify({ pid: process.pid, nonce: this.lockNonce })); fs.fsyncSync(fd); }
    finally { fs.closeSync(fd); }
    try {
    for (const name of ['pending', 'delivered', 'failed', 'cursors']) fs.mkdirSync(path.join(this.dir, name), { recursive: true, mode: 0o700 });
    const manifest = path.join(this.dir, 'backend.json');
    if (fs.existsSync(manifest) && json(manifest).baseUrl !== this.baseUrl) {
      this.close(); throw new Error('queue_backend_changed_use_new_directory');
    }
    if (!fs.existsSync(manifest)) persist(manifest, { protocol: 1, baseUrl: this.baseUrl });
    this.seen = new BoundedDurableIndex({ dir: path.join(this.dir, 'identity-index'), ...this.indexOptions });
    this.acknowledged = new BoundedDurableIndex({ dir: path.join(this.dir, 'ack-index'), ...this.indexOptions });
    this.recoverIndexes();
    return this;
    } catch (error) { this.alert('queue_index_initialization_failed'); this.close(); throw error; }
  }
  recoverIndexes() {
    // Keep both legacy journals intact for rollback. Stream them so startup RAM
    // is bounded, including queues created before disk receipts were available.
    const recovery = { repairTrailing: true, onRepair: () => this.alert('queue_journal_torn_tail_quarantined') };
    forEachJsonLine(path.join(this.dir, 'seen.jsonl'), r => {
      if (typeof r?.key !== 'string' || !r.key || !/^[a-f0-9]{64}$/.test(r.bodyHash))
        throw new Error('seen_record_corrupt');
      this.seen.set(r.key, r.bodyHash);
    }, recovery);
    forEachJsonLine(path.join(this.dir, 'acked.jsonl'), r => {
      if (typeof r?.key !== 'string' || !r.key) throw new Error('ack_record_corrupt');
      this.acknowledged.set(r.key, true);
    }, recovery);
    let maximum = 0;
    for (const folder of ['pending', 'delivered', 'failed']) for (const name of fs.readdirSync(path.join(this.dir, folder))) {
      if (!/^\d{16}-[a-f0-9]+\.json$/.test(name)) continue;
      maximum = Math.max(maximum, Number(name.slice(0, 16)));
      let r;
      try {
        r = json(path.join(this.dir, folder, name));
        if (!r.payload || r.key !== keyOf(r.payload) || r.bodyHash !== hash(JSON.stringify(r.payload))) throw new Error();
      } catch { this.alert('queue_record_corrupt_at_init'); continue; /* flush retains broken pending in failed */ }
      this.seen.set(r.key, r.bodyHash);
      if (folder === 'delivered') this.acknowledged.set(r.key, true);
    }
    this.nextSeq = Math.max(this.nextSeq || 1, maximum + 1); this.indexRecoveryRequired = false;
  }
  pendingFiles() { return fs.readdirSync(path.join(this.dir, 'pending')).filter(n => /^\d{16}-[a-f0-9]+\.json$/.test(n)).sort(); }
  stats() { return { ...this.state, pending: this.pendingFiles().length,
    failed_files: fs.readdirSync(path.join(this.dir, 'failed')).filter(x => x.endsWith('.json')).length,
    index_cache: { seen: this.seen?.cacheStats(), acknowledged: this.acknowledged?.cacheStats() } }; }
  enqueue(payload) {
    if (this.closed) throw new Error('queue_closed');
    if (this.indexRecoveryRequired) this.recoverIndexes();
    const key = keyOf(payload), bodyHash = hash(JSON.stringify(payload));
    const known = this.seen.get(key);
    if (known !== undefined) {
      if (known !== bodyHash) { this.alert('message_identity_body_changed'); throw new Error('message_identity_body_changed'); }
      return { duplicate: true };
    }
    if (this.pendingFiles().length >= this.maxPending) { this.state.rejected++; this.alert('queue_capacity'); return null; }
    let name;
    for (;;) {
      name = `${String(this.nextSeq++).padStart(16, '0')}-${hash(key).slice(0, 24)}.json`;
      try { persist(path.join(this.dir, 'pending', name), { key, bodyHash, payload }, true); break; }
      catch (e) { if (e.code !== 'EEXIST') { this.alert('queue_disk_write'); throw e; } }
    }
    // The payload is durable before the advisory seen log or cursor can advance.
    try { this.seen.set(key, bodyHash); }
    catch (error) { this.indexRecoveryRequired = true; this.alert('seen_index_write_failed_payload_safe'); throw error; }
    try {
      appendJsonLine(path.join(this.dir, 'seen.jsonl'), { key, bodyHash });
    } catch { this.indexRecoveryRequired = true; this.alert('seen_write_failed_payload_safe'); }
    return { accepted: true, name };
  }
  cursor(conversation) { try { return json(path.join(this.dir, 'cursors', hash(conversation) + '.json')); } catch { return null; } }
  saveCursor(conversation, cursor) { persist(path.join(this.dir, 'cursors', hash(conversation) + '.json'), cursor); }
  has(payload) { if (this.indexRecoveryRequired) this.recoverIndexes(); return this.seen.has(keyOf(payload)); }
  async drainUntil(payload, budgetMs = this.timeoutMs) {
    if (this.indexRecoveryRequired) this.recoverIndexes();
    const key = keyOf(payload), deadline = Date.now() + Math.max(0, budgetMs);
    if (!this.seen.has(key)) return false;
    while (!this.acknowledged.has(key) && Date.now() < deadline && !this.closed) {
      await this.flush({ budgetMs: deadline - Date.now() });
      if (!this.acknowledged.has(key)) await new Promise(r => setTimeout(r, Math.min(20, Math.max(0, deadline - Date.now()))));
    }
    return this.acknowledged.has(key);
  }
  async flush({ budgetMs = this.timeoutMs, maxRecords = 100 } = {}) {
    if (this.flushing || this.closed) return;
    this.flushing = true;
    try {
      if (this.indexRecoveryRequired) this.recoverIndexes();
      const deadline = Date.now() + budgetMs;
      for (const name of this.pendingFiles().slice(0, maxRecords)) {
        if (Date.now() >= deadline) break;
        const from = path.join(this.dir, 'pending', name); let record;
        try { record = json(from); if (!record.payload || record.key !== keyOf(record.payload) || record.bodyHash !== hash(JSON.stringify(record.payload))) throw new Error(); }
        catch { fs.renameSync(from, path.join(this.dir, 'failed', name)); this.state.failed++; this.alert('queue_record_corrupt'); continue; }
        const ac = new AbortController(), timer = setTimeout(() => ac.abort(), Math.min(this.timeoutMs, Math.max(1, deadline - Date.now())));
        try {
          const response = await this.fetch(this.baseUrl + '/v1/ingest', { method: 'POST', redirect: 'error',
            headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${this.token}` },
            body: JSON.stringify(record.payload), signal: ac.signal });
          const reply = await response.json().catch(() => null);
          if (response.ok && reply?.ok === true && (reply.data?.accepted === true || reply.data?.duplicate === true)) {
            try {
              appendJsonLine(path.join(this.dir, 'acked.jsonl'), { key: record.key });
              this.acknowledged.set(record.key, true);
              fs.renameSync(from, path.join(this.dir, 'delivered', name)); this.state.delivered++; this.state.last_error = null;
            } catch {
              this.indexRecoveryRequired = true; this.alert('queue_ack_persist_failed_payload_safe'); break;
            }
          } else if ([400, 409, 422].includes(response.status)) {
            fs.renameSync(from, path.join(this.dir, 'failed', name)); this.state.failed++; this.alert('backend_rejected_' + response.status);
          } else { this.alert('backend_unconfirmed_' + response.status); break; }
        } catch (e) { this.alert(e.name === 'AbortError' ? 'backend_timeout' : 'backend_unreachable'); break; }
        finally { clearTimeout(timer); }
      }
    } finally { this.flushing = false; }
  }
  sweep(now = Date.now()) {
    for (const name of fs.readdirSync(path.join(this.dir, 'delivered'))) {
      const p = path.join(this.dir, 'delivered', name);
      if (name.endsWith('.json') && now - fs.statSync(p).mtimeMs > this.deliveredDays * 86400000) fs.unlinkSync(p);
    }
    // pending and failed are never evicted by age/capacity. Failed needs review.
  }
  close() {
    this.closed = true;
    this.seen?.clearCache(); this.acknowledged?.clearCache();
    try { const lock = json(this.lock); if (this.lockNonce && lock.pid === process.pid && lock.nonce === this.lockNonce) fs.unlinkSync(this.lock); } catch { /* no owned lock */ }
  }
}
