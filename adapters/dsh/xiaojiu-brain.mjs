/** Standalone Brain adapter. No old production paths, memories or modules. */
import path from 'node:path';
import os from 'node:os';
import fs from 'node:fs';
import crypto from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';
import { DurableQueue } from './durable-queue.mjs';
import { createImportantTool } from './important-tool.mjs';
import { ChatUsageCollector } from './chat-usage.mjs';
import { createContextManager } from './context-manager.mjs';
import { createSessionHistory } from './session-history.mjs';
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
  if (start >= events.length) return 0;
  const projection = archivalProjection(events, Math.min(events.length, start + limit));
  for (let index = start; index < events.length && scanned < limit; index++, scanned++) {
    const event = events[index]; let blocked = false;
    // Filter by the persistent output event (claim/visible settlement), not by
    // recorded_at: a pre-cutover inbox item claimed afterwards is new output.
    const olderThanCutover = captureAfter !== null && Number(event.time) <= captureAfter;
    for (const message of olderThanCutover ? [] : projection.get(index) || []) {
      const payload = { conversation_id: conversation, ...message };
      if (!queue.enqueue(payload, { index, seq: event.seq })) { blocked = true; break; }
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
  const attachments = new Set(), listeners = [];
  let originals = new WeakMap(), stopPromise;
  const liveSessions = () => {
    const result = [];
    for (const ref of attachments) { const session = ref.deref(); if (session) result.push(session); else attachments.delete(ref); }
    for (const [id, ref] of sessions) if (!ref.deref()) sessions.delete(id);
    return result;
  };
  const listen = (event, handler) => { const off = ctx.on?.(event, handler); if (typeof off === 'function') listeners.push(off); };
  const warn = code => { warnings.push({ code, at: new Date().toISOString() }); if (warnings.length > 50) warnings.shift(); ctx.logger?.warn?.(`xiaojiu-brain: ${code}`); };
  const importantTool = { available: false, state: 'not_started' };
  const profileContext = { supported: null, state: 'not_requested', included: false, omitted_count: 0 };
  let contextManagement=null, sessionHistory=null;
  const stopSupport = async () => { try { await config.compactionSupport?.stop(); } catch { warn('context_support_stop_failed'); } };
  const controller = { enabled: false, warnings, queue: null, chatUsage: null, stop: async () => { sessionHistory?.stop();contextManagement?.stop();config.requestBudget?.stop();config.requestBridge?.dispose();await stopSupport(); }, status: () => ({ enabled: controller.enabled, warnings: [...warnings], queue: controller.queue?.stats() || null, capture: { attached_sessions: liveSessions().length, live_discovery: true }, chat_usage: controller.chatUsage?.status() || { enabled:false }, important_tool: { ...importantTool }, profile_context: { ...profileContext },
    session_history:sessionHistory?.status()||{enabled:false,available:false,tool_available:false,state:'not_started'},
    context_management:{compression:contextManagement?.status()||{state:'not_started'},budget:config.requestBudget?.status()||{state:'unavailable',active:false},support:config.compactionSupport?.status()||{state:'unavailable'}},
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
  let registry, member, timer, queue, toolDependency, toolUnregister, chatUsage, proto;
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
  try {
    let captureAfter = null;
    if (config.captureAfter !== undefined && config.captureAfter !== null) {
      if (typeof config.captureAfter !== 'string' || !/^\d{4}-\d{2}-\d{2}T/.test(config.captureAfter) || !Number.isFinite(Date.parse(config.captureAfter))) throw new Error('capture_after_invalid_iso_timestamp');
      captureAfter = Date.parse(config.captureAfter);
    }
    const mod = await import(moduleSpecifier(config.sessionModule));
    proto = mod.SessionStore?.prototype;
    if (!proto || typeof proto.prepare !== 'function') throw new Error('session_module_unsupported');
    if (proto[PREPARE]?.members.size) throw new Error('adapter_already_enabled_in_process');
    queue = new DurableQueue({ dir: config.queueDir || path.join(os.homedir(), '.xiaojiu-brain', 'dsh-queue'),
      baseUrl, token, fetchImpl, maxPending: config.maxPending || 20000, timeoutMs, onAlert: warn });
    queue.init();
    controller.queue = queue;
    // The queue's exclusive writer lock also owns this recovery outbox. It is
    // disk-backed rather than retaining disposed Session objects in memory.
    const recoveryDir = path.join(queue.dir, 'session-recovery');
    fs.mkdirSync(recoveryDir, { recursive: true, mode: 0o700 });
    const digest = value => crypto.createHash('sha256').update(value).digest('hex');
    const recoveryFiles = conversation => fs.readdirSync(recoveryDir).filter(file =>
      /^[a-f0-9]{64}-\d{16}-[a-f0-9]{64}\.json$/.test(file) && (!conversation || file.startsWith(digest(conversation) + '-'))).sort();
    const spool = (payload, position) => {
      if (queue.closed) throw new Error('session_recovery_queue_closed');
      if (queue.has(payload)) return queue.enqueue(payload); // Verify immutable body too.
      const key = JSON.stringify([payload.conversation_id, payload.message_id, payload.role]);
      const order = Number.isSafeInteger(position.seq) && position.seq >= 0 ? position.seq : position.index;
      const file = path.join(recoveryDir, `${digest(payload.conversation_id)}-${String(order).padStart(16, '0')}-${digest(key)}.json`);
      const row = { payload, bodyHash: digest(JSON.stringify(payload)) };
      if (fs.existsSync(file)) {
        if (fs.readFileSync(file, 'utf8') !== JSON.stringify(row)) throw new Error('session_recovery_identity_conflict');
        return true;
      }
      const tmp = `${file}.${process.pid}-${crypto.randomBytes(8).toString('hex')}.tmp`;
      let fd;
      try {
        fd = fs.openSync(tmp, 'wx', 0o600); fs.writeFileSync(fd, JSON.stringify(row)); fs.fsyncSync(fd); fs.closeSync(fd); fd = undefined;
        try { fs.linkSync(tmp, file); } // Publish only a complete fsynced file.
        catch (error) {
          if (!['EPERM', 'ENOTSUP', 'EOPNOTSUPP', 'EXDEV', 'ENOSYS'].includes(error.code)) throw error;
          // Same-directory rename is safe here only under our queue writer lock.
          if (fs.existsSync(file)) { if (fs.readFileSync(file, 'utf8') !== JSON.stringify(row)) throw new Error('session_recovery_identity_conflict'); }
          else fs.renameSync(tmp, file);
        }
      } finally { if (fd !== undefined) fs.closeSync(fd); try { fs.unlinkSync(tmp); } catch {} }
      return true;
    };
    const drainRecovery = (conversation, limit = 50) => {
      const files = recoveryFiles(conversation);
      for (const name of files.slice(0, limit)) {
        const file = path.join(recoveryDir, name), row = JSON.parse(fs.readFileSync(file, 'utf8'));
        if (!row.payload || row.bodyHash !== digest(JSON.stringify(row.payload))) throw new Error('session_recovery_invalid');
        const key = JSON.stringify([row.payload.conversation_id, row.payload.message_id, row.payload.role]);
        if (queue.acknowledged.has(key)) { fs.unlinkSync(file); continue; }
        // An unacknowledged recovery receipt survives even after main enqueue.
        if (!queue.enqueue(row.payload)) return false;
      }
      return files.length <= limit;
    };
    const preserve = session => {
      if (config.captureEnabled === false || !Array.isArray(session?.log)) return;
      try {
        catchUp({ cursor: id => queue.cursor(id), saveCursor: (id, cursor) => queue.saveCursor(id, cursor), enqueue: spool },
          session, { limit: session.log.length + 1, captureAfter });
      } catch { warn('session_release_capture_not_durable_host_replay_required'); }
    };
    const release = session => {
      const saved = originals.get(session);
      if (!saved) { chatUsage?.detach(session); return; }
      preserve(session); chatUsage?.detach(session);
      // Never restore another same-ID instance or overwrite a later wrapper.
      if (session.append === saved.wrapped) session.append = saved.original;
      if (session[INSTALL] === saved.wrapped) delete session[INSTALL];
      attachments.delete(saved.ref); originals.delete(session);
      if (sessions.get(String(session.id))?.deref() === session) sessions.delete(String(session.id));
      for (const key of cache.keys()) if (key.startsWith(String(session.id) + '\0')) cache.delete(key);
    };
    if (config.chatUsageEnabled !== false) {
      chatUsage = new ChatUsageCollector({ dir:path.join(queue.dir,'chat-usage'),baseUrl,token,fetchImpl,
        timeoutMs:config.chatUsageTimeoutMs || 3000,intervalMs:config.chatUsageIntervalMs || 1000,onWarning:warn });
      try { chatUsage.init(); controller.chatUsage = chatUsage; }
      catch { chatUsage.close(); chatUsage = null; warn('chat_usage_unavailable'); }
      if (chatUsage) {
        listen('session/event', session => { if (controller.enabled) chatUsage.attach(session); });
        listen('agent/assistant-stream', ({agent,frame}) => { if (controller.enabled) chatUsage.observeFrame(agent,frame); });
      }
    }
    const scan = session => {
      if (config.captureEnabled === false) return;
      try {
        if (!drainRecovery(String(session.id))) return;
        for (let n = 0; n < 5; n++) if (catchUp(queue, session, { limit: config.catchupLimit || 200, captureAfter }) < (config.catchupLimit || 200)) break;
      }
      catch { warn('session_capture_failed_pending_will_retry'); }
    };
    const ensureArchived = async (session, selectedSeqs, signal) => {
      if(!controller.enabled||config.captureEnabled===false||signal?.aborted||!Array.isArray(session?.log))return false;
      const deadline=Date.now()+Number(config.contextArchiveTimeoutMs??5000);
      try{
        const events=[...session.log],selected=new Set(selectedSeqs),conversation=String(session.id||'');
        if(!conversation)return false;
        if(!drainRecovery(conversation))return false;
        const projection=archivalProjection(events,events.length),originals=new Map();
        for(let index=0;index<events.length;index++){
          const event=events[index];if(!selected.has(event.seq))continue;
          const messages=projection.get(index)||[];
          if(messages.length&&captureAfter!==null&&Number(event.time)<=captureAfter){warn('context_archive_policy_boundary');return false;}
          for(const message of messages){
            const payload={conversation_id:conversation,...message};
            const key=JSON.stringify([payload.message_id,payload.role]);
            const previous=originals.get(key);
            if(previous&&previous.text!==payload.text){warn('context_archive_identity_mismatch');return false;}
            originals.set(key,payload);
          }
        }
        for(const payload of originals.values()){
          if(signal?.aborted||Date.now()>=deadline)return false;
          if(!queue.enqueue(payload)||!await queue.drainUntil(payload,Math.max(0,deadline-Date.now())))return false;
        }
        const messages=[...originals.values()].map(payload=>({message_id:payload.message_id,role:payload.role,
          body_sha256:createHash('sha256').update(payload.text,'utf8').digest('hex')}));
        for(let index=0;index<messages.length;index+=256){
          if(signal?.aborted||Date.now()>=deadline)return false;
          const chunk=messages.slice(index,index+256),timeout=AbortSignal.timeout(Math.max(1,deadline-Date.now()));
          const combined=signal?AbortSignal.any([signal,timeout]):timeout;
          const response=await fetchImpl(baseUrl+'/v1/archive/confirm',{method:'POST',redirect:'error',signal:combined,
            headers:{'Content-Type':'application/json',Authorization:`Bearer ${token}`},
            body:JSON.stringify({conversation_id:conversation,messages:chunk})});
          const reply=await response.json();
          if(!response.ok||reply?.ok!==true||reply.data?.confirmed!==true||!Array.isArray(reply.data.items)||reply.data.items.length!==chunk.length){
            warn(response.status===404?'context_archive_backend_upgrade_required':'context_archive_not_confirmed');return false;
          }
          if(reply.data.items.some((item,i)=>item.message_id!==chunk[i].message_id||item.role!==chunk[i].role||item.state!=='stored'))return false;
        }
        return !signal?.aborted;
      }catch{warn('context_archive_confirmation_failed');return false;}
    };
    contextManagement=createContextManager({enabled:config.contextEnabled===true,
      triggerTurns:config.contextTriggerTurns??12,keepTurns:config.contextKeepTurns??4,
      timeoutMs:config.contextCompactionTimeoutMs??30000,compactionModule:config.compactionModule,
      prepareAgent:(agent,signal)=>config.compactionSupport?.prepareAgent(agent,signal),
      onWarning:warn,ensureArchived,shutdownSignal:importantShutdown.signal});
    member = session => {
      chatUsage?.attach(session);
      if (!session || session[INSTALL] || typeof session.append !== 'function') return;
      if (session.__fishRawQueueInstalled) { warn('legacy_rawqueue_detected_disable_old_plugin'); return; }
      const original = session.append;
      const wrapped = function (...args) { const event = original.apply(this, args); if (controller.enabled) scan(this); return event; };
      const ref = new WeakRef(session);
      sessions.set(String(session.id), ref); attachments.add(ref); originals.set(session, { original, wrapped, ref });
      session.append = wrapped; session[INSTALL] = wrapped; scan(session);
    };
    if (!proto[PREPARE]) {
      const original = proto.prepare, members = new Set();
      const wrapped = function (...args) { const session = original.apply(this, args); for (const fn of members) { try { fn(session); } catch { /* adapter cannot break host */ } } return session; };
      proto[PREPARE] = { original, wrapped, members }; proto.prepare = wrapped;
    }
    registry = proto[PREPARE]; registry.members.add(member);
    controller.enabled = true;
    const bindCurrentSession = session => {
      if (!controller.enabled || config.captureEnabled === false || !session?.id || !Array.isArray(session.log)) return false;
      member(session); scan(session); return originals.has(session) && drainRecovery(String(session.id));
    };
    const historyTimeout = Number(config.historyTimeoutMs ?? 2000);
    sessionHistory = createSessionHistory({captureMessage,bindSession:bindCurrentSession,queue,fetchImpl,baseUrl,token,
      timeoutMs:Number.isFinite(historyTimeout)?Math.max(100,Math.min(15000,Math.floor(historyTimeout))):2000,
      enabled:()=>controller.enabled && config.captureEnabled !== false && config.recallEnabled !== false && config.historyEnabled !== false,
      autoEnabled:config.historyAutoEnabled !== false,captureAfter,shutdownSignal:importantShutdown.signal,onWarning:warn});
    if(config.historyEnabled === false)sessionHistory.setToolState('disabled');
    // A hot attachment must also adopt sessions that already existed before
    // this adapter loaded. An idle source conversation may never pre-step again.
    const discoveryWarnings = new Set();
    const discoverLive = () => {
      if (!controller.enabled || config.captureEnabled === false) return;
      const adopt = session => { if (session?.id && Array.isArray(session.log)) member(session); };
      for (const name of ['sessions', 'agents']) {
        try {
          const service = ctx.get?.(name);
          const rows = service?.list?.();
          if (!Array.isArray(rows)) continue;
          for (const row of rows) adopt(name === 'agents' ? row?.session : row);
        } catch {
          if (!discoveryWarnings.has(name)) { discoveryWarnings.add(name); warn('live_' + name + '_discovery_unavailable'); }
        }
      }
    };
    listen('session/created', session => { if (controller.enabled && config.captureEnabled !== false) member(session); });
    listen('session/event', session => {
      if (!controller.enabled || config.captureEnabled === false) return;
      member(session); scan(session);
    });
    listen('session/disposed', session => release(session));
    discoverLive();
    let ticking = false;
    const tick = async () => {
      if (!controller.enabled || ticking) return; ticking = true;
      try { drainRecovery(); discoverLive(); for (const session of liveSessions()) scan(session); await queue.flush(); queue.sweep(); }
      finally { ticking = false; }
    };
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
      await contextManagement.beforeStep(payload,decision);
      const cacheKey = `${conversation}\0${turn.id}`;
      const cached = cache.get(cacheKey);
      let blocks = cached?.blocks, hasEvidence = cached?.hasEvidence === true;
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
        hasEvidence = Boolean(evidence.block?.trim());
        // Old backends may ignore include_life_context:false. Preserve their
        // entire block; only add the new side channel when exclusion is explicit
        // or evidence is absent. Never guess by removing text from their result.
        // Explicit owner configuration gets a stable independent allowance and
        // precedes factual evidence. It never waits for raw ACK or retrieval.
        blocks = profile && !evidence.includesProfile ? [profile,evidence.block] : [evidence.block];
        if (life && (!evidence.block || evidence.includesLife === false)) blocks.push(life);
        blocks = [...new Set(blocks.filter(block => typeof block === 'string' && block.trim()))];
        if (!payload?.signal?.aborted) {
          cache.set(cacheKey, {blocks,hasEvidence}); if (cache.size > 100) cache.delete(cache.keys().next().value);
        }
      }
      // History has its own single-turn frozen transient cache. It never enters
      // durable decision.messages, and the next user turn re-checks permission.
      const historyBlock = await sessionHistory.automatic(payload,decision,{coreEvidence:hasEvidence,existingBlock:[...existing,...blocks].join('\n')});
      if(historyBlock)blocks=[...blocks,historyBlock];
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
    listen('agent/pre-step', handlePreStep);
    controller.recall = handlePreStep;
    if (config.importantToolEnabled === false) setImportantStatus('disabled');
    else setImportantStatus('waiting_tools');
    if (config.importantToolEnabled !== false || config.historyEnabled !== false) {
      const toolsUnavailable = code => {
        if(config.importantToolEnabled !== false)setImportantStatus('unavailable');
        if(config.historyEnabled !== false)sessionHistory.setToolState('unavailable');
        warn(code);
      };
      const installTool = async scoped => {
        const runtime = scoped.get?.('tools') || scoped.tools;
        if (!runtime || typeof runtime.register !== 'function') { toolsUnavailable('memory_tool_runtime_unavailable'); return; }
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
        const removers = [];
        try {
          if(config.importantToolEnabled !== false){
            removers.push(runtime.register(createImportantTool({ defineTool, captureMessage, queue, fetchImpl, baseUrl, token,
              timeoutMs: importantTimeout, enabled: () => controller.enabled, shutdownSignal: importantShutdown.signal,
              bindSession:bindCurrentSession })));
            setImportantStatus('ready', true);
          }
          if(config.historyEnabled !== false){removers.push(runtime.register(sessionHistory.createTool(defineTool)));sessionHistory.setToolState('ready');}
        } catch(error) { for(const remove of removers.reverse())try{remove?.();}catch{} throw error; }
        let disposed=false;
        const unregister=()=>{
          if(disposed)return;disposed=true;
          for(const remove of removers.reverse())try{remove?.();}catch{warn('memory_tool_unregistration_failed');}
          if(toolUnregister===unregister)toolUnregister=null;
          if(config.importantToolEnabled!==false)setImportantStatus(controller.enabled?'waiting_tools':'stopped');
          if(config.historyEnabled!==false)sessionHistory.setToolState(controller.enabled?'not_registered':'stopped');
        };
        toolUnregister=unregister;
        return unregister;
      };
      if (typeof ctx.inject === 'function') {
        // Optional service dependency: register when the real ToolRuntime arrives,
        // and unregister/rebind if that service is disposed or replaced.
        toolDependency = ctx.inject(['tools'], async scoped => {
          try { return await installTool(scoped); }
          catch { toolsUnavailable('memory_tool_registration_failed'); }
        });
      } else if (ctx.tools) {
        try { await installTool(ctx); } catch { toolsUnavailable('memory_tool_registration_failed'); }
      } else { toolsUnavailable('memory_tool_runtime_unavailable'); }
    }
    controller.stop = () => stopPromise ||= (async () => {
      controller.enabled = false;sessionHistory?.stop();contextManagement?.stop();config.requestBudget?.stop();config.requestBridge?.dispose(); importantShutdown.abort(); clearInterval(timer); registry.members.delete(member);
      for (const off of listeners.splice(0).reverse()) { try { off(); } catch { warn('adapter_listener_cleanup_failed'); } }
      for (const session of liveSessions()) release(session);
      originals = new WeakMap();
      await stopSupport();
      await chatUsage?.stop();
      if (toolDependency?.dispose) await toolDependency.dispose();
      if (toolUnregister) { toolUnregister(); toolUnregister = null; }
      setImportantStatus('stopped');
      if (!registry.members.size && proto.prepare === registry.wrapped) { proto.prepare = registry.original; delete proto[PREPARE]; }
      // Do not remove a lock while an HTTP delivery is still in flight.
      const end = Date.now() + timeoutMs + 250;
      while (queue.flushing && Date.now() < end) await new Promise(r => setTimeout(r, 20));
      queue.close(); sessions.clear(); attachments.clear(); cache.clear();
    })();
    listen('dispose', () => controller.stop());
    ctx.effect?.(() => () => controller.stop());
    ctx.logger?.info?.('xiaojiu-brain: enabled; visible messages only; request-level recall');
  } catch (e) {
    controller.enabled = false; clearInterval(timer); registry?.members.delete(member);
    if (registry && !registry.members.size && proto?.prepare === registry.wrapped) { proto.prepare = registry.original; delete proto[PREPARE]; }
    for (const off of listeners.splice(0).reverse()) { try { off(); } catch {} }
    for (const session of liveSessions()) { const saved = originals.get(session); if (saved && session.append === saved.wrapped) session.append = saved.original; if (saved && session[INSTALL] === saved.wrapped) delete session[INSTALL]; }
    originals = new WeakMap(); attachments.clear(); sessions.clear(); cache.clear();
    sessionHistory?.stop();contextManagement?.stop();config.requestBudget?.stop();config.requestBridge?.dispose(); await stopSupport();await chatUsage?.stop(); warn(String(e.message || 'adapter_initialization_failed').replace(/[^a-zA-Z0-9_:.-]/g, '')); queue?.close();
  }
  return controller;
}

// Cordis consumes apply's return value as an Effect. A controller object is
// not an Effect; installation owns its disposer through ctx.effect instead.
export async function apply(ctx, config = {}) { await createAdapter(ctx, config); }
