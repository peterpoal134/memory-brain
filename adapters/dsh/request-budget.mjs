/** Read-only guard for the final request snapshot.
 *
 * UTF-8 JSON bytes and a heuristic token reference are diagnostics only. They
 * are not a provider tokenizer and never block on estimated input occupancy.
 * Only a known window exhausted by declared output/safety reserves is rejected.
 * Non-text pricing stays partial. No request or native overflow handling is cut.
 */
export const CONTEXT_BUDGET_ERROR_CODE = 'memory_brain_context_budget_exceeded';

export class ContextBudgetError extends Error {
  constructor() {
    super('模型窗口不足以容纳已声明的输出预留与安全余量，已在发送前停止。请核对模型窗口、maxTokens和安全余量。');
    this.name = 'ContextBudgetError';
    this.code = CONTEXT_BUDGET_ERROR_CODE;
  }
}

const integer = value => Number.isSafeInteger(value) && value >= 0;
const positive = value => integer(value) && value > 0;
const supportedTextBlocks = new Set(['text', 'reasoning', 'tool-call', 'tool-addition', 'tool-removal', 'notice']);
const unavailableReasons = new Set([
  'host_loop_build_unverified', 'host_llm_helpers_missing', 'agent_missing',
  'native_snapshot_present', 'unknown_buildRequest_method', 'unknown_step_method',
  'native_snapshot_missing', 'unknown_withRequestMessages_method', 'native_request_binding_mismatch',
  'instance_method_not_writable', 'instance_method_install_failed',
  'instance_method_restore_failed', 'reentrant_instance_call',
  'request_binding_mismatch', 'host_request_shape_or_brand_mismatch',
  'request_budget_binding_unavailable',
  'request_snapshot_unavailable', 'final_decision_unavailable',
  'instance_inspection_failed', 'abort_binding_unavailable',
  'request_shape_unsupported', 'request_serialization_unavailable',
  'guard_unavailable',
]);

function requestCapacity(request, preparedCall, agent) {
  const prepared = preparedCall?.context?.contextWindow;
  if (positive(prepared)) return prepared;
  try {
    const current = agent?.session?.requestContext?.();
    if (current?.provider === request.provider && current?.model === request.model && positive(current.contextWindow)) {
      return current.contextWindow;
    }
  } catch { /* Missing metadata is reported without retaining an exception. */ }
  return null;
}

function estimateInput(request) {
  let partial = false;
  let fileCount = 0;
  // Price a detached projection only. The actual frozen request stays intact.
  const messages = request.messages.map(message => {
    if (!message || typeof message !== 'object') throw new TypeError('request_shape_unsupported');
    if (!Array.isArray(message.content)) {
      if (typeof message.content !== 'string') throw new TypeError('request_shape_unsupported');
      return message;
    }
    const content = message.content.map(block => {
      if (!block || typeof block !== 'object') throw new TypeError('request_shape_unsupported');
      if (block.type === 'image') {
        partial = true;
        // Binary/image pricing belongs to the provider; do not mistake base64
        // size for text tokens or break an otherwise valid image conversation.
        return { type: 'image', pricing: 'not_estimated' };
      }
      if (block.type === 'file') { partial = true; fileCount++; }
      else if (!supportedTextBlocks.has(block.type)) partial = true;
      return block;
    });
    return { ...message, content };
  });
  const encoded = JSON.stringify({ messages, tools: request.tools, toolHistory: request.toolHistory });
  if (typeof encoded !== 'string') throw new TypeError('request_serialization_unavailable');
  const framing = 512 + messages.length * 16 + (Array.isArray(request.tools) ? request.tools.length * 8 : 0);
  // File handles acquire a fixed instruction and execution-world path later.
  // This allowance is only an estimate, hence file requests stay partial.
  const bytes = Buffer.byteLength(encoded, 'utf8');
  // A rough diagnostic, never a claim about this provider's tokenizer. In
  // particular, raw UTF-8 bytes must not be compared with a token window.
  const tokens = Math.ceil(bytes / 3) + framing + fileCount * 1024;
  if (!integer(bytes) || !integer(tokens)) throw new TypeError('request_serialization_unavailable');
  return { bytes, tokens, partial };
}

export function createRequestBudget({ enabled = true, maxInputTokens = null, safetyTokens = 2048, onWarning } = {}) {
  if (typeof enabled !== 'boolean' || !(maxInputTokens === null || positive(maxInputTokens)) || !integer(safetyTokens) ||
      !(onWarning === undefined || typeof onWarning === 'function')) throw new TypeError('invalid_request_budget_configuration');
  let stopped = false;
  let warningCallback = onWarning;
  const warned = new Set();
  const state = {
    checks: 0, passed: 0, blocked: 0, unguarded: 0, partial: 0, warnings: 0,
    active: false, scope: enabled ? 'awaiting_request' : 'disabled', last_reason: null,
    estimated_input_tokens: null, input_limit_tokens: null, context_window_tokens: null,
    reserved_output_tokens: null, estimated_input_bytes: null, estimation_basis: null,
    estimated_over_limit: null, input_estimate_advisory: true, reserve_check_applied: false,
  };
  function status() {
    // No provider, model, message, identity, source path or exception is stored.
    return { enabled, stopped, safety_tokens: safetyTokens, ...state };
  }
  function notify(reason) {
    if (warned.has(reason)) return;
    warned.add(reason); state.warnings++;
    try { warningCallback?.(reason, Object.freeze(status())); }
    catch { /* A diagnostic consumer cannot change request admission. */ }
  }
  function unavailable(reason = 'guard_unavailable') {
    if (stopped || !enabled) return;
    state.active = false;
    state.scope = 'unavailable';
    state.last_reason = unavailableReasons.has(reason) ? reason : 'guard_unavailable';
    state.estimated_input_tokens = null; state.input_limit_tokens = null;
    state.context_window_tokens = null; state.reserved_output_tokens = null;
    state.estimated_input_bytes = null; state.estimation_basis = null;
    state.estimated_over_limit = null; state.reserve_check_applied = false;
    state.unguarded++;
    notify(state.last_reason);
  }
  function check(request, { preparedCall, agent } = {}) {
    if (stopped || !enabled) return request;
    state.checks++;
    state.active = false;
    state.estimated_input_tokens = null;
    state.input_limit_tokens = null;
    state.context_window_tokens = null;
    state.reserved_output_tokens = null;
    state.estimated_input_bytes = null;
    state.estimation_basis = null;
    state.estimated_over_limit = null;
    state.reserve_check_applied = false;
    if (!request || typeof request !== 'object' || !Array.isArray(request.messages)) {
      unavailable('request_shape_unsupported');
      return request;
    }
    let estimate;
    try { estimate = estimateInput(request); }
    catch { unavailable('request_serialization_unavailable'); return request; }
    const capacity = requestCapacity(request, preparedCall, agent);
    const reserve = integer(request.maxTokens) ? request.maxTokens : null;
    state.estimated_input_tokens = estimate.tokens;
    state.estimated_input_bytes = estimate.bytes;
    state.estimation_basis = 'utf8_json_bytes_div3_heuristic';
    state.context_window_tokens = capacity;
    state.reserved_output_tokens = reserve;
    let limit = maxInputTokens;
    if (capacity !== null) {
      // With an undeclared output reserve only the known input portion can be
      // checked; the resulting scope never claims full context protection.
      const capacityLimit = Math.max(0, capacity - (reserve ?? 0) - safetyTokens);
      limit = limit === null ? capacityLimit : Math.min(limit, capacityLimit);
    }
    state.input_limit_tokens = limit;
    state.estimated_over_limit = limit === null ? null : estimate.tokens > limit;
    state.reserve_check_applied = capacity !== null && (reserve !== null || safetyTokens >= capacity);
    // These are declared controls in one known token unit, not a text guess.
    // Unknown output reservation cannot be invented; safety alone can still
    // exhaust a known window. Equality also leaves no room for any input.
    if (capacity !== null && (safetyTokens >= capacity || reserve !== null && reserve + safetyTokens >= capacity)) {
      state.scope = 'invalid_output_reserve';
      state.blocked++;
      state.last_reason = CONTEXT_BUDGET_ERROR_CODE;
      notify(CONTEXT_BUDGET_ERROR_CODE);
      throw new ContextBudgetError();
    }
    if (capacity === null) {
      state.scope = 'needs_capacity'; state.last_reason = 'needs_capacity';
      notify('needs_capacity');
    } else if (reserve === null) {
      state.scope = 'needs_output_reserve'; state.last_reason = 'needs_output_reserve';
      notify('needs_output_reserve');
    } else if (estimate.partial) {
      state.scope = 'partial_non_text'; state.last_reason = 'non_text_pricing_unavailable';
      notify('non_text_pricing_unavailable');
    } else {
      state.scope = 'advisory_text_json'; state.last_reason = state.estimated_over_limit ? 'input_estimate_over_reference' : 'within_advisory_estimate';
    }
    if (capacity === null || reserve === null || estimate.partial) state.partial++;
    if (limit === null) {
      state.unguarded++; state.passed++;
      return request;
    }
    if (state.estimated_over_limit) notify('input_estimate_over_reference');
    state.passed++;
    return request;
  }
  function stop() {
    stopped = true; warningCallback = null; warned.clear();
    state.active = false; state.scope = 'stopped'; state.last_reason = 'stopped';
  }
  return Object.freeze({ check, status, stop, unavailable });
}
