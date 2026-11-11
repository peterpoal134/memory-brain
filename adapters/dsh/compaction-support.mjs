/** Portable policies over the compatible host's native compaction services. */
const ORIGINAL = Symbol.for('cordis.original');
const OWN_ENGINE = Symbol('memory-brain.native-compaction-owner');
const PRUNE_MARKER = '\n\n[... tool result middle pruned ...]\n\n';
const original = value => value?.[ORIGINAL] || value;
const integer = (value, minimum = 1) => Number.isSafeInteger(value) && value >= minimum;
const read = (ctx, name, strict = true) => typeof ctx?.get === 'function' ? ctx.get(name, strict) : undefined;

export const COMPACTION_SUPPORT_DEFAULTS = Object.freeze({
  summaryEnabled: true, summaryMaxChars: 2000, summaryMaxTokens: 4096,
  pressureCooldownMs: 60000, pressureThresholdRatio: 0.8, pressureRetainRatio: 0.16, pressureHeadroomTokens: 4096,
  toolPruneEnabled: true, toolPruneThresholdChars: 4096, toolPruneHeadChars: 2048, toolPruneTailChars: 512,
});

function policyOptions(input) {
  const value = { ...COMPACTION_SUPPORT_DEFAULTS, ...Object.fromEntries(Object.entries(input).filter(([,value]) => value !== undefined)) };
  for (const key of ['summaryEnabled','toolPruneEnabled']) if (typeof value[key] !== 'boolean') throw new TypeError('compaction_policy_boolean_invalid');
  for (const key of ['summaryMaxChars','summaryMaxTokens','pressureHeadroomTokens','toolPruneThresholdChars'])
    if (!integer(value[key])) throw new RangeError('compaction_policy_budget_invalid');
  for (const key of ['pressureCooldownMs','toolPruneHeadChars','toolPruneTailChars'])
    if (!integer(value[key], 0)) throw new RangeError('compaction_policy_budget_invalid');
  if (!(value.pressureRetainRatio > 0 && value.pressureRetainRatio < value.pressureThresholdRatio && value.pressureThresholdRatio < 1))
    throw new RangeError('compaction_pressure_ratios_invalid');
  if (value.toolPruneHeadChars + Array.from(PRUNE_MARKER).length + value.toolPruneTailChars > value.toolPruneThresholdChars)
    throw new RangeError('compaction_prune_budgets_invalid');
  return Object.freeze(value);
}

function noOpenCompaction(session) {
  const events = Array.isArray(session?.log) ? session.log :
    typeof session?.snapshotEvents === 'function' ? session.snapshotEvents() : null;
  if (!Array.isArray(events)) return false;
  for (let index = events.length - 1; index >= 0; index--) {
    const type = events[index]?.type;
    if (type === 'compaction/start') return false;
    if (type === 'compaction/end' || type === 'session/end-seed') return true;
  }
  return true;
}

function directive(maxChars) {
  return `请将以上对话压缩为供继续交流使用的简洁中文摘要，目标约 ${maxChars} 字以内。不要复制整篇旧摘要或逐轮流水账。\n` +
    '保留用户明确要求、稳定偏好、有依据的重要事实、当前任务、未完成事项、已发生的变更和未确认之处。技术细节只保留继续工作必需的路径、标识和数字。\n' +
    '区分事实、计划、推测和未核实信息，不添加原话没有提供的记忆或经历。已有摘要应合并、去重并保留仍有效的内容。只输出摘要正文，不调用工具。';
}

export function createCompactionSupport({ ctx, enabled = true, basicCompactionModule, llmModule, cordisModule,
  toolResultPrunerModule, tokenMeterModule, onWarning, shutdownSignal, ...input } = {}) {
  if (typeof enabled !== 'boolean') throw new TypeError('compaction_support_enabled_invalid');
  const options = policyOptions(input), shutdown = new AbortController(), fibers = new Set(), operations = new Set();
  const engineRecords = new Map(), pending = new Map(), prepared = new WeakMap(), cooldown = new WeakMap(), warnings = new Set();
  const ownedEngines = new WeakSet(), ownedPruners = new WeakSet(), ownedMeters = new WeakSet();
  let stopped = false, modulesPromise, disposeCreated;
  const view = { state: enabled ? 'waiting_agent' : 'disabled', engine_state: 'not_observed', summary_state: options.summaryEnabled ? 'not_observed' : 'disabled',
    pressure_state: 'not_observed', pruner_state: options.toolPruneEnabled ? 'not_observed' : 'disabled',
    summaries: 0, pressure_attempts: 0, pressure_deferred: 0, pruned_results: 0, pruned_chars: 0, failures: 0,
    last_summary_chars: null, last_reason: null };
  const state = (name, reason = null) => { if (!stopped) { view.state = name; view.last_reason = reason; } };
  const warn = code => {
    if (warnings.has(code)) return;
    warnings.add(code);try { onWarning?.(code); } catch {}
  };
  const combined = signal => AbortSignal.any([shutdown.signal, ...[shutdownSignal, signal].filter(Boolean)]);
  function track(task) {
    let promise;
    try { promise = Promise.resolve(task()); } catch (error) { promise = Promise.reject(error); }
    operations.add(promise);
    promise.then(() => operations.delete(promise), () => operations.delete(promise));return promise;
  }
  function modules() {
    modulesPromise ??= (async () => {
      if (![basicCompactionModule,llmModule,cordisModule].every(value => typeof value === 'string' && value)) return null;
      try {
        const [basic,llm,cordis,pruner,meter] = await Promise.all([import(basicCompactionModule),import(llmModule),import(cordisModule),
          toolResultPrunerModule ? import(toolResultPrunerModule).catch(() => null) : null,
          tokenMeterModule ? import(tokenMeterModule).catch(() => null) : null]);
        if (typeof basic.BasicCompactionEngine !== 'function' || typeof llm.BlockAssembler !== 'function' ||
            !cordis.symbols?.isolate || typeof ctx?.isolate !== 'function') return null;
        return { basic: basic.BasicCompactionEngine, llm, cordis, pruner: pruner?.ToolResultPruner, meter: meter?.TokenMeter };
      } catch { return null; }
    })();
    return modulesPromise;
  }
  function ownsAgent(agent, engine) { return prepared.get(agent)?.engine === engine; }
  function admit(agent, engine) {
    if (!ownsAgent(agent,engine)) return false;
    shutdown.signal.throwIfAborted();shutdownSignal?.throwIfAborted();
    if (!enabled || stopped) throw new Error('compaction_support_stopped');
    if (original(read(agent?.ctx,'compaction')) !== engine) throw new Error('compaction_agent_service_changed');
    return true;
  }
  function ownerContext(agent, host) {
    let owner = ctx;
    const labels = agent.ctx[host.cordis.symbols.isolate];
    for (const name of ['compaction','toolResultPruner','tokenMeter','llm','sessions','sessionProjections'])
      if (labels?.[name] !== undefined) owner = owner.isolate(name, labels[name]);
    return owner;
  }
  async function provide(agent, host, name, Class, config) {
    const available = read(agent.ctx, name);
    if (available) return available;
    if (read(agent.ctx, name, false)) return null; // An existing provider is still starting/stopping.
    const label = agent.ctx[host.cordis.symbols.isolate]?.[name] || name;
    const current = pending.get(label);
    if (current) { await current;return read(agent.ctx, name); }
    const operation = (async () => {
      if (stopped || shutdownSignal?.aborted) return;
      const fiber = ownerContext(agent, host).plugin(Class, config);fibers.add(fiber);
      try { await fiber.await();if (fiber.state !== 2) throw new Error('native_service_not_active'); }
      catch (error) { fibers.delete(fiber);await fiber.dispose();throw error; }
    })();
    pending.set(label, operation);
    try { await operation; } finally { if (pending.get(label) === operation) pending.delete(label); }
    return read(agent.ctx, name);
  }
  function pressureView(receiver) {
    const config = { ...receiver.config, thresholdRatio: options.pressureThresholdRatio,
      retainRatio: options.pressureRetainRatio, headroomTokens: options.pressureHeadroomTokens };
    delete config.retainTokens;
    const shadow = Object.create(receiver);Object.defineProperty(shadow, 'config', { value: config });return shadow;
  }
  async function runPressure(receiver, native, agent, trigger, signal, engine) {
    if (!admit(agent,engine)) throw new Error('compaction_agent_not_admitted');
    const abort = combined(signal);abort.throwIfAborted();
    if (trigger === 'pressure' && Date.now() < (cooldown.get(agent.session) || 0)) {
      view.pressure_deferred++;view.pressure_state = 'cooldown';return null;
    }
    view.pressure_attempts++;view.pressure_state = 'ready';
    try {
      // Preserve Cordis' traced service context: replacing it with agent.ctx
      // would discard the native engine's declared dependency permissions.
      const shadow = pressureView(receiver);
      const result = await Reflect.apply(native, shadow, [agent,trigger,abort]);
      if (trigger === 'pressure' && result) cooldown.set(agent.session, Date.now() + options.pressureCooldownMs);
      return result;
    } catch (error) {
      if (trigger === 'pressure' && !abort.aborted) cooldown.set(agent.session, Date.now() + options.pressureCooldownMs);
      if (!abort.aborted && /contextWindow|message budget|pressure budget|retainTokens|context capacity/.test(String(error?.message || ''))) {
        view.pressure_state = 'needs_config';warn('compaction_pressure_needs_config');
      }
      throw error;
    }
  }
  async function summarize(receiver, host, native, input, agent, signal, engine) {
    if (!admit(agent,engine)) throw new Error('compaction_agent_not_admitted');
    const abort = combined(signal);abort.throwIfAborted();
    if (!options.summaryEnabled) return Reflect.apply(native,receiver,[input,agent,abort]);
    const latest = agent.session.requestHeader()?.config;
    const route = latest?.provider && latest?.model ? latest : agent.options;
    const override = receiver.config?.modelPolicies?.find(item => item.provider === route?.provider && item.model === route?.model);
    const provider = override?.summarizationProvider ?? receiver.config?.summarizationProvider;
    const model = override?.summarizationModel ?? receiver.config?.summarizationModel;
    const target = provider && model ? { provider,model } : route;
    if (!target?.provider || !target?.model) throw new Error('compaction_summary_route_unavailable');
    const llm = read(agent.ctx, 'llm');
    if (typeof llm?.stream !== 'function') throw new Error('compaction_summary_model_unavailable');
    const maxTokens = integer(override?.maxTokens) ? override.maxTokens : options.summaryMaxTokens;
    const optionsForCall = { provider: target.provider, model: target.model,
      messages: [...input.messages,{ role:'user',content:[{ type:'text',text:directive(options.summaryMaxChars) }] }],
      toolHistory: agent.session.toolHistory(), ...(input.tools === undefined ? {} : { tools:[...input.tools] }),
      maxTokens, sessionId: agent.session.id, purpose:'compaction', signal:abort };
    const assembler = new host.llm.BlockAssembler();
    for await (const chunk of llm.stream(optionsForCall)) assembler.push(chunk);
    abort.throwIfAborted();
    if (assembler.finish?.kind !== 'stop') throw new Error('compaction_summary_incomplete');
    const rawOutput = assembler.blocks();
    if (rawOutput.some(block => ['image','tool-call'].includes(block.type))) throw new Error('compaction_summary_non_text');
    const summary = rawOutput.filter(block => block.type === 'text' && typeof block.text === 'string');
    if (!summary.some(block => block.text.trim())) throw new Error('compaction_summary_empty');
    view.summaries++;view.last_summary_chars = summary.reduce((count,block) => count + Array.from(block.text).length, 0);
    view.summary_state = 'short_chinese';
    return { summary,rawOutput,llmStreamCall:true,provider:target.provider,model:target.model,maxTokens,
      ...(assembler.usage === undefined ? {} : { usage:assembler.usage }) };
  }
  function adoptStock(engine, host) {
    if (engineRecords.has(engine) || ownedEngines.has(engine)) return;
    const record = { engine, changes:[] };engineRecords.set(engine,record);
    if (engine.constructor !== host.basic || engine.summarize !== host.basic.prototype.summarize ||
        engine.compactIfNeeded !== host.basic.prototype.compactIfNeeded || engine.compactRegion !== host.basic.prototype.compactRegion) {
      view.summary_state = 'host_custom';view.pressure_state = 'host_custom';return;
    }
    const replace = (key, native, wrapper) => {
      const descriptor = Object.getOwnPropertyDescriptor(engine,key);
      if (descriptor && descriptor.configurable === false) return false;
      Object.defineProperty(engine,key,{ value:wrapper,writable:true,configurable:true });record.changes.push({ key,descriptor,wrapper });return true;
    };
    if (engine.summarize === host.basic.prototype.summarize) {
      const native = engine.summarize;
      const installed = replace('summarize',native,function(input,agent,signal) {
        if (!ownsAgent(agent,engine)) return Reflect.apply(native,this,[input,agent,signal]);
        try { admit(agent,engine); } catch (error) { return Promise.reject(error); }
        return track(() => summarize(this,host,native,input,agent,signal,engine));
      });
      view.summary_state = installed ? options.summaryEnabled ? 'short_chinese' : 'disabled' : 'host_custom';
    } else view.summary_state = 'host_custom';
    if (engine.compactIfNeeded === host.basic.prototype.compactIfNeeded) {
      const native = engine.compactIfNeeded;
      const installed = replace('compactIfNeeded',native,function(agent,trigger,signal) {
        if (!ownsAgent(agent,engine)) return Reflect.apply(native,this,[agent,trigger,signal]);
        try { admit(agent,engine); } catch (error) { return Promise.reject(error); }
        return track(() => runPressure(this,native,agent,trigger,signal,engine));
      });
      view.pressure_state = installed ? engine.config?.auto === false ? 'host_auto_disabled' : 'ready' : 'host_custom';
    } else view.pressure_state = 'host_custom';
  }
  function portableClass(host) {
    return class MemoryCompactionEngine extends host.basic {
      constructor(scope) {
        super(scope,{ auto:true,thresholdRatio:options.pressureThresholdRatio,retainRatio:options.pressureRetainRatio,
          headroomTokens:options.pressureHeadroomTokens,maxTokens:options.summaryMaxTokens });
        ownedEngines.add(this);
        Object.defineProperty(this,OWN_ENGINE,{ value:this });
      }
      compactIfNeeded(agent,trigger,signal) {
        const engine = this[OWN_ENGINE];
        // A global fallback must never pressure-compact an agent that has its
        // own closer service; the native auto hooks remain the sole triggers.
        if (!ownsAgent(agent,engine)) return Promise.resolve(null);
        try { admit(agent,engine); } catch (error) { return Promise.reject(error); }
        return track(() => runPressure(this,host.basic.prototype.compactIfNeeded,agent,trigger,signal,engine));
      }
      summarize(input,agent,signal) {
        const engine = this[OWN_ENGINE];
        try { if (!admit(agent,engine)) throw new Error('compaction_agent_not_admitted'); }
        catch (error) { return Promise.reject(error); }
        return track(() => summarize(this,host,host.basic.prototype.summarize,input,agent,signal,engine));
      }
    };
  }
  async function prepare(agent, signal) {
    if (!enabled || stopped || shutdownSignal?.aborted || signal?.aborted || !agent?.ctx || !agent.session) return;
    if (agent.status === 'running' && !signal) { state('waiting_idle','agent_active');return; }
    const host = await modules();
    if (stopped || shutdownSignal?.aborted || signal?.aborted) return;
    if (!host) { state('unavailable','native_modules_unavailable');warn('compaction_native_modules_unavailable');return; }
    if (!read(agent.ctx,'sessionProjections') || !read(agent.ctx,'sessions') || !read(agent.ctx,'llm')) {
      state('unavailable','native_dependencies_unavailable');return;
    }
    let meter = read(agent.ctx,'tokenMeter');
    if (!meter && host.meter) { meter = await provide(agent,host,'tokenMeter',host.meter);if (meter) ownedMeters.add(original(meter)); }
    if (stopped || shutdownSignal?.aborted || signal?.aborted) return;
    if (!meter) { state('unavailable','token_meter_unavailable');warn('compaction_token_meter_unavailable');return; }
    let engine = read(agent.ctx,'compaction');
    if (!engine) engine = await provide(agent,host,'compaction',portableClass(host));
    if (stopped || shutdownSignal?.aborted || signal?.aborted) return;
    if (!engine || typeof engine.compactRegion !== 'function') { state('unavailable','native_engine_unavailable');return; }
    const canonical = original(engine);prepared.set(agent,{ engine:canonical });
    view.engine_state = ownedEngines.has(canonical) ? 'owned_native' : 'host_existing';
    if (ownedEngines.has(canonical)) {
      view.summary_state = options.summaryEnabled ? 'short_chinese' : 'disabled';view.pressure_state = 'ready';
    } else adoptStock(canonical,host);
    if (options.toolPruneEnabled) {
      let pruner = read(agent.ctx,'toolResultPruner');
      if (!pruner && host.pruner) {
        pruner = await provide(agent,host,'toolResultPruner',host.pruner,{ thresholdChars:options.toolPruneThresholdChars,
          headChars:options.toolPruneHeadChars,tailChars:options.toolPruneTailChars });
        if (pruner) ownedPruners.add(original(pruner));
      }
      if (stopped || shutdownSignal?.aborted || signal?.aborted) return;
      view.pruner_state = pruner ? ownedPruners.has(original(pruner)) ? 'owned_native' : 'host_existing' : 'unavailable';
      // The native operation is synchronous and retains original tool events.
      // Never prune while a summary transaction holds the durable session lock.
      if (pruner && typeof pruner.pruneSession === 'function' && !signal?.aborted && noOpenCompaction(agent.session)) {
        const result = pruner.pruneSession(agent.session);
        view.pruned_results += Array.isArray(result?.pruned) ? result.pruned.length : 0;
        view.pruned_chars += Number.isFinite(result?.charsRemoved) ? result.charsRemoved : 0;
      }
    }
    state(view.pruner_state === 'unavailable' ? 'partial' : 'ready');
  }
  const prepareAgent = (agent,signal) => track(async () => {
    try { await prepare(agent,signal); }
    catch { if (!stopped && !signal?.aborted) { view.failures++;state('unavailable','native_support_failed');warn('compaction_support_failed'); } }
  });
  if (enabled && typeof ctx?.on === 'function') disposeCreated = ctx.on('agent/created',({ agent }) => prepareAgent(agent));
  const ready = (async () => {
    if (!enabled) return;
    try { for (const agent of read(ctx,'agents')?.list?.() || []) await prepareAgent(agent); }
    catch { state('unavailable','existing_agents_unavailable'); }
  })();
  return {
    ready,prepareAgent,status:() => ({ ...view }),
    async stop() {
      if (stopped) return;
      stopped = true;shutdown.abort();view.state = 'stopped';view.last_reason = 'support_stopped';
      try { disposeCreated?.(); } catch {}
      await Promise.allSettled([...operations,...pending.values()]);
      for (const {engine,changes} of engineRecords.values()) for (const {key,descriptor,wrapper} of changes) {
        if (engine[key] !== wrapper) continue;
        if (descriptor) Object.defineProperty(engine,key,descriptor);else delete engine[key];
      }
      engineRecords.clear();
      for (const fiber of [...fibers].reverse()) try { await fiber.dispose();await fiber.await(); } catch {}
      fibers.clear();
    }
  };
}
