/** Profile-owned context policy; never writes Python/model/host configuration. */
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

export const CONTEXT_DEFAULTS = Object.freeze({
  contextEnabled: true, contextTriggerTurns: 12, contextKeepTurns: 4,
  contextCompactionTimeoutMs: 30000, contextArchiveTimeoutMs: 5000,
  contextMaxInputTokens: null, contextSafetyTokens: 2048,
  contextSummaryEnabled: true, contextSummaryMaxChars: 2000, contextSummaryMaxTokens: 4096,
  contextPressureCooldownMs: 60000, contextToolPruneEnabled: true,
  contextPressureThresholdRatio: 0.8, contextPressureRetainRatio: 0.16, contextPressureHeadroomTokens: 4096,
  contextToolPruneThresholdChars: 4096, contextToolPruneHeadChars: 2048, contextToolPruneTailChars: 512,
  historyEnabled: true, historyAutoEnabled: true, historyTimeoutMs: 2000,
});
const FIELDS = Object.keys(CONTEXT_DEFAULTS), MAX_BYTES = 8192;
// Native ToolResultPruner includes this fixed 39-character omission marker.
const PRUNE_MARKER_CHARS = 39;
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value) &&
  (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null);
function problem(code) { const error = new Error(code); error.code = code; return error; }
function integer(value, low, high) { return Number.isInteger(value) && value >= low && value <= high; }
export function validateContextPatch(patch) {
  if (!object(patch) || Object.keys(patch).some(key => !FIELDS.includes(key))) throw problem('context_settings_fields_invalid');
  const result = {};
  for (const key of Object.keys(patch)) {
    const value = patch[key];
    const valid = ['contextEnabled','contextSummaryEnabled','contextToolPruneEnabled','historyEnabled','historyAutoEnabled'].includes(key) ? typeof value === 'boolean' :
      key === 'contextTriggerTurns' ? integer(value, 2, 200) :
      key === 'contextKeepTurns' ? integer(value, 1, 199) :
      key === 'contextCompactionTimeoutMs' ? integer(value, 1000, 120000) :
      key === 'contextArchiveTimeoutMs' ? integer(value, 100, 30000) :
      key === 'contextMaxInputTokens' ? value === null || integer(value, 256, 4194304) :
      key === 'contextSummaryMaxChars' ? integer(value, 200, 20000) :
      key === 'contextSummaryMaxTokens' ? integer(value, 128, 65536) :
      key === 'contextPressureCooldownMs' ? integer(value, 0, 300000) :
      ['contextPressureThresholdRatio','contextPressureRetainRatio'].includes(key) ? typeof value === 'number' && Number.isFinite(value) && value >= 0.01 && value <= 0.99 :
      key === 'contextPressureHeadroomTokens' ? integer(value, 128, 65536) :
      key === 'contextToolPruneThresholdChars' ? integer(value, 128, 1048576) :
      ['contextToolPruneHeadChars','contextToolPruneTailChars'].includes(key) ? integer(value, 0, 1048575) :
      key === 'historyTimeoutMs' ? integer(value, 100, 10000) :
      integer(value, 256, 65536);
    if (!valid) throw problem('context_settings_value_invalid');
    result[key] = value;
  }
  return result;
}
function policy(value) {
  const result = { ...CONTEXT_DEFAULTS, ...validateContextPatch(value) };
  if (result.contextKeepTurns >= result.contextTriggerTurns) throw problem('context_keep_must_be_below_trigger');
  if (result.contextPressureRetainRatio >= result.contextPressureThresholdRatio) throw problem('context_pressure_retention_invalid');
  if (result.contextToolPruneHeadChars + PRUNE_MARKER_CHARS + result.contextToolPruneTailChars > result.contextToolPruneThresholdChars)
    throw problem('context_tool_prune_retention_invalid');
  return result;
}
function rejectLinks(target) {
  for (let current = target; ; current = path.dirname(current)) {
    try { if (fs.lstatSync(current).isSymbolicLink()) throw problem('context_settings_symlink_refused'); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
    const parent = path.dirname(current); if (parent === current) break;
  }
}
function safe(operation) {
  try { return operation(); }
  catch (error) {
    if (/^context_[a-z_]+$/.test(error?.code || '')) throw error;
    throw problem('context_settings_io_failed');
  }
}
export function createContextSettings({ file, defaults = {} } = {}) {
  if (typeof file !== 'string' || !file || file.includes('\0')) throw problem('context_settings_file_invalid');
  const target = path.resolve(file), baseline = policy(defaults);
  function read() {
    return safe(() => {
      rejectLinks(target);
      let stat; try { stat = fs.lstatSync(target); } catch (error) { if (error.code === 'ENOENT') return { ...baseline }; throw error; }
      if (!stat.isFile() || stat.size > MAX_BYTES) throw problem('context_settings_file_invalid');
      const descriptor = fs.openSync(target, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0));
      let bytes;
      try {
        const opened = fs.fstatSync(descriptor);
        if (!opened.isFile() || opened.size > MAX_BYTES || opened.dev !== stat.dev || opened.ino !== stat.ino)
          throw problem('context_settings_file_changed');
        bytes = Buffer.alloc(MAX_BYTES + 1);
        let length = 0, count;
        while (length < bytes.length && (count = fs.readSync(descriptor, bytes, length, bytes.length - length, null)) > 0) length += count;
        if (length > MAX_BYTES) throw problem('context_settings_file_invalid');
        bytes = bytes.subarray(0, length);
      } finally { fs.closeSync(descriptor); }
      let saved; try { saved = JSON.parse(bytes.toString('utf8')); } catch { throw problem('context_settings_schema_invalid'); }
      if (!object(saved) || Object.keys(saved).length !== 2 || saved.schema_version !== 1 || !Object.hasOwn(saved, 'settings'))
        throw problem('context_settings_schema_invalid');
      return policy({ ...baseline, ...validateContextPatch(saved.settings) });
    });
  }
  function save(patch) {
    return safe(() => {
      const clean = validateContextPatch(patch);
      if (!Object.keys(clean).length) throw problem('context_settings_empty');
      const settings = policy({ ...read(), ...clean }), directory = path.dirname(target);
      rejectLinks(target); fs.mkdirSync(directory, { recursive: true, mode: 0o700 }); rejectLinks(target);
      const temporary = path.join(directory, '.context-management-' + crypto.randomBytes(12).toString('hex') + '.tmp');
      let descriptor;
      try {
        descriptor = fs.openSync(temporary, 'wx', 0o600);
        fs.writeFileSync(descriptor, JSON.stringify({ schema_version: 1, settings }, null, 2) + '\n', 'utf8');
        fs.fsyncSync(descriptor); fs.closeSync(descriptor); descriptor = undefined;
        rejectLinks(target); fs.renameSync(temporary, target);
      } finally {
        if (descriptor !== undefined) fs.closeSync(descriptor);
        try { fs.unlinkSync(temporary); } catch { /* already renamed or unavailable */ }
      }
      return { ...settings };
    });
  }
  function signature(value = read()) { return crypto.createHash('sha256').update(JSON.stringify(policy(value))).digest('hex'); }
  return Object.freeze({ read, save, signature });
}
