/** Turn-based policy over the current agent's native compaction transaction. */
const ORIGINAL_SERVICE = Symbol.for('cordis.original');

function realUser(message) {
  return message?.role === 'user' && message.source?.kind === 'user' &&
    message.source.form === undefined && message.id !== undefined && message.id !== null && String(message.id).length > 0;
}
function currentUser(payload, decision) {
  for (const messages of [payload?.messages, decision?.messages]) {
    if (!Array.isArray(messages)) continue;
    for (let index = messages.length - 1; index >= 0; index--) if (realUser(messages[index])) return messages[index];
  }
  return null;
}
function activeEngine(agent) {
  return typeof agent?.ctx?.get === 'function' ? agent.ctx.get('compaction') : undefined;
}
function serviceIdentity(service) { return service?.[ORIGINAL_SERVICE] || service; }
function numberOrNull(value) { return Number.isFinite(value) && value >= 0 ? value : null; }
function measure(agent) {
  try { return numberOrNull(agent.ctx.get('tokenMeter')?.measure(agent.session)?.totalTokens); }
  catch { return null; }
}
function surfaceSnapshot(session) {
  const surface = session.surface;
  if (!Array.isArray(surface?.nodes) || typeof session.eventAt !== 'function') return null;
  const nodes = [...surface.nodes], events = nodes.map(seq => session.eventAt(seq));
  if (events.some((event, index) => !event || event.seq !== nodes[index])) return null;
  return { nodes, events, replaceGeneration: surface.replaceGeneration, contentGeneration: surface.contentGeneration };
}
function unchangedSurface(session, snapshot) {
  const surface = session.surface;
  return Array.isArray(surface?.nodes) && surface.replaceGeneration === snapshot.replaceGeneration &&
    surface.contentGeneration === snapshot.contentGeneration && surface.nodes.length === snapshot.nodes.length &&
    snapshot.nodes.every((seq, index) => surface.nodes[index] === seq && session.eventAt(seq) === snapshot.events[index]);
}

/** Read log order, rather than comparing sequence numbers across replacements. */
function turnHistory(session, pending, turn) {
  const events = typeof session.snapshotEvents === 'function' ? session.snapshotEvents() : session.log;
  if (!Array.isArray(events)) return null;
  const turnBySeq = new Map(), closed = new Set(), sinceCheckpoint = new Set(), ids = new Set();
  const hasTurnMarkers = events.some(event => event?.type === 'turn/start');
  let open = null;
  for (const event of events) {
    if (event?.type === 'turn/start') open = `turn:${String(event.data?.turn)}`;
    if (event?.type === 'user/message') {
      const message = event.data;
      // Only a committed replacement checkpoint resets the policy. A failed
      // transaction's summary event by itself is not a successful checkpoint.
      if (message?.source?.kind === 'compact-checkpoint' && event.surfaceOp?.op === 'replace') sinceCheckpoint.clear();
      if (realUser(message)) {
        const id = String(message.id), key = open || `user:${id}`;
        ids.add(id); turnBySeq.set(event.seq, key); sinceCheckpoint.add(key);
        if (!hasTurnMarkers) closed.add(key);
      }
    }
    if (event?.type === 'turn/end' && open !== null) {
      closed.add(open); open = null;
    }
  }
  // At the first pre-step, an admitted inbox message is not yet a surface node.
  if (!ids.has(String(pending.id))) sinceCheckpoint.add(Number.isInteger(turn) ? `turn:${turn}` : `user:${String(pending.id)}`);
  return { turnBySeq, closed, count: sinceCheckpoint.size };
}

function selectPrefix(session, snapshot, history, keepTurns, helpers) {
  const { nodes, events } = snapshot;
  if (nodes.length === 0) return null;
  const first = events[0].type === 'system/message' ? 1 : 0;
  const complete = [];
  const seen = new Set();
  for (let index = first; index < nodes.length; index++) {
    if (events[index].type !== 'user/message' || !realUser(events[index].data)) continue;
    const key = history.turnBySeq.get(nodes[index]);
    if (key !== undefined && history.closed.has(key) && !seen.has(key)) {
      seen.add(key); complete.push({ key, index });
    }
  }
  if (complete.length <= keepTurns) return null;
  let cut = complete[complete.length - keepTurns].index;
  // Move the cut toward older content when necessary, retaining the complete
  // tool unit with the recent tail instead of dropping part of that tail.
  while (cut > first && (!helpers.toolPairingBalancedBefore(session, nodes[cut]) ||
      !helpers.toolPairingBalancedAfter(session, nodes[cut - 1]))) cut--;
  if (cut <= first || !helpers.toolPairingBalancedBefore(session, nodes[first])) return null;
  return { start: nodes[first], end: nodes[cut - 1], seqs: nodes.slice(first, cut) };
}

export function createContextManager({ enabled = true, triggerTurns = 12, keepTurns = 4, timeoutMs = 30000,
  compactionModule, onWarning, ensureArchived, prepareAgent, shutdownSignal } = {}) {
  if (typeof enabled !== 'boolean') throw new TypeError('context_enabled_must_be_boolean');
  if (!Number.isInteger(triggerTurns) || triggerTurns < 2 || triggerTurns > 200 ||
      !Number.isInteger(keepTurns) || keepTurns < 1 || keepTurns > 199 || keepTurns >= triggerTurns) {
    throw new RangeError('context_turn_limits_invalid');
  }
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 2147483647) throw new RangeError('context_timeout_invalid');
  const sessions = new WeakMap(), stopController = new AbortController(), warned = new Set();
  let stopped = false, helpersPromise;
  const view = { state: enabled ? 'waiting_agent' : 'disabled', trigger_turns: triggerTurns, keep_turns: keepTurns,
    successes: 0, deferred: 0, failures: 0, last_reason: null,
    estimated_before_tokens: null, estimated_after_tokens: null, estimated_compacted_tokens: null };
  const setState = (state, reason = null) => {
    if (!stopped) { view.state = state; view.last_reason = reason; }
  };
  const warn = code => {
    if (warned.has(code)) return;
    warned.add(code);
    try { if (typeof onWarning === 'function') onWarning(code); } catch {}
  };
  const defer = (state, reason = state) => { view.deferred++; setState(state, reason); };
  const helpers = () => {
    if (typeof compactionModule !== 'string' || !compactionModule.trim()) return Promise.resolve(null);
    helpersPromise ??= import(compactionModule).then(module =>
      typeof module.toolPairingBalancedBefore === 'function' && typeof module.toolPairingBalancedAfter === 'function' ? module : null,
      () => null);
    return helpersPromise;
  };

  async function beforeStep(payload, decision) {
    if (!enabled || stopped || shutdownSignal?.aborted || decision?.kind !== 'enter') return decision;
    // Service preparation and synchronous tool-result pruning also apply to
    // admitted tool continuations; turn-based summarization remains step-one.
    if (typeof prepareAgent === 'function' && payload?.agent) {
      try { await prepareAgent(payload.agent, payload.signal); }
      catch { warn('context_native_support_unavailable'); }
    }
    if (stopped || shutdownSignal?.aborted || payload?.signal?.aborted) return decision;
    // DSH's first admitted model step is numbered one; tool continuations must
    // never initiate another policy compaction for the same real input turn.
    if (payload?.step !== 1) return decision;
    const agent = payload.agent, session = agent?.session, user = currentUser(payload, decision);
    if (!session || typeof session !== 'object' || !user) return decision;
    let record = sessions.get(session);
    if (!record) { record = { busy: false, attemptedTurn: null }; sessions.set(session, record); }
    const turnKey = Number.isInteger(payload.turn) ? `turn:${payload.turn}` : `user:${String(user.id)}`;
    if (record.busy) { defer('busy', 'policy_in_progress'); return decision; }
    if (record.attemptedTurn === turnKey) return decision;
    record.attemptedTurn = turnKey; record.busy = true;
    let signal;
    try {
      signal = AbortSignal.any([stopController.signal, AbortSignal.timeout(timeoutMs),
        ...[shutdownSignal, payload.signal].filter(Boolean)]);
      signal.throwIfAborted();
      const ownerContext = agent.ctx, engine = activeEngine(agent), identity = serviceIdentity(engine);
      if (!engine || typeof engine.compactRegion !== 'function') {
        defer('unsupported_service', 'native_compaction_unavailable'); warn('context_native_compaction_unavailable'); return decision;
      }
      const module = await helpers();
      signal.throwIfAborted();
      if (!module) { defer('unsupported_module', 'native_boundary_helpers_unavailable'); warn('context_boundary_helpers_unavailable'); return decision; }
      const snapshot = surfaceSnapshot(session), history = turnHistory(session, user, payload.turn);
      if (!snapshot || !history) { defer('unsupported_service', 'session_surface_unavailable'); return decision; }
      if (history.count < triggerTurns) { setState('ready', 'below_trigger'); return decision; }
      const range = selectPrefix(session, snapshot, history, keepTurns, module);
      if (!range) { defer('waiting_complete_turns', 'no_safe_prefix'); return decision; }
      if (typeof ensureArchived !== 'function') { defer('waiting_archive', 'archive_guard_unavailable'); return decision; }
      setState('waiting_archive');
      const archived = await ensureArchived(session, Object.freeze([...range.seqs]), signal);
      signal.throwIfAborted();
      if (archived !== true) { defer('waiting_archive', 'originals_not_acknowledged'); return decision; }
      if (agent.session !== session || agent.ctx !== ownerContext ||
          serviceIdentity(activeEngine(agent)) !== identity || !unchangedSurface(session, snapshot)) {
        defer('changed', 'session_or_service_changed'); return decision;
      }
      signal.throwIfAborted();
      view.estimated_before_tokens = measure(agent);
      view.estimated_after_tokens = null; view.estimated_compacted_tokens = null;
      setState('compacting');
      // The native engine owns durable locks, cancellation, balanced-range
      // validation, summary shrink checks, and transactional replacement.
      const result = await engine.compactRegion(range.start, range.end, agent, signal);
      if (!result || !Array.isArray(result.shadowedSeqs) || result.shadowedSeqs.length === 0) {
        defer('ready', 'native_compaction_no_change'); return decision;
      }
      view.successes++;
      view.estimated_after_tokens = measure(agent);
      view.estimated_compacted_tokens = numberOrNull(result.shadowedTokenCount);
      setState('ready', 'compacted');
    } catch (error) {
      if (signal?.aborted) {
        const timeout = signal.reason?.name === 'TimeoutError';
        defer(timeout ? 'timed_out' : 'cancelled', timeout ? 'operation_timeout' : 'operation_cancelled');
      } else if (error?.code === 'busy') defer('busy', 'native_compaction_busy');
      else if (error?.code === 'cancelled') defer('cancelled', 'native_compaction_cancelled');
      else if (error?.code === 'changed') defer('changed', 'native_surface_changed');
      else {
        view.failures++; setState('failed', 'native_compaction_failed'); warn('context_compaction_failed');
      }
    } finally { record.busy = false; }
    return decision;
  }

  return {
    beforeStep,
    status: () => ({ ...view }),
    stop() {
      if (stopped) return;
      stopped = true; view.state = 'stopped'; view.last_reason = 'manager_stopped'; stopController.abort();
    }
  };
}
