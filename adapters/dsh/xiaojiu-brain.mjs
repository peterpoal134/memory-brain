/** Standalone Brain adapter. No old production paths, memories or modules. */
import path from 'node:path';
import os from 'node:os';
import fs from 'node:fs';
import { pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';
import { DurableQueue } from './durable-queue.mjs';
import { createImportantTool } from './important-tool.mjs';
export const name = 'xiaojiu-brain';
export const inject = [];
const PREPARE = Symbol.for('xiaojiu-brain.prepare.v1');
const INSTALL = Symbol('xiaojiu-brain.session');
const adapters = new WeakMap();
export const getAdapter = ctx => adapters.get(ctx);

export function visibleText(message) {
  const blocks = message?.content;
  if (typeof blocks === 'string') return blocks;
  return Array.isArray(blocks) ? blocks.filter(b => b?.type === 'text' && typeof b.text === 'string').map(b => b.text).join('\n') : '';
}
export function captureMessage(event) {
  if (!event || !['user/message', 'assistant/message'].includes(event.type)) return null;
  const m = event.type === 'user/message' ? event.data : event.data?.message;
  if (!m?.id || !m.source || m.source.form !== undefined) return null;
  if (event.type === 'user/message' && m.role !== 'user' || event.type === 'assistant/message' && m.role !== 'assistant') return null;
  if (m.role === 'user' && m.source.kind !== 'user') return null;
  if (m.role === 'assistant' && (m.source.kind !== 'model' || event.data?.interrupted)) return null;
  if (!['user', 'assistant'].includes(m.role)) return null;
  const text = visibleText(m);
  if (!text) return null;
  return { message_id: String(m.id), role: m.role, text,
    recorded_at: new Date(Number.isFinite(Number(event.time)) ? Number(event.time) : Date.now()).toISOString(),
    metadata: { adapter: name, source_kind: m.source.kind } };
}
function eventIdentity(event) { return `${event?.seq}|${event?.type}|${event?.data?.id || event?.data?.message?.id || ''}`; }

function archivalProjection(events, until) {
  const inbox = { 'next-turn': [], 'next-step': [] }, inserted = new Map(), output = new Map();
  for (let index = 0; index < until; index++) {
    const event = events[index], messages = [];
    if (event.type === 'agent/inbox/spliced') {
      const splice = event.data, target = inbox[splice?.target];
      if (!target || !Number.isInteger(splice.start) || !Array.isArray(splice.inserted)) continue;
      const removed = target.splice(splice.start, splice.removedCount || 0, ...splice.inserted);
      for (const message of splice.inserted) inserted.set(String(message.id), { time: event.time, text: visibleText(message) });
      // A real loop claims durable inbox input before appending user/message.
      // Canceled pending input is not a user utterance admitted by the loop.
      if (splice.outcome !== 'canceled') for (const message of removed) {
        const original = inserted.get(String(message.id));
        const captured = captureMessage({ type: 'user/message', data: message, time: original?.time ?? event.time });
        if (captured) messages.push(captured);
      }
    } else {
      let normalized = event;
      if (event.type === 'user/message') {
        const original = inserted.get(String(event.data?.id));
        if (original && original.text === visibleText(event.data)) normalized = { ...event, time: original.time };
      }
      const captured = captureMessage(normalized); if (captured) messages.push(captured);
    }
    if (messages.length) output.set(index, messages);
  }
  return output;
}

export function catchUp(queue, session, { limit = 200, captureAfter = null } = {}) {
  const conversation = String(session?.id || '');
  if (!conversation || !Array.isArray(session.log)) return 0;
  const events = session.log, cursor = queue.cursor(conversation); let start = 0, scanned = 0;
  if (cursor && events[cursor.index] && eventIdentity(events[cursor.index]) === cursor.identity) start = cursor.index + 1;
  const projection = archivalProjection(events, Math.min(events.length, start + limit));
  for (let index = start; index < events.length && scanned < limit; index++, scanned++) {
    const event = events[index]; let blocked = false;
    // Filter by the persistent output event (claim/visible settlement), not by
    // recorded_at: a pre-cutover inbox item claimed afterwards is new output.
    const olderThanCutover = captureAfter !== null && Number(event.time) <= captureAfter;
    for (const message of olderThanCutover ? [] : projection.get(index) || []) {
      const payload = { conversation_id: conversation, ...message };
      if (!queue.enqueue(payload)) { blocked = true; break; }
    }
    if (blocked) break; // Leave the gap before every newer message.
    queue.saveCursor(conversation, { index, identity: eventIdentity(event) });
  }
  return scanned;
}

function currentUser(payload) {
  const choose = messages => {
    for (const m of [...(messages || [])].reverse()) {
      if (m?.role === 'user' && m.source?.kind === 'user' && m.source.form === undefined && m.id && visibleText(m)) return { id: String(m.id), text: visibleText(m) };
    }
    return null;
  };
  return choose(payload?.messages) || choose(payload?.agent?.session?.deriveMessages?.());
}
function moduleSpecifier(value) {
  if (!value) return '@deepseek-ai/dsh-session';
  if (/^[a-z][a-z0-9+.-]*:/i.test(value) && !/^[a-z]:[\\/]/i.test(value)) return value;
  return path.isAbsolute(value) ? pathToFileURL(value).href : value;
}

export async function createAdapter(ctx, config = {}) {
  const warnings = [], sessions = new Map(), cache = new Map();
  const warn = code => { warnings.push({ code, at: new Date().toISOString() }); if (warnings.length > 50) warnings.shift(); ctx.logger?.warn?.(`xiaojiu-brain: ${code}`); };
  const importantTool = { available: false, state: 'not_started' };
  const profileContext = { supported: null, state: 'not_requested', included: false, omitted_count: 0 };
  const controller = { enabled: false, warnings, queue: null, stop: async () => { config.requestBridge?.dispose(); }, status: () => ({ enabled: controller.enabled, warnings: [...warnings], queue: controller.queue?.stats() || null, important_tool: { ...importantTool }, profile_context: { ...profileContext },
    request_injection: config.requestBridge?.status() || { mode: config.injectionMode || 'unverified_direct', active: controller.enabled } }) };
  adapters.set(ctx, controller);
  if (config.enabled !== true) return controller;
  const baseUrl = String(config.baseUrl || process.env.XIAOJIU_BRAIN_URL || 'http://127.0.0.1:18880').replace(/\/+$/, '');
  const token = String(config.token || process.env.XIAOJIU_BRAIN_TOKEN || '');
  if (!token) { warn('management_token_missing'); return controller; }
  const fetchImpl = config.fetchImpl || globalThis.fetch;
  const timeoutMs = Math.max(100, Number(config.timeoutMs || 3000));
  const configuredLifeTimeout = Number(config.lifeTimeoutMs ?? 800);
  const lifeTimeoutMs = Number.isFinite(configuredLifeTimeout) ? Math.max(1, Math.min(800, configuredLifeTimeout)) : 800;
  const configuredProfileTimeout = Number(config.profileTimeoutMs ?? 800);
  const profileTimeoutMs = Number.isFinite(configuredProfileTimeout) ? Math.max(1, Math.min(800, configuredProfileTimeout)) : 800;
  let registry, member, timer, queue, toolDependency, toolUnregister;
  const importantShutdown = new AbortController();
  const publishCapabilities = () => {
    if (!queue?.dir) return;
    const target = path.join(queue.dir, 'capabilities.json');
    const temporary = path.join(queue.dir, `.capabilities-${process.pid}-${Date.now()}-${Math.random().toString(16).slice(2)}.tmp`);
    try {
      // Diagnostic only: never include configuration, credentials, message IDs
      // or content. A missing/old sidecar is not permission to stop conversation.
      const value = { schema_version: 1, pid: process.pid, observed_at: new Date().toISOString(), important_tool: { ...importantTool } };
      fs.writeFileSync(temporary, JSON.stringify(value), { encoding: 'utf8', flag: 'wx', mode: 0o600 });
      fs.renameSync(temporary, target);
    } catch { warn('important_tool_capability_write_failed'); }
    finally { try { fs.unlinkSync(temporary); } catch { /* already renamed, or unavailable diagnostic storage */ } }
  };
  const setImportantStatus = (state, available = false) => { importantTool.state = state; importantTool.available = available; publishCapabilities(); };
  const originals = new Map();
  try {
    let captureAfter = null;
    if (config.captureAfter !== undefined && config.captureAfter !== null) {
      if (typeof config.captureAfter !== 'string' || !/^\d{4}-\d{2}-\d{2}T/.test(config.captureAfter) || !Number.isFinite(Date.parse(config.captureAfter))) throw new Error('capture_after_invalid_iso_timestamp');
      captureAfter = Date.parse(config.captureAfter);
    }
    const mod = await import(moduleSpecifier(config.sessionModule));
    const proto = mod.SessionStore?.prototype;
    if (!proto || typeof proto.prepare !== 'function') throw new Error('session_module_unsupported');
    if (proto[PREPARE]?.members.size) throw new Error('adapter_already_enabled_in_process');
    queue = new DurableQueue({ dir: config.queueDir || path.join(os.homedir(), '.xiaojiu-brain', 'dsh-queue'),
      baseUrl, token, fetchImpl, maxPending: config.maxPending || 20000, timeoutMs, onAlert: warn });
    queue.init();
    controller.queue = queue;
    const scan = session => {
      if (config.captureEnabled === false) return;
      try { for (let n = 0; n < 5; n++) if (catchUp(queue, session, { limit: config.catchupLimit || 200, captureAfter }) < (config.catchupLimit || 200)) break; }
      catch { warn('session_capture_failed_pending_will_retry'); }
    };
    member = session => {
      if (!session || session[INSTALL] || typeof session.append !== 'function') return;
      if (session.__fishRawQueueInstalled) { warn('legacy_rawqueue_detected_disable_old_plugin'); return; }
      sessions.set(String(session.id), session); originals.set(session, session.append);
      const original = session.append;
      const wrapped = function (...args) { const event = original.apply(this, args); scan(this); return event; };
      session.append = wrapped; session[INSTALL] = wrapped; scan(session);
    };
    if (!proto[PREPARE]) {
      const original = proto.prepare, members = new Set();
      const wrapped = function (...args) { const session = original.apply(this, args); for (const fn of members) { try { fn(session); } catch { /* adapter cannot break host */ } } return session; };
      proto[PREPARE] = { original, wrapped, members }; proto.prepare = wrapped;
    }
    registry = proto[PREPARE]; registry.members.add(member);
    controller.enabled = true;
    const tick = async () => { for (const session of sessions.values()) scan(session); await queue.flush(); queue.sweep(); };
    timer = setInterval(() => { tick().catch(() => warn('queue_tick_failed')); }, Math.max(50, config.intervalMs || 1000)); timer.unref?.();
    const recall = async (payload, next) => {
      const decision = await next();
      if (!controller.enabled || payload?.signal?.aborted) return decision;
      const existing = Array.isArray(decision?.requestMessages) ? decision.requestMessages.filter(x => typeof x === 'string') : [];
      if (existing.some(x => x.includes('### 本轮可用原话') || x.includes('### 想起的相关旧事'))) { warn('legacy_memory_injection_detected'); return decision; }
      let turn;
      try { turn = currentUser(payload); } catch { warn('current_user_unavailable'); return decision; }
      const session = payload?.agent?.session, conversation = String(session?.id || payload?.agent?.id || '');
      if (!turn || !conversation) return decision;
      if (session) { member(session); scan(session); }
      const cacheKey = `${conversation}\0${turn.id}`;
      let blocks = cache.get(cacheKey);
      if (blocks === undefined) {
        // Life state is a separate, bounded request-only channel. A slow recall
        // provider or an unconfirmed archive cannot make its local state vanish.
        const fetchLife = async () => {
          const ac = new AbortController(), abort = () => ac.abort();
          payload?.signal?.addEventListener('abort', abort, { once: true });
          if (payload?.signal?.aborted) abort();
          const timeout = setTimeout(abort, lifeTimeoutMs);
          try {
            const response = await fetchImpl(baseUrl + '/v1/context/life', { method: 'POST', redirect: 'error', signal: ac.signal,
              headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
              body: JSON.stringify({ conversation_id: conversation }) });
            if (response.status === 404) { warn('life_context_legacy_backend'); return ''; }
            const reply = await response.json();
            if (!response.ok || reply?.ok !== true || typeof reply.data?.block !== 'string') throw new Error('life_context_unconfirmed');
            if (reply.data.degraded?.length) warn('life_context_degraded');
            return reply.data.block;
          } catch (e) { warn(e.name === 'AbortError' ? 'life_context_timeout' : 'life_context_unavailable'); return ''; }
          finally { clearTimeout(timeout); payload?.signal?.removeEventListener('abort', abort); }
        };
        const fetchProfile = async () => {
          const ac = new AbortController(), abort = () => ac.abort();
          payload?.signal?.addEventListener('abort', abort, { once: true });
          if (payload?.signal?.aborted) abort();
          const timeout = setTimeout(abort, profileTimeoutMs);
          try {
            const response = await fetchImpl(baseUrl + '/v1/context/profile', { method: 'POST', redirect: 'error', signal: ac.signal,
              headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
              body: JSON.stringify({ conversation_id: conversation, token_limit: 384 }) });
            if (response.status === 404) { Object.assign(profileContext,{supported:false,state:'legacy_backend',included:false}); return ''; }
            const reply = await response.json(), block = reply?.data?.block;
            if (!response.ok || reply?.ok !== true || typeof block !== 'string' || Math.ceil(Buffer.byteLength(block, 'utf8') / 3) > 384) throw new Error('profile_context_unconfirmed');
            Object.assign(profileContext,{supported:true,state:block?'included':'empty',included:Boolean(block),omitted_count:reply.data.trace?.omitted_count||0});
            if (profileContext.omitted_count) warn('profile_context_omitted_budget');
            return block;
          } catch (e) {
            Object.assign(profileContext,{state:e.name==='AbortError'?'timeout':'unavailable',included:false});
            warn(e.name === 'AbortError' ? 'profile_context_timeout' : 'profile_context_unavailable');return '';
          } finally { clearTimeout(timeout);payload?.signal?.removeEventListener('abort',abort); }
        };
        const fetchEvidence = async () => {
          if (config.recallEnabled === false) return { block: '', includesLife: false, includesProfile: false };
          const began = Date.now();
          const current = { conversation_id: conversation, message_id: turn.id, role: 'user' };
          try {
            if (!queue.has(current) || !await queue.drainUntil(current, Math.max(100, Math.floor(timeoutMs * .6)))) {
              warn('current_message_not_confirmed_recall_skipped'); return { block: '', includesLife: false };
            }
          } catch {
            warn('current_message_confirmation_unavailable'); return { block: '', includesLife: false };
          }
          const ac = new AbortController(), abort = () => ac.abort();
          payload?.signal?.addEventListener('abort', abort, { once: true });
          if (payload?.signal?.aborted) abort();
          const timeout = setTimeout(abort, Math.max(1, timeoutMs - (Date.now() - began)));
          try {
            const response = await fetchImpl(baseUrl + '/v1/recall', { method: 'POST', redirect: 'error', signal: ac.signal,
              headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
              body: JSON.stringify({ conversation_id: conversation, query: turn.text, current_message_id: turn.id, token_budget: config.tokenBudget || 1200,
                include_life_context: false, include_profile_context: false }) });
            const reply = await response.json();
            if (!response.ok || reply?.ok !== true || typeof reply.data?.block !== 'string') throw new Error('recall_unconfirmed');
            if (reply.data.degraded?.length) warn('recall_degraded:' + reply.data.degraded.map(x => String(x).replace(/[^a-zA-Z0-9_:.-]/g, '')).join(','));
            return { block: reply.data.block, includesLife: reply.data.trace?.life_context_included,
              includesProfile: reply.data.trace?.personalization_included === true };
          } catch (e) { warn(e.name === 'AbortError' ? 'recall_timeout' : 'recall_unavailable'); return { block: '', includesLife: false }; }
          finally { clearTimeout(timeout); payload?.signal?.removeEventListener('abort', abort); }
        };
        const [life, profile, evidence] = await Promise.all([fetchLife(), fetchProfile(), fetchEvidence()]);
        // Old backends may ignore include_life_context:false. Preserve their
        // entire block; only add the new side channel when exclusion is explicit
        // or evidence is absent. Never guess by removing text from their result.
        // Explicit owner configuration gets a stable independent allowance and
        // precedes factual evidence. It never waits for raw ACK or retrieval.
        blocks = profile && !evidence.includesProfile ? [profile,evidence.block] : [evidence.block];
        if (life && (!evidence.block || evidence.includesLife === false)) blocks.push(life);
        blocks = [...new Set(blocks.filter(block => typeof block === 'string' && block.trim()))];
        if (!payload?.signal?.aborted) {
          cache.set(cacheKey, blocks); if (cache.size > 100) cache.delete(cache.keys().next().value);
        }
      }
      if (payload?.signal?.aborted || !controller.enabled || !blocks.length) return decision;
      const requestMessages = [...existing];
      for (const block of blocks) if (!requestMessages.includes(block)) requestMessages.push(block);
      return { ...(decision || {}), requestMessages };
    };
    const handlePreStep = async (payload, next) => {
      try {
        const decision = await recall(payload, next);
        if (config.requestBridge) {
          if (!controller.enabled || payload?.signal?.aborted) config.requestBridge.clear(payload?.agent);
          else {
            try {
              const staged = config.requestBridge.stage(payload, decision);
              if (!staged.staged && !['step_not_admitted', 'aborted', 'disposed'].includes(staged.reason))
                warn('request_bridge_agent_unsupported');
            }
            catch { config.requestBridge.clear(payload?.agent); warn('request_bridge_agent_unsupported'); }
          }
        }
        return decision;
      } catch (error) { config.requestBridge?.clear(payload?.agent); throw error; }
    };
    ctx.on?.('agent/pre-step', handlePreStep);
    controller.recall = handlePreStep;
    if (config.importantToolEnabled === false) setImportantStatus('disabled');
    else {
      setImportantStatus('waiting_tools');
      const installTool = async scoped => {
        const runtime = scoped.get?.('tools') || scoped.tools;
        if (!runtime || typeof runtime.register !== 'function') { setImportantStatus('unavailable'); warn('important_tool_runtime_unavailable'); return; }
        let toolsModule = config.toolsModule;
        if (!toolsModule && config.sessionModule) {
          const sessionURL = moduleSpecifier(config.sessionModule);
          if (sessionURL.startsWith('file:')) toolsModule = createRequire(sessionURL).resolve('@deepseek-ai/dsh-tools');
        }
        const { defineTool } = await import(moduleSpecifier(toolsModule || '@deepseek-ai/dsh-tools'));
        if (!controller.enabled) return;
        if (typeof defineTool !== 'function') throw new Error('important_tool_module_unsupported');
        const configuredTimeout = Number(config.importantTimeoutMs ?? 5000);
        const importantTimeout = Number.isFinite(configuredTimeout) ? Math.max(500, Math.min(15000, configuredTimeout)) : 5000;
        const unregister = runtime.register(createImportantTool({ defineTool, captureMessage, queue, fetchImpl, baseUrl, token,
          timeoutMs: importantTimeout, enabled: () => controller.enabled, shutdownSignal: importantShutdown.signal,
          bindSession: session => {
            if (!controller.enabled || config.captureEnabled === false || !session?.id || !Array.isArray(session.log)) return false;
            member(session); scan(session); return sessions.get(String(session.id)) === session;
          } }));
        toolUnregister = unregister; setImportantStatus('ready', true);
        return () => { unregister(); if (toolUnregister === unregister) toolUnregister = null;
          setImportantStatus(controller.enabled ? 'waiting_tools' : 'stopped'); };
      };
      if (typeof ctx.inject === 'function') {
        // Optional service dependency: register when the real ToolRuntime arrives,
        // and unregister/rebind if that service is disposed or replaced.
        toolDependency = ctx.inject(['tools'], async scoped => {
          try { return await installTool(scoped); }
          catch { setImportantStatus('unavailable'); warn('important_tool_registration_failed'); }
        });
      } else if (ctx.tools) {
        try { await installTool(ctx); } catch { setImportantStatus('unavailable'); warn('important_tool_registration_failed'); }
      } else { setImportantStatus('unavailable'); warn('important_tool_runtime_unavailable'); }
    }
    controller.stop = async () => {
      controller.enabled = false; config.requestBridge?.dispose(); importantShutdown.abort(); clearInterval(timer); registry.members.delete(member);
      if (toolDependency?.dispose) await toolDependency.dispose();
      if (toolUnregister) { toolUnregister(); toolUnregister = null; }
      setImportantStatus('stopped');
      for (const [session, original] of originals) if (session.append === session[INSTALL]) { session.append = original; delete session[INSTALL]; }
      if (!registry.members.size && proto.prepare === registry.wrapped) { proto.prepare = registry.original; delete proto[PREPARE]; }
      // Do not remove a lock while an HTTP delivery is still in flight.
      const end = Date.now() + timeoutMs + 250;
      while (queue.flushing && Date.now() < end) await new Promise(r => setTimeout(r, 20));
      queue.close(); sessions.clear(); cache.clear();
    };
    ctx.on?.('dispose', () => controller.stop());
    ctx.effect?.(() => () => controller.stop());
    ctx.logger?.info?.('xiaojiu-brain: enabled; visible messages only; request-level recall');
  } catch (e) { config.requestBridge?.dispose(); warn(String(e.message || 'adapter_initialization_failed').replace(/[^a-zA-Z0-9_:.-]/g, '')); queue?.close(); }
  return controller;
}

// Cordis consumes apply's return value as an Effect. A controller object is
// not an Effect; installation owns its disposer through ctx.effect instead.
export async function apply(ctx, config = {}) { await createAdapter(ctx, config); }
