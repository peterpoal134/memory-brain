/**
 * Immutable, single-writer key receipts on disk with a bounded RAM hot cache.
 * The caller must hold its queue/collector writer lock. Values are JSON metadata,
 * never Session objects. A different value for an existing key is an error.
 */
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

const sha256 = value => crypto.createHash('sha256').update(value).digest('hex');
const missing = error => error?.code === 'ENOENT';
const linkUnavailable = error => ['EPERM', 'ENOTSUP', 'EOPNOTSUPP', 'EXDEV', 'ENOSYS'].includes(error?.code);

export class BoundedDurableIndex {
  constructor({ dir, maxCacheEntries = 256, maxCacheBytes = 1024 * 1024,
    maxRecordBytes = 1024 * 1024 } = {}) {
    if (!dir) throw new Error('durable_index_directory_required');
    for (const value of [maxCacheEntries, maxCacheBytes, maxRecordBytes]) {
      if (!Number.isSafeInteger(value) || value < 1) throw new Error('durable_index_invalid_limit');
    }
    this.dir = path.resolve(dir); this.maxEntries = maxCacheEntries;
    this.maxBytes = maxCacheBytes; this.maxRecordBytes = maxRecordBytes;
    this.cache = new Map(); this.bytes = 0;
    fs.mkdirSync(this.dir, { recursive: true, mode: 0o700 });
  }
  fileFor(key) {
    if (typeof key !== 'string' || !key || Buffer.byteLength(key) > this.maxRecordBytes / 2)
      throw new Error('durable_index_invalid_key');
    const digest = sha256(key);
    return path.join(this.dir, digest.slice(0, 2), digest.slice(2) + '.json');
  }
  cacheValue(key, valueJson) {
    const previous = this.cache.get(key);
    if (previous) { this.bytes -= previous.bytes; this.cache.delete(key); }
    const bytes = Buffer.byteLength(key) + Buffer.byteLength(valueJson);
    if (bytes > this.maxBytes) return;
    this.cache.set(key, { valueJson, bytes }); this.bytes += bytes;
    while (this.cache.size > this.maxEntries || this.bytes > this.maxBytes) {
      const first = this.cache.keys().next().value;
      this.bytes -= this.cache.get(first).bytes; this.cache.delete(first);
    }
  }
  get(key) {
    const cached = this.cache.get(key);
    if (cached) {
      this.cache.delete(key); this.cache.set(key, cached);
      return JSON.parse(cached.valueJson);
    }
    const file = this.fileFor(key); let data;
    try {
      if (fs.statSync(file).size > this.maxRecordBytes) throw new Error('durable_index_record_too_large');
      data = JSON.parse(fs.readFileSync(file, 'utf8'));
    } catch (error) { if (missing(error)) return undefined; throw error; }
    if (data?.schema_version !== 1 || data.key !== key || !Object.hasOwn(data, 'value'))
      throw new Error('durable_index_record_invalid');
    const valueJson = JSON.stringify(data.value);
    this.cacheValue(key, valueJson);
    return JSON.parse(valueJson);
  }
  has(key) { return this.get(key) !== undefined; }
  set(key, value) {
    const valueJson = JSON.stringify(value);
    if (valueJson === undefined) throw new Error('durable_index_invalid_value');
    const file = this.fileFor(key), previous = this.get(key);
    if (previous !== undefined) {
      if (JSON.stringify(previous) !== valueJson) throw new Error('durable_index_conflict');
      return this;
    }
    const contents = JSON.stringify({ schema_version: 1, key, value: JSON.parse(valueJson) });
    if (Buffer.byteLength(contents) > this.maxRecordBytes) throw new Error('durable_index_record_too_large');
    fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
    const temporary = file + '.' + crypto.randomUUID() + '.tmp';
    try {
      const fd = fs.openSync(temporary, 'wx', 0o600);
      try { fs.writeFileSync(fd, contents); fs.fsyncSync(fd); }
      finally { fs.closeSync(fd); }
      // A complete fsynced file becomes visible atomically, without overwrite.
      // The fallback requires the caller's single-writer lock: after checking
      // again, no legitimate writer can insert a competing file before rename.
      // Both paths publish only the complete same-directory temporary file;
      // never open the final receipt with 'wx' and write an incomplete body.
      try { fs.linkSync(temporary, file); }
      catch (error) {
        if (error.code !== 'EEXIST' && !linkUnavailable(error)) throw error;
        const existing = this.get(key);
        if (existing !== undefined) {
          if (JSON.stringify(existing) !== valueJson) throw new Error('durable_index_conflict');
        } else if (linkUnavailable(error)) fs.renameSync(temporary, file);
        else throw error;
      }
    } finally { try { fs.unlinkSync(temporary); } catch { /* orphan temp is not an index entry */ } }
    this.cacheValue(key, valueJson);
    return this;
  }
  clearCache() { this.cache.clear(); this.bytes = 0; }
  cacheStats() { return { entries: this.cache.size, bytes: this.bytes,
    maxEntries: this.maxEntries, maxBytes: this.maxBytes }; }
}

function scanJsonLines(file, onRecord, { onInvalid, maxLineBytes, allowTornTail = false }) {
  if (!Number.isSafeInteger(maxLineBytes) || maxLineBytes < 1) throw new Error('jsonl_invalid_limit');
  let fd;
  try { fd = fs.openSync(file, 'r'); } catch (error) { if (missing(error)) return { records: 0, missing: true }; throw error; }
  const chunk = Buffer.alloc(64 * 1024); let tail = Buffer.alloc(0), lineNumber = 0, totalBytes = 0, records = 0;
  const consume = (line, final = false) => {
    lineNumber++;
    if (line.length > maxLineBytes) throw new Error('jsonl_line_too_large');
    if (!line.toString('utf8').trim()) return;
    let value;
    try { value = JSON.parse(line.toString('utf8')); }
    catch (error) {
      if (final && allowTornTail) return false;
      onInvalid?.(error, lineNumber); throw new Error('jsonl_record_invalid');
    }
    onRecord?.(value, lineNumber); records++;
    return true;
  };
  try {
    for (;;) {
      const length = fs.readSync(fd, chunk, 0, chunk.length, null);
      if (!length) break;
      totalBytes += length;
      const data = tail.length ? Buffer.concat([tail, chunk.subarray(0, length)]) : chunk.subarray(0, length);
      let start = 0, end;
      while ((end = data.indexOf(10, start)) !== -1) { consume(data.subarray(start, end)); start = end + 1; }
      tail = Buffer.from(data.subarray(start));
      if (tail.length > maxLineBytes) throw new Error('jsonl_line_too_large');
    }
    const torn = tail.length && consume(tail, true) === false;
    return { records, torn: Boolean(torn), prefixBytes: totalBytes - tail.length,
      unterminatedComplete: Boolean(tail.length && !torn), totalBytes };
  } finally { fs.closeSync(fd); }
}

function repairTornTail(file, prefixBytes) {
  const quarantine = file + '.torn-tail-' + crypto.randomUUID() + '.quarantine';
  const temporary = file + '.' + crypto.randomUUID() + '.repair.tmp';
  try {
    // The original remains untouched until a full protected evidence copy and
    // a complete fsynced prefix are available. The queue owns the writer lock.
    fs.copyFileSync(file, quarantine, fs.constants.COPYFILE_EXCL);
    fs.chmodSync(quarantine, 0o600);
    const evidence = fs.openSync(quarantine, 'r+');
    try { fs.fsyncSync(evidence); } finally { fs.closeSync(evidence); }
    const input = fs.openSync(file, 'r'); let output;
    try {
      output = fs.openSync(temporary, 'wx', 0o600);
      const buffer = Buffer.alloc(64 * 1024); let copied = 0;
      while (copied < prefixBytes) {
        const length = fs.readSync(input, buffer, 0, Math.min(buffer.length, prefixBytes - copied), copied);
        if (!length) throw new Error('jsonl_prefix_short_read');
        writeAll(output, buffer.subarray(0, length)); copied += length;
      }
      fs.fsyncSync(output);
    } finally { fs.closeSync(input); if (output !== undefined) fs.closeSync(output); }
    fs.renameSync(temporary, file);
    return quarantine;
  } catch { throw new Error('jsonl_tail_repair_failed'); }
  finally { try { fs.unlinkSync(temporary); } catch { /* original/evidence retained on failure */ } }
}

/** Single-writer append. A complete final JSON without LF is kept, not joined. */
function writeAll(fd, body) {
  let offset = 0;
  while (offset < body.length) {
    const written = fs.writeSync(fd, body, offset, body.length - offset);
    if (!written) throw new Error('jsonl_short_write');
    offset += written;
  }
}
export function appendJsonLine(file, value) {
  const fd = fs.openSync(file, 'a+', 0o600);
  try {
    const size = fs.fstatSync(fd).size;
    if (size) {
      const last = Buffer.alloc(1); fs.readSync(fd, last, 0, 1, size - 1);
      if (last[0] !== 10) writeAll(fd, Buffer.from('\n'));
    }
    writeAll(fd, Buffer.from(JSON.stringify(value) + '\n')); fs.fsyncSync(fd);
  } finally { fs.closeSync(fd); }
}

/**
 * Bounded-memory UTF-8 reader. Recovery is opt-in and requires caller's writer
 * lock. Only an unparsable final record without LF may be quarantined. Middle
 * corruption and immutable receipt conflicts still fail closed. No record
 * callback runs until the entire journal has been checked and repaired.
 */
export function forEachJsonLine(file, onRecord, { onInvalid, maxLineBytes = 1024 * 1024,
  repairTrailing = false, onRepair } = {}) {
  if (!Number.isSafeInteger(maxLineBytes) || maxLineBytes < 1) throw new Error('jsonl_invalid_limit');
  const scan = scanJsonLines(file, null, { onInvalid, maxLineBytes, allowTornTail: repairTrailing });
  let quarantine;
  if (scan.torn) {
    quarantine = repairTornTail(file, scan.prefixBytes);
    onRepair?.({ category: 'jsonl_torn_tail_quarantined', quarantine });
  }
  const result = scanJsonLines(file, onRecord, { onInvalid, maxLineBytes });
  return { ...result, repaired: Boolean(quarantine), quarantine };
}
