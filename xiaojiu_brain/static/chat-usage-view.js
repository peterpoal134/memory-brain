/* Usage-only presentation. Never render prompt/answer or arbitrary payload JSON. */
const text = value => typeof value === 'string' ? value : (typeof value === 'number' && Number.isFinite(value) ? String(value) : '未报告');
const known = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
const countKnown = value => Number.isInteger(value) && value >= 0;

export function quantity(value) {
  return known(value) ? value.toLocaleString('zh-CN') : '未报告';
}

function amount(value) {
  if (typeof value === 'string' && !/^\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$/.test(value.trim())) return null;
  if (typeof value !== 'number' && typeof value !== 'string') return null;
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : null;
}

export function money(value) {
  const number = amount(value);
  if (number === null) return '未计价';
  if (number > 0 && number < .00000001) return '少于 ¥0.00000001';
  return '¥' + number.toLocaleString('zh-CN', { minimumFractionDigits: 0, maximumFractionDigits: 8 });
}

export function cacheRate(record = {}) {
  const ratio = record.cache_hit_rate;
  if (known(ratio) && ratio <= 1) return (ratio * 100).toLocaleString('zh-CN', { maximumFractionDigits: 2 }) + '%';
  const reported = record.reported_cache_hit_rate;
  if (known(reported) && reported <= 1) return (reported * 100).toLocaleString('zh-CN', { maximumFractionDigits: 2 }) + '%（已知部分）';
  return '未报告';
}

export function costLabel(record = {}) {
  const low = amount(record.cost_low_cny), high = amount(record.cost_high_cny);
  if (low !== null && low === high && !(record.unpriced_requests > 0) && !(record.uncertain_price_requests > 0) && record.price_period !== 'uncertain') return money(record.cost_low_cny);
  if (low !== null && high !== null && low <= high) return `${money(record.cost_low_cny)} — ${money(record.cost_high_cny)}（${record.unpriced_requests > 0 ? '已知部分区间' : '区间'}）`;
  if (amount(record.estimated_cost_cny) !== null && !(record.unpriced_requests > 0) && !(record.uncertain_price_requests > 0)) return money(record.estimated_cost_cny);
  if (amount(record.known_cost_cny) !== null) return money(record.known_cost_cny) + '（已知部分）';
  return '未计价';
}

export function usageTime(value, timezone) {
  if (value === null || value === undefined || value === '' || typeof value === 'boolean') return '时间未报告';
  const time = new Date(typeof value === 'number' ? value * (Math.abs(value) < 1e12 ? 1000 : 1) : value);
  if (!Number.isFinite(time.getTime())) return '时间未报告';
  try { return time.toLocaleString('zh-CN', { hour12: false, ...(typeof timezone === 'string' ? { timeZone: timezone } : { timeZone: 'UTC' }) }); }
  catch { return time.toISOString(); }
}

const periods = { peak: '标准价时段', off_peak: '闲时价', mixed: '跨价格档', uncertain: '时段不确定' };
const periodLabel = value => periods[value] || '时段未报告';

function node(doc, tag, value = '', cls = '') {
  const result = doc.createElement(tag);
  result.textContent = value;
  if (cls) result.className = cls;
  return result;
}

function metric(doc, label, value) {
  const result = node(doc, 'div', '', 'stat usage-stat');
  result.append(node(doc, 'span', label, 'label'), node(doc, 'div', value, 'value'));
  return result;
}

function partialNote(record) {
  const unknown = countKnown(record.unknown_usage_requests) ? quantity(record.unknown_usage_requests) : '数量未报告';
  const partial = countKnown(record.partial_usage_requests) ? quantity(record.partial_usage_requests) : '数量未报告';
  const unpriced = countKnown(record.unpriced_requests) ? quantity(record.unpriced_requests) : '数量未报告';
  const uncertain = countKnown(record.uncertain_price_requests) ? quantity(record.uncertain_price_requests) : '数量未报告';
  return `未返回用量：${unknown} 次；部分字段缺失：${partial} 次；未计价：${unpriced} 次；价格时段不确定：${uncertain} 次。缺失不代表免费。` + (record.unpriced_requests > 0 ? ' 费用只含可计价部分，所示区间不是全部费用的上界。' : '');
}

export function renderUsageSummary(container, data) {
  const doc = container.ownerDocument;
  container.replaceChildren();
  const record = data?.summary;
  if (!record || typeof record !== 'object' || Array.isArray(record)) {
    container.append(node(doc, 'p', '服务尚未返回对话汇总；未知用量和费用不按 0 计算。', 'muted'));
    return;
  }
  const heading = node(doc, 'p', `所选范围：${quantity(record.turn_count)} 轮 · ${quantity(record.request_count)} 次模型请求 · 已报告合计 ${quantity(record.total_tokens)} tokens`, 'usage-scope');
  container.append(heading);
  const grid = node(doc, 'div', '', 'stats-grid usage-stats');
  for (const [label, value] of [
    ['已报告输入 tokens', quantity(record.input_tokens)], ['已报告输出 tokens', quantity(record.output_tokens)],
    ['已报告缓存命中 tokens', quantity(record.cache_hit_tokens)], ['已报告缓存未命中 tokens', quantity(record.cache_miss_tokens)],
    ['输入缓存命中率', cacheRate(record)], ['预计人民币费用', costLabel(record)]
  ]) grid.append(metric(doc, label, value));
  container.append(grid, node(doc, 'p', partialNote(record), 'muted usage-note'),
    node(doc, 'small', '命中率按已报告输入统计，部分已知会单独标注；费用是估算，不是平台账单。工具步骤中的多次模型请求计入其所在轮次，不重复加到其他轮。'));
}

export function renderCaptureInfo(container, capture, timezone) {
  const doc = container.ownerDocument;
  container.replaceChildren();
  const info = capture && typeof capture === 'object' ? capture : {};
  container.append(node(doc, 'p', `账本首次采集：${usageTime(info.first_recorded_at, timezone)} · 最近采集：${usageTime(info.last_recorded_at, timezone)}`, 'muted'),
    node(doc, 'p', `最早已记录请求：${usageTime(info.earliest_request_at, timezone)} · 最晚已记录请求：${usageTime(info.latest_request_at, timezone)}`, 'muted'),
    node(doc, 'p', `已保存 ${quantity(info.record_count)} 条请求用量记录；实时 ${quantity(info.live_records)} 条，历史补采 ${quantity(info.history_records)} 条。`, 'muted'),
    node(doc, 'small', typeof info.coverage_note === 'string' ? info.coverage_note : '只包含已采集的宿主结算记录；没有保留 usage 的历史无法反推。底层隐藏重试及缺失用量不代表免费。'));
}

function requestTable(doc, requests, timezone) {
  const wrapper = node(doc, 'div', '', 'usage-table-wrap');
  wrapper.setAttribute('tabindex', '0');
  wrapper.setAttribute('aria-label', '本轮每次请求用量，窄屏可横向滚动');
  const table = node(doc, 'table', '', 'usage-table');
  const header = node(doc, 'tr');
  for (const title of ['时间 / 模型', '输入 / 输出', '缓存命中 / 未命中', '命中率', '预计费用 / 时段', '记录依据']) {
    const th = node(doc, 'th', title); th.setAttribute('scope', 'col'); header.append(th);
  }
  const thead = node(doc, 'thead'); thead.append(header); table.append(thead);
  const body = node(doc, 'tbody');
  for (const request of requests) {
    const row = node(doc, 'tr');
    const identity = node(doc, 'td');
    identity.append(node(doc, 'b', text(request.model)), node(doc, 'small', usageTime(request.started_at, timezone)),
      node(doc, 'small', '结束 ' + usageTime(request.ended_at, timezone)), node(doc, 'small', `提供方 ${text(request.provider)} · 结果 ${text(request.outcome)}`),
      node(doc, 'small', text(request.request_id), 'mono'));
    const tokens = node(doc, 'td'); tokens.append(node(doc, 'div', '输入 ' + quantity(request.input_tokens)), node(doc, 'div', '输出 ' + quantity(request.output_tokens)), node(doc, 'small', '合计 ' + quantity(request.total_tokens)));
    const cache = node(doc, 'td'); cache.append(node(doc, 'div', '命中 ' + quantity(request.cache_hit_tokens)), node(doc, 'div', '未命中 ' + quantity(request.cache_miss_tokens)));
    const cost = node(doc, 'td'); cost.append(node(doc, 'b', costLabel(request)), node(doc, 'small', periodLabel(request.price_period)));
    if (typeof request.price_note === 'string' && request.price_note) cost.append(node(doc, 'small', request.price_note));
    const basis = node(doc, 'td');
    basis.append(node(doc, 'small', '捕获 ' + text(request.capture_mode)), node(doc, 'small', '时间 ' + text(request.time_basis)), node(doc, 'small', '用量 ' + text(request.normalization_basis)));
    row.append(identity, tokens, cache, node(doc, 'td', cacheRate(request)), cost, basis); body.append(row);
  }
  table.append(body); wrapper.append(table); return wrapper;
}

export function renderUsageTurns(container, turns, { append = false, timezone } = {}) {
  const doc = container.ownerDocument;
  if (!append) container.replaceChildren();
  const rows = Array.isArray(turns) ? turns : [];
  if (!rows.length && !append) {
    const empty = node(doc, 'div', '', 'empty');
    empty.append(node(doc, 'h3', '这个范围还没有对话用量记录'), node(doc, 'p', '没有记录不等于用量或费用为零。正常聊天后刷新，或等待可恢复的历史 usage 补采。'));
    container.append(empty); return 0;
  }
  const existing = new Set(Array.from(container.children).map(child => child.dataset?.usageKey).filter(Boolean));
  for (const turn of rows) {
    const key = JSON.stringify([turn.conversation_id, turn.turn_id]);
    if (existing.has(key)) continue;
    existing.add(key);
    const detail = node(doc, 'details', '', 'usage-turn'); detail.dataset.usageKey = key;
    const summary = node(doc, 'summary');
    const identity = node(doc, 'span', '', 'usage-turn-identity');
    identity.append(node(doc, 'b', `第 ${text(turn.turn_id)} 轮`), node(doc, 'span', text(turn.conversation_id), 'mono muted'));
    const models = Array.isArray(turn.models) ? turn.models.filter(x => typeof x === 'string') : [];
    const brief = node(doc, 'span', '', 'usage-turn-brief');
    brief.append(node(doc, 'span', `${quantity(turn.request_count)} 次请求 · 输入 ${quantity(turn.input_tokens)} / 输出 ${quantity(turn.output_tokens)}`),
      node(doc, 'b', costLabel(turn)), node(doc, 'span', periodLabel(turn.price_period), 'badge muted'));
    summary.append(identity, brief); detail.append(summary,
      node(doc, 'p', `${usageTime(turn.started_at, timezone)} — ${usageTime(turn.ended_at, timezone)} · 模型：${models.length ? models.join('、') : '未报告'}`, 'muted usage-turn-meta'));
    const facts = node(doc, 'div', '', 'usage-turn-values');
    facts.append(node(doc, 'span', '缓存命中 ' + quantity(turn.cache_hit_tokens)), node(doc, 'span', '缓存未命中 ' + quantity(turn.cache_miss_tokens)),
      node(doc, 'span', '命中率 ' + cacheRate(turn)), node(doc, 'span', '合计 ' + quantity(turn.total_tokens) + ' tokens'));
    detail.append(facts, node(doc, 'small', partialNote(turn)));
    const requests = Array.isArray(turn.requests) ? turn.requests : [];
    const total = countKnown(turn.requests_total) ? turn.requests_total : null;
    const shown = countKnown(turn.requests_shown) ? turn.requests_shown : requests.length;
    if (total !== null && shown < total) detail.append(node(doc, 'p', `本轮汇总包含全部 ${quantity(total)} 次请求；这里只展示 ${quantity(shown)} 次明细，剩余明细未展开。`, 'notice'));
    if (requests.length) {
      const perRequest = node(doc, 'details', '', 'usage-requests');
      perRequest.append(node(doc, 'summary', '查看每次模型请求（含工具步骤）'), requestTable(doc, requests, timezone)); detail.append(perRequest);
    } else detail.append(node(doc, 'p', '本轮尚未返回逐请求明细，汇总不据此归零。', 'muted'));
    container.append(detail);
  }
  return Array.from(container.children).filter(child => child.dataset?.usageKey).length;
}

export function renderPriceInfo(container, info) {
  const doc = container.ownerDocument;
  container.replaceChildren();
  if (!info || typeof info !== 'object') { container.append(node(doc, 'p', '服务未返回价格依据；不按未知模型猜费用。', 'muted')); return; }
  container.append(node(doc, 'p', `估价模型：${text(info.model)} · 币种：${text(info.currency)} · 单位：${text(info.unit)}`, 'muted'),
    node(doc, 'p', `时区：${text(info.timezone)} · 标准价时段：${typeof info.peak_hours === 'string' ? info.peak_hours : '以服务定价信息为准'}`, 'muted'));
  const list = node(doc, 'div', '', 'usage-price-grid');
  for (const [key,label] of [['peak','标准价时段'],['off_peak','闲时价']]) {
    const rate = info.rates?.[key] || {}, row = node(doc, 'div', '', 'extension-row');
    row.append(node(doc, 'b', label), node(doc, 'p', `缓存命中 ${money(rate.cache_hit)} · 缓存未命中 ${money(rate.cache_miss)} · 输出 ${money(rate.output)} / 百万 tokens`)); list.append(row);
  }
  container.append(list);
  for (const value of [info.billing_note, info.holiday_note]) if (typeof value === 'string' && value) container.append(node(doc, 'small', value));
  const source = node(doc, 'p', '', 'usage-price-source muted');
  source.append(node(doc, 'span', '价格核对时间：' + usageTime(info.verified_at, info.timezone) + ' '));
  try {
    const url = new URL(info.source_url);
    if (['https:','http:'].includes(url.protocol) && !url.username && !url.password) {
      const link = node(doc, 'a', '查看价格来源'); link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer'; source.append(link);
    }
  } catch { /* Unknown source remains unlinked; never execute a supplied URL. */ }
  container.append(source, node(doc, 'small', '估费不是账单；跨价格档或结算时点无法确定时给出区间，未知请求不会被当作免费。'));
}

export function renderUsageFailure(container, missing = false) {
  const doc = container.ownerDocument;
  container.replaceChildren(node(doc, 'div', missing ? '当前内核尚未提供对话用量接口。后台整理统计仍可单独查看；对话用量不是 0。' : '暂时没有取得对话用量。请确认本机服务后重试；缺失记录不能按 0 计算。', 'notice'));
}
