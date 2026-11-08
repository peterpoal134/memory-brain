const $ = selector => document.querySelector(selector);
const TOKEN_KEY = 'xiaojiu.management.token';
let token = sessionStorage.getItem(TOKEN_KEY) || '', currentPage = 'overview', messageOffset = 0, latestStatus = null;
let authenticated = false, authEpoch = 0, authTask = null, refreshTask = null;
const titles = { overview: '记忆总览', messages: '原话与笔记', memories: '记忆管理', recall: '检索与出处', maintenance: '维护与备份', extensions: '自我与情绪', life: '生活', profile: '角色与约定' };
function panelDestination(search = location.search) {
  const params = new URLSearchParams(search), requested = params.get('page');
  const page = Object.hasOwn(titles, requested) ? requested : 'overview';
  return { page, kind: ['raw', 'summary', 'fact'].includes(params.get('kind')) ? params.get('kind') : '',
    status: ['active', 'archived'].includes(params.get('status')) ? params.get('status') : '' };
}
async function enterPanel() {
  await refresh();
  if (!authenticated) throw new Error('登录状态已变化');
  const destination = panelDestination();
  if (destination.page === 'memories') {
    $('#memory-filter').elements.kind.value = destination.kind;
    $('#memory-filter').elements.status.value = destination.status;
  }
  $('#connecting').hidden = true; $('#login').hidden = true; $('#app').hidden = false;
  await navigate(destination.page);
}
const settingLabels = { capture_enabled: '允许原话接收', recall_enabled: '允许记忆检索', semantic_enabled: '启用语义检索', derive_enabled: '后台整理与抽取', decay_enabled: '派生记忆保留策略', cross_conversation: '同一所有者的跨会话检索', emotion_enabled: '情绪信号扩展', self_notes_enabled: '自我笔记扩展', life_enabled: '启用本地生活层', life_context_enabled: '生活状态带入当次聊天' };
function el(tag, text = '', cls = '') { const n = document.createElement(tag); n.textContent = text; if (cls) n.className = cls; return n; }
function empty(container, title = '这里还没有记录', hint = '正常聊天或添加一条笔记后，数据会出现在这里。') { const n = el('div', '', 'empty'); n.append(el('span', '◇'), el('h3', title), el('p', hint)); container.replaceChildren(n); }
function badge(text, cls = 'muted') { return el('span', text, 'badge ' + cls); }
function recordType(record) {
  const kind = record?.metadata?.source_kind;
  if (kind === 'imported_memory') return '导入笔记 · 原说话者未核实';
  if (kind === 'owner_note') return '用户手写笔记';
  return record.role === 'assistant' ? '助手' : '用户';
}
function button(text, action, cls = 'quiet') { const b = el('button', text, cls); b.type = 'button'; b.addEventListener('click', action); return b; }
function date(value) { if (value === undefined || value === null) return '时间未知'; const d = new Date(typeof value === 'number' ? value * 1000 : value); return Number.isNaN(d.getTime()) ? '时间未知' : d.toLocaleString('zh-CN', { hour12: false }); }
function listOf(data, key) { return Array.isArray(data) ? data : (data?.items || data?.[key] || []); }
function notice(text, error = false) { const n = $('#notice'); n.hidden = !text; n.textContent = text; n.className = 'notice' + (error ? ' error' : ''); }
let toastTimer;
function toast(text) { const n = $('#toast'); n.textContent = text; n.hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => { n.hidden = true; }, 3500); }
async function api(path, { method = 'GET', body } = {}) {
  const requestEpoch = authEpoch;
  const ac = new AbortController(), timer = setTimeout(() => ac.abort(), path === '/v1/backup' ? 120000 : 12000);
  try {
    const r = await fetch(path, { method, signal: ac.signal, redirect: 'error', credentials: 'same-origin',
      headers: { ...(token ? { Authorization: 'Bearer ' + token } : {}), ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) }, body: body === undefined ? undefined : JSON.stringify(body) });
    const payload = await r.json().catch(() => null);
    if (requestEpoch !== authEpoch) throw new Error('登录状态已变化，请重新检查');
    if (!r.ok || payload?.ok !== true) {
      if (r.status === 401 && authenticated) showLogin(false);
      throw new Error(typeof payload?.error === 'string' ? payload.error : (payload?.error?.message || `请求未完成（${r.status}）`));
    }
    return payload.data;
  } catch (e) { if (e.name === 'AbortError') throw new Error('服务响应超时，请检查本机服务状态后重试。'); throw e; }
  finally { clearTimeout(timer); }
}
async function busy(target, fn) {
  if (target) target.disabled = true;
  try { return await fn(); } catch (e) { notice(e.message || '操作未完成', true); }
  finally { if (target) target.disabled = false; }
}
function scrubPrivateView() {
  latestStatus = null; messageOffset = 0;
  for (const id of ['stats', 'provider-state', 'overview-jobs', 'maintenance-jobs', 'model-usage', 'recent-messages', 'messages-list', 'memories-list', 'recall-result', 'evidence-content', 'backup-result', 'settings-list', 'extensions-list', 'self-notes', 'conversation-options', 'life-snapshot', 'life-events', 'life-tasks', 'life-books', 'life-journal', 'life-journal-meta', 'life-state-error', 'life-rules', 'emotion-live-summary', 'emotion-rules', 'emotion-events', 'emotion-event-result', 'profile-summary', 'profile-host', 'profile-self', 'profile-important']) $('#' + id)?.replaceChildren();
  for (const node of document.querySelectorAll('#app textarea, #app input[type=text], #app input:not([type])')) node.value = '';
  for(const id of ['standards-items','standards-history','profile-context-result'])$('#'+id)?.replaceChildren();
  $('#evidence-dialog').close(); $('#toast').hidden = true; notice('');
  $('#build-id').textContent = ''; $('#last-refresh').textContent = ''; $('#message-count').textContent = '';
  $('#connection').textContent = '尚未登录'; $('#connection').className = 'badge muted';
}
function showLogin(show = true) { authenticated = false; authEpoch++; token = ''; sessionStorage.removeItem(TOKEN_KEY); scrubPrivateView(); $('#connecting').hidden = true; $('#app').hidden = true; $('#login').hidden = false; $('#management-token').value = ''; if (show) $('#login-error').textContent = ''; }
async function logout() {
  const buttons = [$('#logout'), $('#logout-top')]; buttons.forEach(b => { b.disabled = true; });
  try { await api('/v1/session/logout', { method: 'POST', body: {} }); showLogin(); $('#login-error').textContent = '已退出此浏览器的管理登录。需要时可从桌面入口重新打开。'; }
  catch (e) { if (authenticated) notice('暂未能退出，请重试；登录尚未撤销。', true); }
  finally { buttons.forEach(b => { b.disabled = false; }); }
}
async function navigate(page) {
  if (!authenticated) return;
  if (!Object.hasOwn(titles, page)) page = 'overview';
  currentPage = page; $('#page-title').textContent = titles[page]; notice('');
  for (const n of document.querySelectorAll('.page')) n.hidden = n.id !== 'page-' + page;
  for (const n of document.querySelectorAll('.nav')) n.classList.toggle('active', n.dataset.page === page);
  await busy(null, async () => {
    if (page === 'messages') { messageOffset = 0; await messages(false); }
    if (page === 'memories') await memories();
    if (page === 'maintenance') await settings();
    if (page === 'extensions') await extensions();
    if (page === 'profile') await profile();
    if (page === 'life') await life();
  });
}
function stateRows(container, object, labels = {}) {
  container.replaceChildren();
  if (!object || !Object.keys(object).length) return empty(container, '暂时没有状态数据', '刷新后查看服务返回的实际状态。');
  for (const [key, value] of Object.entries(object)) {
    const row = el('div', '', 'status-line'); row.append(el('b', labels[key] || key));
    row.append(el('span', typeof value === 'object' ? JSON.stringify(value) : String(value), 'muted mono')); container.append(row);
  }
}
function renderModelUsage(container, usage) {
  if (!container) return;
  if (!usage || typeof usage !== 'object' || Array.isArray(usage)) return empty(container, '暂无后台整理用量记录', '服务尚未返回这项统计；没有记录不代表用量为零。');
  container.replaceChildren();
  const known = value => Number.isInteger(value) && value >= 0;
  const fields = [ ['attempts', '整理尝试次数'], ['reported_responses', '已报告用量的响应'],
    ['input_tokens', '已报告输入 tokens'], ['output_tokens', '已报告输出 tokens'],
    ['cache_hit_tokens', '已报告缓存命中 tokens'], ['cache_miss_tokens', '已报告缓存未命中 tokens'],
    ['unknown_usage_attempts', '未报告用量的尝试'] ];
  const grid = el('div', '', 'stats-grid');
  for (const [key, label] of fields) {
    const cell = el('div', '', 'stat'); cell.append(el('span', label, 'label'),
      el('div', known(usage[key]) ? usage[key].toLocaleString('zh-CN') : '未报告', 'value')); grid.append(cell);
  }
  container.append(grid);
  const unknown = usage.unknown_usage_attempts;
  container.append(el('p', known(unknown) ? (unknown > 0 ? `另有 ${unknown.toLocaleString('zh-CN')} 次尝试未报告用量，不能按 0 计算；tokens 仅统计已报告部分。` : 'tokens 来自服务已收到的模型用量记录；旧版本没有记录的调用不会补估。') : '未报告用量的尝试数尚未知；缺失字段不按 0 计算。', 'muted'));
  if (usage.by_outcome && typeof usage.by_outcome === 'object' && !Array.isArray(usage.by_outcome)) {
    const entries = Object.entries(usage.by_outcome).filter(([, value]) => known(value));
    if (entries.length) {
      const detail = el('details'), outcome = el('div'); detail.append(el('summary', '查看整理尝试的处理结果'));
      stateRows(outcome, Object.fromEntries(entries), { success: '成功', failed: '失败', error: '错误',
        empty: '无派生项', no_facts: '无派生项', invalid_json: '结果解析失败',
        length: '输出截断', lease_lost: '处理权已转交' }); detail.append(outcome); container.append(detail);
    }
  }
}
function refresh() {
  if (!authenticated) return Promise.reject(new Error('请先从桌面入口打开或重新检查登录'));
  if (refreshTask) return refreshTask;
  refreshTask = refreshData().finally(() => { refreshTask = null; }); return refreshTask;
}
async function refreshData() {
  const status = await api('/v1/status'); latestStatus = status;
  $('#connection').textContent = '服务已连接'; $('#connection').className = 'badge good';
  $('#build-id').textContent = status.build_id ? '构建 ' + status.build_id : '';
  const stats = status.stats || {}, jobs = status.jobs || {};
  const values = [ ['对话原话', stats.original_messages ?? stats.messages], ['导入旧笔记', stats.imported_notes ?? 0], ['派生记忆', stats.memories ?? 0], ['手写笔记', stats.owner_notes ?? 0], ['待处理任务', jobs.pending ?? jobs.by_status?.pending], ['失败任务', jobs.failed ?? jobs.by_status?.failed] ];
  $('#stats').replaceChildren(...values.map(([label, value]) => { const c = el('div', '', 'stat'); c.append(el('span', label, 'label'), el('div', value === undefined ? '—' : Number(value).toLocaleString('zh-CN'), 'value')); return c; }));
  const ps = $('#provider-state'); ps.replaceChildren();
  const providers = status.providers || {};
  if (!Object.keys(providers).length) empty(ps, '未返回模型状态', '已配置不等于已验证连通，实际降级以检索结果为准。');
  for (const [key, value] of Object.entries(providers)) {
    const row = el('div', '', 'status-line'), detail = el('div');
    const p = typeof value === 'object' && value ? value : { configured: Boolean(value) };
    detail.append(el('b', ({ embedding: '语义索引', embedder: '语义索引', llm: '记忆整理', derivation: '记忆整理', reranker: '候选重排' })[key] || key));
    if (p.model) detail.append(el('small', p.model));
    row.append(detail, badge(p.configured ?? p.enabled ? '已配置' : '未配置', p.configured ?? p.enabled ? 'good' : 'warn')); ps.append(row);
  }
  const jobLabels = { pending: '等待处理', running: '处理中', done: '已完成', failed: '失败待查', retry: '等待重试', waiting_provider: '等待配置模型', blocked_policy: '策略暂停', exhausted: '重试已耗尽' };
  const jobValues = Object.fromEntries(Object.keys(jobLabels).filter(k => typeof jobs[k] === 'number').map(k => [k, jobs[k]]));
  stateRows($('#overview-jobs'), jobValues, jobLabels);
  stateRows($('#maintenance-jobs'), jobValues, jobLabels);
  renderModelUsage($('#model-usage'), status.model_usage);
  if (jobs.recent_errors?.length) {
    const detail = el('details'); detail.append(el('summary', '查看最近的任务异常'));
    for (const item of jobs.recent_errors) detail.append(el('p', `任务 #${item.id} · ${item.kind} · ${item.error} · ${date(item.updated_at)}`, 'mono muted'));
    $('#maintenance-jobs').append(detail);
  }
  const data = await api('/v1/messages?limit=4'); renderMessages($('#recent-messages'), listOf(data, 'messages'), false);
  $('#last-refresh').textContent = '最近刷新 ' + new Date().toLocaleTimeString('zh-CN', { hour12: false });
  await conversations();
  if (currentPage === 'life') await life();
}
async function conversations() {
  try {
    const data = await api('/v1/conversations'); let dl = $('#conversation-options');
    if (!dl) { dl = el('datalist'); dl.id = 'conversation-options'; document.body.append(dl); }
    dl.replaceChildren(...listOf(data).map(c => { const o = el('option', `${c.message_count || 0} 条原话`); o.value = c.id; return o; }));
    for (const input of document.querySelectorAll('input[name=conversation_id]')) input.setAttribute('list', dl.id);
  } catch { /* optional convenience; manual IDs still work */ }
}
function renderMessages(container, records, allowDelete = true, append = false) {
  if (!append) container.replaceChildren();
  if (!records.length && !append) return empty(container, '还没有原话记录', '启用聊天适配器后，可见消息会按顺序归档。');
  for (const r of records) {
    const row = el('article', '', 'message'), head = el('div', '', 'row-head');
    head.append(el('span', recordType(r), 'role ' + (r.role === 'assistant' ? 'assistant' : '')), el('span', '#' + r.id, 'mono muted'), el('span', date(r.recorded_at), 'time'));
    const meta = el('div', '', 'meta'); meta.append(el('span', r.conversation_id || '', 'mono'));
    if (allowDelete) meta.append(button('删除', () => busy(null, async () => { if (!confirm('删除这条原话及依赖它的索引与派生记忆？此操作不可撤销。')) return; await api('/v1/messages/' + encodeURIComponent(r.id), { method: 'DELETE' }); await messages(false); toast('原话及关联数据已删除'); }), 'quiet danger'));
    row.append(head, el('div', r.text || '', 'body-text'), meta); container.append(row);
  }
}
async function messages(append = false) {
  if (!append) messageOffset = 0;
  const conv = new FormData($('#message-filter')).get('conversation_id');
  const p = new URLSearchParams({ limit: '30', offset: String(messageOffset) }); if (conv) p.set('conversation_id', conv);
  const data = await api('/v1/messages?' + p), records = listOf(data, 'messages');
  renderMessages($('#messages-list'), records, true, append); messageOffset += records.length;
  $('#message-count').textContent = `已显示 ${messageOffset} 条` + (data.total !== undefined ? ` · 共 ${data.total} 条` : '');
  $('#messages-more').hidden = data.total !== undefined ? messageOffset >= data.total : records.length < 30;
}
function sourceButtons(container, refs) {
  if (!Array.isArray(refs) || !refs.length) { container.append(el('small', '暂无来源指针')); return; }
  for (const ref of refs) container.append(button('来源 #' + ref.message_id, () => busy(null, () => evidence(ref)), 'evidence-tag'));
}
async function evidence(ref) {
  const record = await api('/v1/evidence/' + encodeURIComponent(ref.message_id));
  const c = $('#evidence-content'); c.replaceChildren();
  c.append(el('p', `${recordType(record)} · ${date(record.recorded_at)} · 来源 #${record.id}`, 'muted'));
  c.append(el('p', record.conversation_id || '', 'mono muted'));
  const text = String(record.text || ''), p = el('div', '', 'body-text');
  // Source offsets are Unicode code points, as used by the Python store.
  const points = Array.from(text), start = ref.start, end = ref.end;
  if (Number.isInteger(start) && Number.isInteger(end) && start >= 0 && end <= points.length && end >= start) p.append(document.createTextNode(points.slice(0, start).join('')), el('mark', points.slice(start, end).join('')), document.createTextNode(points.slice(end).join('')));
  else p.textContent = text;
  c.append(p); $('#evidence-dialog').showModal();
}
async function memories() {
  const form = new FormData($('#memory-filter')), params = new URLSearchParams({ limit: '100' });
  for (const k of ['kind', 'status']) if (form.get(k)) params.set(k, form.get(k));
  const data = await api('/v1/memories?' + params), container = $('#memories-list'), records = listOf(data, 'memories'); container.replaceChildren();
  if (!records.length) return empty(container, '当前筛选下没有记忆', '原话索引会立即建立，摘要与事实整理按配置在后台运行。');
  for (const r of records) {
    const row = el('article', '', 'memory'), head = el('div', '', 'row-head');
    head.append(badge(r.metadata?.source_kind === 'imported_memory' && r.kind === 'raw' ? '导入笔记' : (({ raw: '原话片段', summary: '摘要', fact: '事实' })[r.kind] || r.kind)), el('span', r.id, 'mono muted'), el('span', date(r.recorded_at), 'time'));
    if (r.pinned) head.append(badge('已置顶', 'good'));
    if (r.status === 'archived') head.append(badge('已归档'));
    if (r.disclosure !== 'normal') head.append(badge(r.disclosure === 'never_say' ? '不注入聊天' : '仅明确问及时使用', 'warn'));
    row.append(head, el('div', r.text || '', 'body-text')); const sources = el('div'); sourceButtons(sources, r.source_refs); row.append(sources);
    const actions = el('div', '', 'memory-actions'), weight = el('input'); weight.type = 'number'; weight.min = '0'; weight.max = '1'; weight.step = '.05'; weight.value = String(r.importance ?? .5); weight.setAttribute('aria-label', '记忆重要性');
    const patch = async body => { await api('/v1/memories/' + encodeURIComponent(r.id), { method: 'PATCH', body }); await memories(); toast('记忆已更新'); };
    actions.append(weight, button('保存重要性', () => busy(null, () => patch({ importance: Number(weight.value) })), 'secondary'), button(r.pinned ? '取消置顶' : '置顶', () => busy(null, () => patch({ pinned: !r.pinned })), 'secondary'), button(r.status === 'archived' ? '恢复使用' : '归档', () => busy(null, () => patch({ status: r.status === 'archived' ? 'active' : 'archived' })), 'secondary'));
    if (r.kind !== 'raw') actions.append(button('删除', () => busy(null, async () => { if (!confirm('删除这条派生记忆和它的索引？原始记录仍然保留。')) return; await api('/v1/memories/' + encodeURIComponent(r.id), { method: 'DELETE' }); await memories(); toast('派生记忆已删除'); }), 'danger'));
    else actions.append(el('small', '删除原话请到“原话记录”，会同时移除依赖它的记忆。'));
    row.append(actions); container.append(row);
  }
}
function renderRecall(data) {
  const container = $('#recall-result'); container.replaceChildren();
  if (data.degraded?.length) { const n = el('div', '本次检索处于降级状态：' + data.degraded.join('；'), 'notice'); container.append(n); }
  if (data.coverage_gaps?.length) container.append(el('div', '证据缺口：' + data.coverage_gaps.map(x => typeof x === 'string' ? x : JSON.stringify(x)).join('；'), 'notice'));
  if (!data.evidence?.length) { const holder = el('div'); empty(holder, '没有找到足够的相关证据', '可以换一种自然问法，或检查会话范围、原话是否已到达和模型配置。'); container.append(holder); }
  for (const r of data.evidence || []) {
    const card = el('article', '', 'card'), head = el('div', '', 'row-head');
    head.append(badge(({ raw: '原话证据', fact: '派生事实', summary: '摘要线索' })[r.kind] || r.kind, 'good'), el('span', r.document_id || '', 'mono muted'), el('span', date(r.recorded_at), 'time'));
    card.append(head, el('div', r.text || '', 'body-text')); const sources = el('div'); sourceButtons(sources, r.source_refs); card.append(sources); container.append(card);
  }
  if (data.conflicts?.length) { const c = el('article', '', 'card'); c.append(el('h2', '存在待解释的冲突'), el('p', '这些证据被并列保留，没有自动选取最新一条。', 'muted')); c.append(el('pre', JSON.stringify(data.conflicts, null, 2), 'trace')); container.append(c); }
  const detail = el('details', '', 'card'); detail.append(el('summary', '查看本次检索轨迹'), el('pre', JSON.stringify(data.trace || {}, null, 2), 'trace')); container.append(detail);
}
async function downloadData(url, filename) {
  const requestEpoch = authEpoch;
  const target = new URL(url, location.origin);
  if (target.origin !== location.origin || !(target.pathname.startsWith('/v1/backups/') || target.pathname === '/v1/export')) throw new Error('文件下载地址不属于当前服务。');
  const r = await fetch(target, { headers: token ? { Authorization: 'Bearer ' + token } : {}, credentials: 'same-origin', redirect: 'error' });
  if (requestEpoch !== authEpoch) throw new Error('登录状态已变化');
  if (!r.ok) { if (r.status === 401 && authenticated) showLogin(false); throw new Error('文件下载失败'); }
  const blob = await r.blob(); if (requestEpoch !== authEpoch) throw new Error('登录状态已变化'); saveBlob(blob, filename);
}
function publicDownloadName(value, fallback) { return String(value || fallback).replace(/xiaojiu/gi, 'memory-brain'); }
function saveBlob(blob, filename) { const u = URL.createObjectURL(blob), a = document.createElement('a'); a.href = u; a.download = filename; document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(u), 30000); }
async function settings() {
  const state = await api('/v1/settings'), container = $('#settings-list'); container.replaceChildren();
  for (const [key, value] of Object.entries(state)) {
    if (typeof value !== 'boolean') continue;
    const row = el('div', '', 'switch-row'), label = el('label', settingLabels[key] || key), input = el('input');
    input.type = 'checkbox'; input.checked = value; input.setAttribute('aria-label', settingLabels[key] || key);
    if (key === 'cross_conversation') label.append(el('small', '默认关闭；开启后允许检索同一所有者的其他会话。'));
    if (key === 'life_context_enabled') label.append(el('small', '只控制当次聊天状态段，不停止本地生活检查或删除记录。'));
    input.addEventListener('change', () => busy(input, async () => { await api('/v1/settings', { method: 'PATCH', body: { [key]: input.checked } }); toast('设置已保存'); })); row.append(label, input); container.append(row);
  }
  const numeric = { token_budget: ['默认证据预算（tokens）', 128, 8192, 1], derive_interval: ['整理检查间隔（秒）', 1, 600, .1], derive_batch_messages: ['每批整理消息数（最多 12 条）', 1, 12, 1], derive_max_wait_seconds: ['最长积累时间（秒）', 30, 86400, 1], emotion_half_life_hours: ['统一情绪半衰期（兼容设置，覆盖三轴）', .1, 720, .1], decay_rate_per_day: ['派生记忆每日衰减率', 0, 1, .005], archive_after_days: ['满足归档条件的最短天数', 1, 36500, 1], archive_below: ['派生记忆归档分值下限', 0, 1, .01] };
  const hints = { emotion_half_life_hours: '修改此兼容选项会统一三轴节奏。分别调整请前往自我与情绪。', derive_interval: '只控制后台整理何时检查待处理消息，不控制向量索引更新。',
    derive_batch_messages: '计消息，不是回合；12 条通常约 6 轮。凑够批量或达到最长积累时间后才调用整理模型。',
    derive_max_wait_seconds: '默认 900 秒（15 分钟）；不足一批时，在后续检查中达到该时限也可开始整理。' };
  for (const [key, [labelText, min, max, step]] of Object.entries(numeric)) {
    if (typeof state[key] !== 'number') continue;
    const row = el('div', '', 'switch-row'), label = el('label', labelText), input = el('input');
    input.type = 'number'; input.min = String(min); input.max = String(max); input.step = String(step); input.value = String(state[key]); input.className = 'setting-number'; input.setAttribute('aria-label', labelText);
    if (hints[key]) label.append(el('small', hints[key]));
    input.addEventListener('change', () => busy(input, async () => { if (!input.checkValidity()) { input.reportValidity(); return; } await api('/v1/settings', { method: 'PATCH', body: { [key]: Number(input.value) } }); toast('设置已保存'); }));
    row.append(label, input); container.append(row);
  }
  const ext = await api('/v1/extensions'), list = $('#extensions-list'); list.replaceChildren();
  for (const item of listOf(ext)) {
    const row = el('div', '', 'extension-row'), head = el('div', '', 'row-head'); head.append(el('b', item.title || item.id), badge(item.implemented ? (item.enabled ? '已启用' : '已实现 · 关闭') : '预留 · 未实现', item.implemented && item.enabled ? 'good' : 'muted')); row.append(head, el('p', item.description || ''));
    list.append(row);
  }
}
const lifeKindLabels = { task_completed: '首次完成待办', owner_note: '你手工记录', routine_simulation: '作息规则生成 · 模拟',
  book_excerpt: '本地文本原文摘录', chat_observed: '有来源的聊天活动记录', scheduled_tick: '生活检查' };
function lifeSource(record) {
  const source = record?.source;
  const kind = typeof source === 'string' ? source : source?.kind;
  let label = lifeKindLabels[kind] || (kind ? String(kind) : '来源未报告');
  if (source && typeof source === 'object' && source.book_id) label += ' · 书籍 #' + source.book_id;
  if (source && typeof source === 'object' && Number.isInteger(source.start) && Number.isInteger(source.end)) label += ` · 字符 ${source.start}–${source.end}`;
  if (source?.model_understanding === false) label += ' · 未做模型理解';
  return label;
}
function lifeAction(action) {
  if (!action || typeof action !== 'object') return '尚无记录';
  return (lifeKindLabels[action.kind] || action.kind || '动作') + ' · ' + date(action.at);
}
function renderLifeSnapshot(snapshot) {
  const enabled = $('#life-enabled');
  enabled.textContent = typeof snapshot.enabled === 'boolean' ? (snapshot.enabled ? '已启用本地检查' : '生活层已关闭') : '启用状态未报告';
  enabled.className = 'badge ' + (snapshot.enabled === true ? 'good' : 'muted');
  const state = snapshot.rhythm || {}, counts = snapshot.counts || {};
  const phase = state.phase ?? snapshot.state?.activity;
  const rows = { '规则作息（模拟）': phase === 'awake' ? '清醒时段 · 不是真实经历' : phase === 'rest' ? '休息时段 · 不是真实睡眠' : '未报告',
    '服务时区': snapshot.timezone || '未报告',
    '最近一次动作': lifeAction(snapshot.last_action),
    '下一次动作': snapshot.enabled === false ? '已关闭，不安排检查' : lifeAction(snapshot.next_action),
    '最近检查时刻': snapshot.last_tick_at == null ? '尚未检查' : date(snapshot.last_tick_at) };
  for (const [key, label] of [['energy', '精力（模拟）'], ['boredom', '无聊（模拟）'], ['social', '社交需求（模拟）'], ['curiosity', '好奇活跃度（模拟）']]) {
    const value = snapshot.needs?.[key];
    rows[label] = typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1 ? (value * 100).toFixed(1) + '% · 规则状态，不是真实感受' : '未报告';
  }
  for (const [key, label] of [['events', '已记录生活事件'], ['pending_tasks', '待处理待办'], ['books', '本地文本书']]) {
    rows[label] = Number.isInteger(counts[key]) && counts[key] >= 0 ? counts[key] : '未报告';
  }
  for (const [key,label] of Object.entries({energy:'精力',boredom:'无聊',social:'社交需求',curiosity:'好奇'})) { const t=snapshot.needs_trace?.dimensions?.[key]; if(t)rows[label+'变化依据']='日常基线 '+(t.base*100).toFixed(1)+'，活动修正 '+(t.activity_correction>=0?'+':'')+(t.activity_correction*100).toFixed(1)+' 点'; }
  stateRows($('#life-snapshot'), rows);
  const error = $('#life-state-error'); error.hidden = !snapshot.error;
  error.textContent = snapshot.error ? '最近的生活检查异常：' + (typeof snapshot.error === 'string' ? snapshot.error : JSON.stringify(snapshot.error)) : '';
  const form = $('#life-settings-form'), values = snapshot.settings || latestStatus?.settings || {};
  if (form.dataset.dirty !== 'true') {
    for (const key of ['life_enabled', 'life_context_enabled']) if (typeof values[key] === 'boolean') form.elements[key].checked = values[key];
    for (const key of ['life_wake_time', 'life_sleep_time', 'life_interval_seconds']) if (values[key] != null) form.elements[key].value = String(values[key]);
  }
}
function renderLifeEvents(data) {
  const container = $('#life-events'), records = listOf(data); container.replaceChildren();
  if (!records.length) return empty(container, '还没有生活事件', '手工添加，或启用后由本地规则留下带来源的记录。');
  for (const record of records) {
    const row = el('article', '', 'message'), head = el('div', '', 'row-head');
    head.append(badge(lifeSource(record)), el('span', '#' + record.id, 'mono muted'), el('span', date(record.occurred_at ?? record.created_at), 'time'));
    const meta = el('div', '', 'meta'); meta.append(el('span', record.conversation_id || '会话未报告', 'mono'));
    if (typeof record.importance === 'number' && Number.isFinite(record.importance)) meta.append(el('span', '生活层重要性 ' + record.importance));
    meta.append(button('删除记录', () => busy(null, async () => { if (!confirm('删除这条生活事件？不会修改事实记忆库。')) return; await api('/v1/life/events/' + encodeURIComponent(record.id), { method: 'DELETE' }); await life(); toast('生活事件已删除'); }), 'quiet danger'));
    row.append(head, el('div', record.text || '', 'body-text'), meta); container.append(row);
  }
}
function renderLifeTasks(data) {
  const container = $('#life-tasks'), records = listOf(data); container.replaceChildren();
  if (!records.length) return empty(container, '还没有待办', '只有你明确创建的待办才会显示在这里。');
  for (const task of records) {
    const row = el('article', '', 'message'), head = el('div', '', 'row-head');
    const labels = { pending: '待处理', done: '已完成', cancelled: '已取消' };
    head.append(el('b', task.title || '未命名待办'), badge(labels[task.status] || task.status || '状态未报告', task.status === 'done' ? 'good' : 'muted'));
    const due = task.due_at == null || task.due_at === '' ? '未设截止时间' : '截止 ' + date(task.due_at);
    row.append(head, el('p', due + ' · 会话 ' + (task.conversation_id || '未报告'), 'muted'));
    const actions = el('div', '', 'button-row');
    if (task.status === 'pending') {
      for (const [text, status] of [['标记完成', 'done'], ['取消待办', 'cancelled']]) actions.append(button(text, () => busy(null, async () => { await api('/v1/life/tasks/' + encodeURIComponent(task.id), { method: 'PATCH', body: { status } }); await life(); toast('待办状态已更新'); }), 'secondary'));
    }
    actions.append(button('删除', () => busy(null, async () => { if (!confirm('从生活层删除这条待办？')) return; await api('/v1/life/tasks/' + encodeURIComponent(task.id), { method: 'DELETE' }); await life(); toast('待办已删除'); }), 'quiet danger'));
    row.append(actions); container.append(row);
  }
}
function renderLifeBooks(data) {
  const container = $('#life-books'), books = listOf(data); container.replaceChildren();
  if (!books.length) return empty(container, '还没有本地文本书', '添加纯文本后，本地规则可推进游标并留原文摘录；不会进行模型理解。');
  for (const book of books) {
    const row = el('article', '', 'message'), head = el('div', '', 'row-head');
    head.append(el('b', book.title || '未命名文本书'), badge('程序游标，不代表理解'));
    const total = book.total_chars ?? book.characters, cursor = book.cursor;
    let detail = Number.isInteger(cursor) && Number.isInteger(total) ? `已推进 ${cursor.toLocaleString('zh-CN')} / ${total.toLocaleString('zh-CN')} 字符` : '阅读位置未报告';
    const known = typeof book.progress === 'number' && Number.isFinite(book.progress) && book.progress >= 0 && book.progress <= 1;
    if (known) detail += ` · ${(book.progress * 100).toFixed(1)}%`;
    row.append(head, el('p', detail, 'muted'));
    if (known) { const progress = el('progress'); progress.className = 'life-progress'; progress.max = 1; progress.value = book.progress; progress.setAttribute('aria-label', '本地文本游标进度'); row.append(progress); }
    row.append(button('删除文本书', () => busy(null, async () => { if (!confirm('从本地生活层删除这本文本书？')) return; await api('/v1/life/books/' + encodeURIComponent(book.id), { method: 'DELETE' }); await life(); toast('文本书已删除'); }), 'quiet danger'));
    container.append(row);
  }
}
function lifeJournalPath() {
  const input = $('#life-journal-date');
  return '/v1/life/journal' + (input.dataset.explicit === 'true' && input.value ? '?date=' + encodeURIComponent(input.value) : '');
}
function renderLifeJournal(journal) {
  $('#life-journal').textContent = typeof journal.text === 'string' ? journal.text : '服务未报告纪要内容。';
  const day = journal.date || journal.day;
  $('#life-journal-meta').textContent = (day || '日期未报告') + ' · ' + (journal.timezone || '时区未报告') + ' · 生活层确定性汇总';
  if (day && $('#life-journal-date').dataset.explicit !== 'true') $('#life-journal-date').value = day;
}
function ruleTable(headers, records) {
  const wrap=el('div','','rules-table-wrap'),table=el('table','','rules-table'),head=el('thead'),hr=el('tr'),body=el('tbody');
  for(const label of headers)hr.append(el('th',label));head.append(hr);
  for(const record of records){const row=el('tr');for(const value of record)row.append(el('td',String(value)));body.append(row);}
  table.append(head,body);wrap.append(table);return wrap;
}
const needLabels={energy:'精力',boredom:'无聊',social:'社交需求',curiosity:'好奇活跃度'};
const emotionLabels={pleasure:'愉悦度',arousal:'唤醒度',dominance:'掌控感'};
const emotionEventLabels={joy:'开心',sadness:'低落',excitement:'激动',stress:'紧张',comfort:'安慰与放松',rest:'休息'};
function signed(value,digits=2){return typeof value==='number'&&Number.isFinite(value)?(value>0?'+':'')+value.toFixed(digits):'未报告';}
function renderLifeRules(rules) {
  const box=$('#life-rules');box.replaceChildren();
  box.append(el('p',rules.engineering_note||'以下是工程模拟参数，不是统一的人类心理量表。','muted'));
  box.append(el('p','作息 '+(rules.wake_time||'—')+'–'+(rules.sleep_time||'—')+'；精力清醒时由85缓慢降到35，休息时由35恢复到85。其他状态回到随作息变化的基线。'));
  const records=[];
  for(const [kind,label] of [['chat','真人聊天'],['book','本地书摘活动'],['task','首次完成待办']]){
    const rule=rules.activities?.[kind];if(!rule)continue;
    const changes=Object.entries(needLabels).map(([key,name])=>name+' '+signed((rule.impulse?.[key]??0)*100,1)+'点').join('；');
    records.push([label,changes,(rule.half_life_hours??rule.half_life_seconds/3600)+'小时',kind==='task'?'每项仅首次完成':Math.round(rule.bucket_seconds/60)+'分钟内仅一次影响']);
  }
  box.append(ruleTable(['真实活动','即时基础影响','影响减半时间','防止重复累加'],records));
  box.append(el('p','总活动修正封顶：精力±12点，其他三项±25点，最终限制在0～100。好奇表示探索兴趣的活跃程度；阅读可以激发兴趣。助手自己的输出不算真人来访，程序待机不算阅读或完成任务。','muted'));
}
async function life() {
  const requests = [['snapshot', '/v1/life'], ['events', '/v1/life/events'], ['tasks', '/v1/life/tasks'], ['books', '/v1/life/books'], ['journal', lifeJournalPath()], ['rules', '/v1/life/rules']];
  const results = await Promise.allSettled(requests.map(([, path]) => api(path)));
  const render = { snapshot: renderLifeSnapshot, events: renderLifeEvents, tasks: renderLifeTasks, books: renderLifeBooks, journal: renderLifeJournal, rules: renderLifeRules };
  let failures = 0;
  for (let n = 0; n < results.length; n++) {
    const key = requests[n][0], result = results[n];
    if (result.status === 'fulfilled') render[key](result.value || {});
    else {
      failures++;
      if (key === 'snapshot') { $('#life-enabled').textContent = '生活状态暂不可用'; $('#life-enabled').className = 'badge warn'; }
      empty($('#life-' + (key === 'snapshot' ? 'snapshot' : key)), '暂时无法读取', '请确认当前服务支持生活层，稍后刷新；缺失数据不会显示为 0。');
    }
  }
  if (failures) notice('生活层有 ' + failures + ' 项数据暂时无法读取；其余页面可继续使用。', true);
}
async function importantSources(id) {
  const epoch=authEpoch,detail=await api('/v1/memory-map/'+encodeURIComponent(id));
  if(!authenticated||epoch!==authEpoch||currentPage!=='profile')return;
  const box=$('#evidence-content');box.replaceChildren();
  if(detail.restricted)box.append(el('p','此条记忆受限，在这里不展示正文或来源。'));
  else for(const source of detail.sources||[]){box.append(el('h3','原话 #'+source.message_id),el('small',(source.role==='user'?'用户':'助手')+' · '+date(source.recorded_at)+' · 所引片段'),el('p',source.text||'','body-text'));}
  if(!box.childNodes.length)box.append(el('p','暂未取得可展示的来源，请稍后核对。'));
  $('#evidence-dialog').showModal();
}
async function profile() {
  const epoch=authEpoch,data=await api('/v1/profile');
  if(!authenticated||epoch!==authEpoch||currentPage!=='profile')return;
  const host=data.host_persona||{},self=data.self||{},important=data.important||{};
  renderStandards(data.standards||{enabled:false,version:0,items:[],history:[]});
  const stats=$('#profile-summary');stats.replaceChildren();
  for(const [label,value] of [['登记的人格来源',host.status==='unavailable'?'—':host.sources?.length||0],['用户自我笔记',self.notes?.length||0],['重要记忆记录',important.status==='unavailable'?'—':important.total??0]]){
    const card=el('div','','stat');card.append(el('div',String(value),'value'),el('div',label,'label'));stats.append(card);
  }
  const sources=$('#profile-host');sources.replaceChildren();
  for(const source of host.sources||[]){
    const box=el('details','','profile-record'),summary=el('summary');
    summary.append(el('strong',source.title||'宿主人格来源'),badge(source.status==='unavailable'?'来源暂不可核对':source.stale?'来源已变化，快照待更新':'磁盘快照',''+(source.stale?'warn':'muted')));box.append(summary);
    box.append(el('p',source.text||'','body-text'),el('small','所属：'+(source.host||'聊天宿主')+' · '+(source.role_scope||'未注明范围')),
      el('small','来源：'+(source.source_path||'未注明'),'mono'),el('small','登记时间：'+date(source.observed_at??host.observed_at??data.observed_at)));
    sources.append(box);
  }
  if(!host.sources?.length)empty(sources,host.configured?'人格来源暂未读到':'尚未登记宿主人格来源','没有读取到不代表宿主没有人格；这里只展示明确登记的来源。');
  const own=$('#profile-self');own.replaceChildren();
  own.append(badge(self.enabled?'已允许表达笔记注入':'表达笔记注入当前关闭',self.enabled?'good':'muted'),el('p','新版适配器使用独立384估算tokens通道，完整条目超额会明确省略；此页可预览材料，但未证明当前宿主已实际请求。','muted'));
  own.append(el('h3','表达偏好'),el('p',self.style_preferences||'尚未填写表达偏好。','body-text'));
  for(const note of self.notes||[]){const row=el('div','','self-note');row.append(el('p',note.text||'','body-text'),el('small','用户填写 · '+date(note.created_at)));own.append(row);}
  if(!self.notes?.length)own.append(el('small','尚无用户填写的自我笔记。'));
  const list=$('#profile-important');list.replaceChildren();
  $('#profile-important-count').textContent=important.status==='unavailable'?'暂未读取':'最近 '+(important.items?.length||0)+' / '+(important.total??0)+' 条';
  for(const item of important.items||[]){
    const box=el('details','','profile-record'),summary=el('summary');
    summary.append(el('strong',item.restricted?'受限记忆':item.pinned?'已置顶的重要记忆':'已取消置顶的叮嘱'),badge(item.origin==='explicit_user_request'?'明确保留操作':'手工置顶'),el('small',date(item.recorded_at)));box.append(summary);
    box.append(el('p',item.preview||'','body-text'),el('small','重要性 '+Number(item.importance??0).toFixed(2)+' · 来源 '+(item.source_count??0)+' 条 · '+(item.pinned?'置顶保留':'普通保留策略')));
    if(!item.restricted)box.append(button('核对原话片段',()=>busy(null,()=>importantSources(item.id)),'secondary'));
    box.append(button(item.pinned?'取消置顶':'重新置顶',()=>busy(null,async()=>{
      await api('/v1/memories/'+encodeURIComponent(item.id),{method:'PATCH',body:{pinned:!item.pinned}});await profile();toast(item.pinned?'已取消置顶，原话保留':'已置顶');
    }),'quiet'));list.append(box);
  }
  if(!important.items?.length)empty(list,important.status==='unavailable'?'暂时无法读取重要记忆':'尚无可展示的重要记忆记录',important.status==='unavailable'?'请稍后刷新；读取失败不表示记录为零。':'以后明确要求记住时，成功保存的回执与出处会在这里出现；既有聊天原话保持不变。');
  if(important.has_more)list.append(el('small','这里只显示最近50条，可到记忆管理页继续核对。'));
}
function renderStandards(section) {
  $('#standards-version').textContent='版本 '+section.version+' · '+(section.enabled?'已启用':'未启用');
  $('#standards-toggle-form').elements.enabled.checked=section.enabled===true;
  const list=$('#standards-items');list.replaceChildren();
  for(const item of [...(section.items||[])].sort((a,b)=>b.priority-a.priority||a.id.localeCompare(b.id))){
    const box=el('details','','profile-record'),summary=el('summary');
    summary.append(el('strong',item.enabled?'已启用条目':'已停用条目'),badge('优先级 '+item.priority+' · 修订 '+item.revision));box.append(summary);
    box.append(el('p',item.text||'','body-text'),el('small',item.scope==='conversation'?'仅会话 '+item.conversation_id:'所有自己的会话'));
    const form=el('form','','grid-form'),label=el('label','修改准则'),text=el('textarea');text.name='text';text.maxLength=1000;text.required=true;text.value=item.text;label.append(text);label.className='full';form.append(label);
    const rankLabel=el('label','用户层优先级'),rank=el('input');rank.type='number';rank.name='priority';rank.min='0';rank.max='100';rank.value=item.priority;rankLabel.append(rank);form.append(rankLabel);
    const save=el('button','保存此条修订');save.type='submit';form.append(save);
    form.addEventListener('submit',event=>{event.preventDefault();busy(save,async()=>{await api('/v1/standards',{method:'PATCH',body:{update:{id:item.id,text:text.value,priority:Number(rank.value)}}});await profile();toast('准则修订已保存');});});
    box.append(form,button(item.enabled?'停用此条':'启用此条',()=>busy(null,async()=>{await api('/v1/standards',{method:'PATCH',body:{update:{id:item.id,enabled:!item.enabled}}});await profile();})),
      button('删除此条',()=>busy(null,async()=>{if(!confirm('删除此条准则？最近修改记录会保留此操作。'))return;await api('/v1/standards/'+encodeURIComponent(item.id),{method:'DELETE'});await profile();})));
    list.append(box);
  }
  if(!section.items?.length)empty(list,'尚未设置行为准则','只有你明确添加的条目才会进入这里。');
  const history=$('#standards-history');history.replaceChildren();
  for(const event of [...(section.history||[])].reverse())history.append(el('p','版本 '+event.version+' · '+date(event.at)+' · '+(event.operation==='delete'?'删除条目':'更新准则'),'muted'));
  if(!section.history?.length)history.append(el('p','尚无修改记录。','muted'));
}
$('#standards-toggle-form').addEventListener('submit',event=>{event.preventDefault();busy(event.submitter,async()=>{await api('/v1/standards',{method:'PATCH',body:{enabled:event.target.elements.enabled.checked}});await profile();toast('准则开关已保存');});});
$('#standards-add-form').addEventListener('submit',event=>{event.preventDefault();busy(event.submitter,async()=>{
  const data=new FormData(event.target),scope=data.get('scope');
  const add={text:data.get('text'),priority:Number(data.get('priority')),scope};if(scope==='conversation')add.conversation_id=String(data.get('conversation_id')).trim();
  await api('/v1/standards',{method:'PATCH',body:{enabled:true,add}});event.target.reset();await profile();toast('已添加并启用准则');
});});
$('#profile-context-form').addEventListener('submit',event=>{event.preventDefault();busy(event.submitter,async()=>{
  const result=await api('/v1/context/profile',{method:'POST',body:{conversation_id:String(new FormData(event.target).get('conversation_id')).trim()}});
  const box=$('#profile-context-result');box.replaceChildren(el('p','实际装入 '+(result.trace?.included_ids?.length||0)+' 项；省略 '+(result.trace?.omitted_count||0)+' 项；'+(result.trace?.estimated_tokens||0)+' / 384估算tokens。','muted'));
  box.append(el('pre',result.block||'当前没有可装入的角色材料。','body-text'));
  if(result.trace?.omitted_count)box.append(el('p','未装入条目标识：'+result.trace.omitted_ids.join('、'),'warning-text'));
});});
function renderEmotionRules(emotion,rules,events) {
  const summary=$('#emotion-live-summary');summary.replaceChildren();
  summary.append(el('p','0表示中性；正负表示偏离基线，不代表好坏、健康程度或事实可信度。','muted'));
  for(const [key,label] of Object.entries(emotionLabels)){
    const t=emotion.trace?.axes?.[key];if(!t)continue;
    summary.append(el('p',label+' '+signed(emotion[key])+' · 基线 '+t.baseline+' · 半衰期 '+t.half_life_hours+'小时 · 距上次状态锚点 '+Number(t.elapsed_hours).toFixed(1)+'小时'));
  }
  const form=$('#emotion-decay-form');
  if(form.dataset.dirty!=='true')for(const key of Object.keys(emotionLabels))form.elements['emotion_'+key+'_half_life_hours'].value=rules.half_life_hours?.[key]??'';
  const select=$('#emotion-event-kind'),old=select.value;select.replaceChildren();
  for(const preset of rules.presets||[]){const option=el('option',preset.label||emotionEventLabels[preset.kind]||preset.kind);option.value=preset.kind;select.append(option);}
  if([...select.options].some(option=>option.value===old))select.value=old;
  const box=$('#emotion-rules');box.replaceChildren(el('p',rules.notice||'以下为明确事件的工程模拟规则，不自动猜测聊天情绪。','muted'));
  box.append(ruleTable(['事件（强度1时）','愉悦度','唤醒度','掌控感'],(rules.presets||[]).map(p=>[p.label||p.kind,...Object.keys(emotionLabels).map(key=>signed(p.delta?.[key]))])));
  box.append(el('p','实际增量还要乘以强度，并在接近上下限时减弱。同类事件冷却 '+Math.round((rules.cooldown_seconds??300)/60)+' 分钟；保留最近 '+(rules.max_events??100)+' 条事件，此窗口内重试不会重复加值。','muted'));
  const list=$('#emotion-events');list.replaceChildren(el('h3','最近明确记录的事件'));
  for(const event of (events.items||[]).slice(0,8))list.append(el('p',(emotionEventLabels[event.kind]||event.kind||'状态事件')+' · 强度 '+event.intensity+' · '+date(event.at??event.created_at??event.occurred_at),'muted'));
  if(!events.items?.length)list.append(el('p','尚未明确记录情绪事件；没有活动不代表悲伤。','muted'));
}
async function extensions() {
  const [emotion,self,rules,events]=await Promise.all([api('/v1/emotion'),api('/v1/self'),api('/v1/emotion/rules'),api('/v1/emotion/events')]);
  renderEmotionRules(emotion,rules,events);
  const form = $('#emotion-form'); for (const name of ['pleasure', 'arousal', 'dominance', 'notes']) form.elements[name].value = emotion[name] ?? (name === 'notes' ? '' : 0);
  form.elements.tags.value = Array.isArray(emotion.tags) ? emotion.tags.join(', ') : '';
  $('#self-style-form').elements.style_preferences.value = self.style_preferences || '';
  const notes = $('#self-notes'); notes.replaceChildren();
  for (const note of self.notes || []) { const row = el('div', '', 'self-note'); row.append(button('删除', () => busy(null, async () => { if (!confirm('删除这条自我笔记？')) return; await api('/v1/self/' + encodeURIComponent(note.id), { method: 'DELETE' }); await extensions(); })), el('div', note.text || '', 'body-text'), el('small', date(note.created_at))); notes.append(row); }
  if (!self.notes?.length) empty(notes, '尚未填写自我笔记', '只有你明确写下的内容会保存在这里。');
}
$('#login-form').addEventListener('submit', e => { e.preventDefault(); void initialize($('#management-token').value.trim()); });
$('#login-retry').addEventListener('click', () => { void initialize(); });
$('#logout').addEventListener('click', () => logout());
$('#logout-top').addEventListener('click', () => logout());
for (const n of document.querySelectorAll('[data-page]')) n.addEventListener('click', () => navigate(n.dataset.page));
for (const n of document.querySelectorAll('[data-go]')) n.addEventListener('click', () => navigate(n.dataset.go));
$('#refresh').addEventListener('click', e => busy(e.currentTarget, async () => { await refresh(); await navigate(currentPage); toast('已刷新'); }));
$('#message-filter').addEventListener('submit', e => { e.preventDefault(); busy(e.submitter, () => messages(false)); });
$('#messages-more').addEventListener('click', e => busy(e.currentTarget, () => messages(true)));
$('#memory-filter').addEventListener('submit', e => { e.preventDefault(); busy(e.submitter, memories); });
$('#run-maintenance').addEventListener('click', e => busy(e.currentTarget, async () => { const r = await api('/v1/maintenance', { method: 'POST', body: {} }); await memories(); toast(r.enabled === false ? '保留策略当前关闭，原话未改变' : '保留策略已运行，原话保持不变'); }));
$('#remember-form').addEventListener('submit', e => { e.preventDefault(); busy(e.submitter, async () => { const d = new FormData(e.target); await api('/v1/remember', { method: 'POST', body: { conversation_id: d.get('conversation_id'), text: d.get('text'), importance: Number(d.get('importance')), disclosure: d.get('disclosure'), metadata: { disclosure_topics: String(d.get('topics')).split(/[,，]/).map(x => x.trim()).filter(Boolean) } } }); e.target.elements.text.value = ''; await memories(); toast('笔记已保存'); }); });
$('#recall-form').addEventListener('submit', e => { e.preventDefault(); busy(e.submitter, async () => { const d = new FormData(e.target); const r = await api('/v1/recall', { method: 'POST', body: { conversation_id: d.get('conversation_id'), query: d.get('query'), token_budget: Number(d.get('token_budget')) } }); renderRecall(r); }); });
$('#close-evidence').addEventListener('click', () => $('#evidence-dialog').close());
$('#backup').addEventListener('click', e => busy(e.currentTarget, async () => { const r = await api('/v1/backup', { method: 'POST', body: {} }); const filename = publicDownloadName(r.file_name, 'memory-brain-backup.db'), c = $('#backup-result'); c.replaceChildren(el('p', '备份已创建，下载文件名：' + filename)); if (r.url) c.append(button('下载这份备份', () => busy(null, () => downloadData(r.url, filename)), 'secondary')); toast('备份已创建'); }));
$('#export').addEventListener('click', e => busy(e.currentTarget, async () => { await downloadData('/v1/export', 'memory-brain-export-' + new Date().toISOString().slice(0, 10) + '.json'); toast('导出已生成'); }));
$('#emotion-form').addEventListener('submit', e => { e.preventDefault(); busy(e.submitter, async () => { const f = new FormData(e.target); await api('/v1/emotion', { method: 'PATCH', body: { pleasure: Number(f.get('pleasure')), arousal: Number(f.get('arousal')), dominance: Number(f.get('dominance')), tags: String(f.get('tags')).split(/[,，]/).map(s => s.trim()).filter(Boolean), notes: f.get('notes') } }); toast('情绪状态已保存'); }); });
$('#emotion-decay-form').addEventListener('input',e=>{e.currentTarget.dataset.dirty='true';});
$('#emotion-decay-form').addEventListener('submit',e=>{e.preventDefault();busy(e.submitter,async()=>{
  const form=e.target,data=new FormData(form),body={};for(const key of Object.keys(emotionLabels))body['emotion_'+key+'_half_life_hours']=Number(data.get('emotion_'+key+'_half_life_hours'));
  await api('/v1/settings',{method:'PATCH',body});delete form.dataset.dirty;await extensions();toast('已承接当前状态并更新衰减节奏');
});});
$('#emotion-event-form').addEventListener('input',e=>{delete e.currentTarget.dataset.pendingKey;});
$('#emotion-event-form').addEventListener('change',e=>{delete e.currentTarget.dataset.pendingKey;});
$('#emotion-event-form').addEventListener('submit',e=>{e.preventDefault();busy(e.submitter,async()=>{
  const form=e.target,data=new FormData(form);form.dataset.pendingKey??=crypto.randomUUID();
  const result=await api('/v1/emotion/events',{method:'POST',body:{kind:data.get('kind'),intensity:Number(data.get('intensity')),idempotency_key:form.dataset.pendingKey}});
  if(result.applied||result.replayed){delete form.dataset.pendingKey;$('#emotion-event-result').textContent=result.replayed?'已核对先前回执，没有重复增加数值。':'本次状态变化已记录；随后按半衰期回到中性。';}
  else $('#emotion-event-result').textContent=result.reason==='cooldown'?'同类事件仍在冷却，没有重复增加数值。':'本次事件没有改变状态。';
  await extensions();
});});
$('#self-style-form').addEventListener('submit', e => { e.preventDefault(); busy(e.submitter, async () => { await api('/v1/self', { method: 'PATCH', body: { style_preferences: new FormData(e.target).get('style_preferences') } }); toast('风格偏好已保存'); }); });
$('#self-note-form').addEventListener('submit', e => { e.preventDefault(); busy(e.submitter, async () => { await api('/v1/self', { method: 'POST', body: { text: new FormData(e.target).get('text') } }); e.target.reset(); await extensions(); toast('自我笔记已添加'); }); });
$('#life-settings-form')?.addEventListener('input', e => { e.currentTarget.dataset.dirty = 'true'; });
$('#life-settings-form')?.addEventListener('change', e => { e.currentTarget.dataset.dirty = 'true'; });
$('#life-settings-form')?.addEventListener('submit', e => {
  e.preventDefault(); busy(e.submitter, async () => {
    const form = e.target, data = new FormData(form);
    await api('/v1/settings', { method: 'PATCH', body: { life_enabled: form.elements.life_enabled.checked,
      life_context_enabled: form.elements.life_context_enabled.checked, life_interval_seconds: Number(data.get('life_interval_seconds')),
      life_wake_time: data.get('life_wake_time'), life_sleep_time: data.get('life_sleep_time') } });
    delete form.dataset.dirty; await life(); toast('生活设置已保存');
  });
});
$('#life-tick')?.addEventListener('click', e => busy(e.currentTarget, async () => {
  const result = await api('/v1/life/tick', { method: 'POST', body: {} }); await life();
  toast(result.ran === true ? '本地生活检查已执行' : result.reason === 'disabled' ? '生活层当前关闭，没有执行动作' : '尚未到检查时间，保持当前记录');
}));
$('#life-event-form')?.addEventListener('submit', e => { e.preventDefault(); busy(e.submitter, async () => {
  const f = new FormData(e.target); await api('/v1/life/events', { method: 'POST', body: { text: f.get('text'), conversation_id: f.get('conversation_id') || 'manual', importance: Number(f.get('importance')) } });
  e.target.elements.text.value = ''; await life(); toast('生活事件已记录');
}); });
$('#life-task-form')?.addEventListener('submit', e => { e.preventDefault(); busy(e.submitter, async () => {
  const f = new FormData(e.target), local = f.get('due_at'); let due = null;
  if (local) { const value = new Date(String(local)); if (Number.isNaN(value.getTime())) throw new Error('截止时间格式不正确'); due = value.toISOString(); }
  await api('/v1/life/tasks', { method: 'POST', body: { title: f.get('title'), conversation_id: f.get('conversation_id') || 'manual', due_at: due } });
  e.target.elements.title.value = ''; e.target.elements.due_at.value = ''; await life(); toast('待办已创建');
}); });
$('#life-book-file')?.addEventListener('change', e => busy(e.target, async () => {
  const file = e.target.files?.[0]; if (!file) return;
  if (file.size > 2 * 1024 * 1024) throw new Error('文本文件过大；请只添加不超过 100,000 字符的 UTF-8 纯文本');
  let text; try { text = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer()); } catch { throw new Error('请先把文件另存为 UTF-8 纯文本，再添加'); }
  if (text.includes('\0') || Array.from(text).length > 100000) throw new Error('只支持不超过 100,000 字符的纯文本');
  const form = $('#life-book-form'); form.elements.text.value = text;
  if (!form.elements.title.value) form.elements.title.value = file.name.toLowerCase().endsWith('.txt') ? file.name.slice(0, -4) : file.name;
  toast('已读取到表单，点击“添加文本书”后才保存');
}));
$('#life-book-form')?.addEventListener('submit', e => { e.preventDefault(); busy(e.submitter, async () => {
  const f = new FormData(e.target), text = String(f.get('text') || '');
  if (!text.trim() || text.includes('\0') || Array.from(text).length > 100000) throw new Error('正文不能为空、包含 NUL 或超过 100,000 字符');
  await api('/v1/life/books', { method: 'POST', body: { title: f.get('title'), text } });
  e.target.reset(); await life(); toast('文本书已保存在本机生活层');
}); });
$('#life-journal-date')?.addEventListener('change', e => { e.target.dataset.explicit = e.target.value ? 'true' : ''; });
$('#life-journal-form')?.addEventListener('submit', e => { e.preventDefault(); busy(e.submitter, async () => { renderLifeJournal(await api(lifeJournalPath())); }); });
function initialize(manualToken) {
  const ticket = new URLSearchParams(location.hash.slice(1)).get('ticket');
  // Clear the one-use ticket before the first network request or await.
  if (ticket) history.replaceState(null, '', location.pathname + location.search);
  if (authTask) return authTask;
  const legacy = manualToken === undefined ? token : manualToken;
  authenticated = false; authEpoch++; scrubPrivateView(); $('#connecting').hidden = false; $('#app').hidden = true; $('#login').hidden = true;
  $('#login-error').textContent = '正在检查本机登录…';
  for (const b of $('#login-form').querySelectorAll('button')) b.disabled = true;
  authTask = (async () => { try {
    if (refreshTask) await refreshTask.catch(() => {});
    token = '';
    let identity;
    if (ticket) {
      await api('/v1/session/bootstrap', { method: 'POST', body: { ticket } }); identity = await api('/v1/session/current');
    } else {
      // A valid browser cookie wins over an obsolete per-tab management token.
      try { identity = await api('/v1/session/current'); } catch (e) { if (!legacy) throw e; }
      if (identity?.authenticated !== true && legacy) {
        token = legacy; await api('/v1/session/browser', { method: 'POST', body: {} }); token = ''; identity = await api('/v1/session/current');
      }
    }
    if (identity?.authenticated !== true) throw new Error('本机登录尚未确认');
    token = ''; sessionStorage.removeItem(TOKEN_KEY); authenticated = true; await enterPanel();
    if (authenticated) { $('#management-token').value = ''; $('#login-error').textContent = ''; }
  } catch (e) {
    showLogin(false); $('#login-error').textContent = e.name === 'AbortError' ? '本机服务响应超时，请稍后重新检查。' : '暂未进入。请从桌面“忆核大脑”打开一次，再点击重新检查；也可展开高级选项手动登录。';
  } finally { for (const b of $('#login-form').querySelectorAll('button')) b.disabled = false; authTask = null; } })();
  return authTask;
}
initialize();
setInterval(() => { if (authenticated && !document.hidden) refresh().catch(() => { if (authenticated) { $('#connection').textContent = '服务暂不可用'; $('#connection').className = 'badge warn'; } }); }, 20000);
