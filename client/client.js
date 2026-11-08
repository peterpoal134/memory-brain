/* DSH browser entry. React and the slot registry belong to the host. */
window.__ModuleLoader__.load({
  id: 'dsh-memory-brain',
  factory(require) {
    const React = require('react');
    const h = React.createElement;
    const { useCallback, useEffect, useRef, useState } = React;
    const prefix = 'api/memory-brain/setup/';
    const fields = [
      ['embed_url', '嵌入服务地址', 'url', '例如 http://127.0.0.1:8000/v1'],
      ['embed_model', '嵌入模型名称', 'text', '填写服务实际提供的模型名'],
      ['embed_key', '嵌入服务新密钥', 'password', '留空保留已保存的密钥'],
      ['llm_url', '整理模型服务地址', 'url', '兼容接口的服务地址'],
      ['llm_model', '整理模型名称', 'text', '填写服务实际提供的模型名'],
      ['llm_key', '整理服务新密钥', 'password', '留空保留已保存的密钥'],
    ];
    const styles = {
      root: { color: 'inherit', fontSize: 14, lineHeight: 1.6, maxWidth: 1000, margin: '0 auto', padding: '16px 12px 24px' },
      muted: { opacity: 0.72, margin: '6px 0 12px' },
      row: { display: 'flex', flexWrap: 'wrap', gap: 12, alignItems: 'center' },
      section: { padding: '16px 0', borderTop: '1px solid rgba(128,128,128,.25)' },
      heading: { fontSize: 17, fontWeight: 650, margin: '0 0 8px' },
      label: { display: 'flex', flexDirection: 'column', gap: 5, flex: '1 1 260px', minWidth: 0, marginBottom: 12 },
      input: { boxSizing: 'border-box', width: '100%', minWidth: 0, padding: '9px 10px', borderRadius: 7, border: '1px solid rgba(128,128,128,.5)', background: 'transparent', color: 'inherit', font: 'inherit' },
      button: { padding: '8px 14px', borderRadius: 7, border: '1px solid rgba(128,128,128,.5)', background: 'transparent', color: 'inherit', font: 'inherit', cursor: 'pointer' },
      primary: { background: '#2376c9', color: '#fff', borderColor: '#2376c9' },
      error: { padding: '9px 12px', borderRadius: 7, border: '1px solid #b95858', color: '#c75b5b', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' },
      notice: { padding: '9px 12px', borderRadius: 7, background: 'rgba(80,140,200,.1)', overflowWrap: 'anywhere' },
    };

    function endpoint(action) {
      return new URL(prefix + action, document.baseURI).pathname;
    }
    function message(value, fallback) {
      const explanations = {
        python_310_required: '未找到 Python 3.10 或更新版本。请先安装 Python，再重新检查。',
        owned_process_failed: '本机准备步骤没有完成。请检查 Python、完整安装包和用户目录权限后重试；已有记忆不会被删除。',
        owned_process_timeout: '准备步骤超时，已有配置和记忆保留。请检查 Python 和用户目录权限后重试。',
        owned_process_output_limit: '准备步骤的输出超过限制，已停止本次准备，已有配置和记忆保留。',
        owned_process_unavailable: '无法启动所需的本机程序。请检查 Python 安装和用户目录权限。',
        owned_process_cleanup_failed: '尚未确认本次安装的后台进程已结束，已暂停重试。请保留当前目录并检查安装进程；已有数据不会删除。',
        previous_install_cleanup_unconfirmed: '上一次安装的进程清理尚未确认，暂不重试，以免两次安装互相覆盖。请联系维护者检查本机安装状态。',
        finish_current_chat_before_applying_models: '请先等当前对话结束，再应用新的模型配置。',
        agent_inventory_unavailable: '当前宿主无法确认对话是否空闲，暂不重启内核。',
        managed_kernel_upgrade_requires_maintenance: '检测到已有旧版内核，已保留环境和记忆。本页暂不执行跨版本升级，请按安装文档的维护步骤处理。',
        kernel_running_upgrade_requires_stop: '已有内核正在运行，准备操作已暂停，原环境和数据保持不变。',
        runtime_not_initialized: '请先完成第一步“准备环境”。',
        kernel_initialization_required: '请先完成运行环境准备，再启用。',
        setup_busy: '另一项操作仍在进行，请等待状态更新后重试。',
        model_url_invalid: '服务地址需要使用 HTTP 或 HTTPS，不能在地址中携带密钥、账号或查询参数。',
        model_keys_require_environment_on_this_platform: '此平台请使用环境变量提供密钥；本页的加密密钥保存目前仅支持 Windows。',
      };
      if (typeof value === 'string' && value.trim()) return explanations[value] || value.slice(0, 600);
      if (value && typeof value === 'object') {
        for (const key of ['message', 'reason', 'code']) {
          if (typeof value[key] === 'string') return value[key].slice(0, 600);
        }
      }
      return fallback;
    }
    async function request(action, { body, csrf, signal } = {}) {
      const options = { method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal };
      if (body !== undefined) {
        if (!csrf) throw new Error('初始化凭据尚未就绪，请先重新检查状态。');
        options.headers = { 'Content-Type': 'application/json', 'X-Memory-Brain-Setup': csrf };
        options.body = JSON.stringify(body);
      }
      const response = await fetch(endpoint(action), options);
      if (response.status === 401) throw new Error('DSH 登录已失效，请重新打开 DSH 后再试。');
      if (response.status === 404) throw new Error('首次设置入口尚未加载。当前宿主可能不支持诊断入口；请检查插件是否启用，并让宿主作者提供运行环境信息。不会改用匿名接口。');
      let result;
      try { result = await response.json(); } catch { throw new Error('初始化入口没有返回有效结果，请重新检查或查看 DSH 插件状态。'); }
      if (!response.ok || result?.ok !== true || !result.data) {
        throw new Error(response.status === 401 ? 'DSH 登录已失效，请重新打开 DSH 后再试。' : message(result?.error, `请求未完成（${response.status}），可以重试。`));
      }
      return result.data;
    }
    function localPanelUrl(value) {
      if (typeof value !== 'string') throw new Error('面板入口未返回有效地址。');
      const url = new URL(value);
      if (!['http:', 'https:'].includes(url.protocol) || !['localhost', '127.0.0.1', '[::1]'].includes(url.hostname) || url.username || url.password) {
        throw new Error('面板地址不是本机服务，已停止打开。');
      }
      return url.href;
    }
    function diagnosticOnly(compatibility) {
      return compatibility && typeof compatibility === 'object' &&
        (compatibility.supported === false || compatibility.mode === 'diagnostic_only');
    }
    function safeDiagnostic(compatibility) {
      // Never serialize the whole status, module paths, arbitrary extra fields,
      // credentials or runtime home. Only the public compatibility facts travel.
      const version = value => typeof value === 'string' && /^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/.test(value) ? value : null;
      const code = value => typeof value === 'string' && /^[a-zA-Z0-9_-]{1,96}$/.test(value) ? value : null;
      const digest = value => typeof value === 'string' && /^[a-fA-F0-9]{64}$/.test(value) ? value : null;
      const result = { supported: false, mode: 'diagnostic_only',
        reason: code(compatibility.reason) || 'host_build_unverified',
        plugin_version: version(compatibility.plugin_version), node_version: version(compatibility.node_version),
        platform: code(compatibility.platform), arch: code(compatibility.arch), modules: {} };
      for (const name of ['loop', 'session', 'llm', 'tools', 'connection']) {
        const item = compatibility.modules?.[name];
        if (!item || typeof item !== 'object') continue;
        result.modules[name] = { version: version(item.version), sha256: digest(item.sha256) };
        if (name === 'loop' && digest(item.canonical_sha256)) result.modules[name].canonical_sha256 = digest(item.canonical_sha256);
      }
      for (const name of ['loop_sha256', 'loop_canonical_sha256']) if (digest(compatibility[name])) result[name] = digest(compatibility[name]);
      return result;
    }

    function SetupCard(props = {}) {
      const [status, setStatus] = useState(null);
      const [error, setError] = useState('');
      const [networkError, setNetworkError] = useState('');
      const [notice, setNotice] = useState('');
      const [pending, setPending] = useState(false);
      const [values, setValues] = useState(() => Object.fromEntries(fields.map(([key]) => [key, ''])));
      const [panelLink, setPanelLink] = useState('');
      const [manualDiagnosticCopy, setManualDiagnosticCopy] = useState(false);
      const [diagnosticCopyNotice, setDiagnosticCopyNotice] = useState('');
      const alive = useRef(true);
      const statusFlight = useRef(null);
      const pendingRef = useRef(false);
      const statusRef = useRef(null);
      const statusAbort = useRef(null);

      const refresh = useCallback(() => {
        if (statusFlight.current) return statusFlight.current;
        const controller = new AbortController();
        statusAbort.current = controller;
        const timeout = setTimeout(() => controller.abort(), 15000);
        const flight = request('status', { signal: controller.signal }).then(data => {
          if (!alive.current) return;
          statusRef.current = data;
          setStatus(data);
          setNetworkError('');
        }).catch(err => {
          if (alive.current) setNetworkError(err.name === 'AbortError' ? '检查状态超时，可以重新检查。' : err.message);
        }).finally(() => {
          clearTimeout(timeout);
          statusFlight.current = null;
          if (statusAbort.current === controller) statusAbort.current = null;
        });
        statusFlight.current = flight;
        return flight;
      }, []);

      useEffect(() => {
        alive.current = true;
        refresh();
        const timer = setInterval(refresh, 3000);
        return () => { alive.current = false; clearInterval(timer); statusAbort.current?.abort(); };
      }, [refresh]);

      const runtime = status?.runtime || {};
      const models = runtime.models || {};
      const operation = status?.operation;
      const working = Boolean(pending || status?.busy);
      const pythonMissing = runtime.python?.available === false;
      const prepared = runtime.state === 'ready' || Boolean(runtime.installed_version);
      const active = status?.adapter?.enabled === true;
      const operationText = typeof operation === 'string' ? ({ initialize: '正在准备运行环境与依赖…', models: '正在保存本机模型配置…', start: '正在启动并连接独立内核…' }[operation] || operation) : message(operation, '');
      const phaseText = active ? '已接入 DSH' : runtime.running ? '内核已启动，等待接入 DSH' : prepared ? '环境已准备，等待启用' : '等待首次准备';
      const legacy = status?.legacy === true;
      const readOnlyDiagnostic = diagnosticOnly(status?.compatibility);

      async function perform(action, body, success) {
        if (diagnosticOnly(statusRef.current?.compatibility)) {
          setError('当前构建仅可查看诊断，采集和召回未启用。');
          return;
        }
        if (pendingRef.current || statusRef.current?.busy) return;
        pendingRef.current = true;
        setPending(true);
        setError('');
        setNotice('');
        try {
          const result = await request(action, { body, csrf: statusRef.current?.csrfToken });
          if (!alive.current) return;
          if (action === 'models') setValues(current => ({ ...current, embed_key: '', llm_key: '' }));
          setNotice(success);
          await refresh();
          return result;
        } catch (err) {
          if (alive.current) setError(err.message);
        } finally {
          pendingRef.current = false;
          if (alive.current) setPending(false);
        }
      }

      async function saveModels(event) {
        event.preventDefault();
        const body = {};
        for (const [key] of fields) if (values[key].trim()) body[key] = values[key].trim();
        if (!Object.keys(body).length) { setNotice('没有填写新的配置；已有配置保持不变，也可以先跳过模型配置。'); return; }
        await perform('models', body, '模型配置已提交，正在检查结果。密钥留空时保留原值。');
      }

      async function openPanel() {
        if (diagnosticOnly(statusRef.current?.compatibility)) return;
        if (pendingRef.current || statusRef.current?.busy) return;
        setPanelLink('');
        // Reserve a user-initiated window before the asynchronous ticket request.
        let opened;
        try { opened = window.open('about:blank', '_blank'); if (opened) opened.opener = null; } catch { opened = null; }
        const result = await perform('panel', {}, '本机面板入口已生成。');
        if (!result || !alive.current) { try { opened?.close(); } catch {} return; }
        try {
          const url = localPanelUrl(result.url);
          if (opened && !opened.closed) opened.location.replace(url);
          else setPanelLink(url);
        } catch (err) { try { opened?.close(); } catch {} setError(err.message); }
      }

      function button(label, onClick, { disabled = working || !status, primary = false } = {}) {
        return h('button', { type: 'button', onClick, disabled, style: { ...styles.button, ...(primary ? styles.primary : {}), opacity: disabled ? 0.5 : 1 } }, label);
      }

      if (props.view === 'summary') return null;
      if (readOnlyDiagnostic) {
        const diagnosticText = JSON.stringify(safeDiagnostic(status.compatibility), null, 2);
        const copyDiagnostic = async () => {
          try {
            if (typeof globalThis.navigator?.clipboard?.writeText !== 'function') throw new Error('clipboard_unavailable');
            await globalThis.navigator.clipboard.writeText(diagnosticText);
            if (alive.current) { setManualDiagnosticCopy(false); setDiagnosticCopyNotice('安全诊断已复制。'); }
          } catch {
            if (alive.current) { setManualDiagnosticCopy(true); setDiagnosticCopyNotice('自动复制不可用，请从下方只读框手动复制。'); }
          }
        };
        return h('div', { style: styles.root },
          h('h2', { style: { ...styles.heading, fontSize: 21 } }, '忆核大脑 · 运行环境诊断'),
          h('p', { role: 'status', style: styles.notice }, '当前构建尚未完成核验，采集和召回未启用。'),
          h('p', { style: styles.muted }, '这里只读取兼容性信息，不准备环境、不保存模型配置、不启动内核，也不打开面板。需要先核对实际运行宿主，不能仅凭“0.2.1-rc.2”版本号判断。'),
          h('p', { style: styles.muted }, '手机可能只是网页访问端；如果是手机独立 App，需要由其作者核实内置 Node、Python 及插件接口能力，当前不宣称已兼容。'),
          h('div', { style: styles.row }, button('重新检查', refresh, { disabled: pending }), button('复制安全诊断', copyDiagnostic, { disabled: false })),
          networkError ? h('p', { role: 'alert', style: styles.error }, networkError) : null,
          diagnosticCopyNotice ? h('p', { role: 'status', style: styles.muted }, diagnosticCopyNotice) : null,
          h('details', null, h('summary', { style: { cursor: 'pointer', marginTop: 12 } }, '展开安全诊断 JSON'),
            h('pre', { style: { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontSize: 12 } }, diagnosticText)),
          manualDiagnosticCopy ? h('label', { style: { ...styles.label, marginTop: 12 } }, h('span', null, '手动复制安全诊断'),
            h('textarea', { readOnly: true, value: diagnosticText, rows: 12, spellCheck: false,
              'aria-label': '手动复制安全诊断', style: { ...styles.input, fontFamily: 'monospace', fontSize: 12 },
              onFocus: event => event.target.select?.() })) : null,
          h('p', { style: styles.muted }, '诊断只包含插件/Node 版本、平台、架构和模块指纹，不包含目录、模型密钥或对话正文。'));
      }
      return h('div', { style: styles.root },
        h('h2', { style: { ...styles.heading, fontSize: 21 } }, '忆核大脑 · 首次设置'),
        h('p', { style: styles.muted }, '在这里准备独立内核、选择模型并接入 DSH。准备完成前，插件尚未开始保存或召回你的对话。'),
        h('div', { style: styles.row },
          h('strong', null, status ? phaseText : '正在检查本机环境…'),
          button('重新检查', refresh, { disabled: pending, primary: false })),
        status?.home ? h('p', { style: { ...styles.muted, overflowWrap: 'anywhere' } }, '本机运行与数据目录：', h('code', null, status.home)) : null,
        operationText ? h('p', { role: 'status', style: styles.muted }, operationText) : null,
        networkError ? h('p', { role: 'alert', style: styles.error }, networkError) : null,
        error ? h('p', { role: 'alert', style: styles.error }, error) : null,
        status?.error ? h('p', { role: 'alert', style: styles.error }, message(status.error, '上一次操作未完成，可以重试。')) : null,
        notice ? h('p', { role: 'status', style: styles.notice }, notice) : null,
        h('section', { style: styles.section },
          h('h3', { style: styles.heading }, '1 · 准备环境'),
          h('p', { style: styles.muted }, '点击后才会创建独立运行环境。所需时区依赖已随包提供，准备过程不联网下载；不会自动安装 Python，也不会读取或迁移你以前的记忆。'),
          h('p', null,
            'Python：', runtime.python?.available === true ? `可用${runtime.python.version ? ' · ' + runtime.python.version : ''}` : pythonMissing ? '未找到兼容版本' : '待检查',
            '；内核：', runtime.installed_version || '尚未安装',
            runtime.expected_version ? `（目标 ${runtime.expected_version}）` : ''),
          pythonMissing ? h('p', null,
            '请先安装兼容的 Python，并按 DSH 提示重新加载插件后检查。 ',
            h('a', { href: 'https://www.python.org/downloads/', target: '_blank', rel: 'noopener noreferrer' }, '打开 Python 官方下载页')) : null,
          legacy ? h('p', { style: styles.notice }, '正在沿用原有本机安装，原配置和记忆位置保持不变；无需在这里重新初始化。') :
          button(working ? '正在处理…' : prepared ? '检查并准备环境' : '准备运行环境', () => perform('initialize', { allowDownloads: true }, '准备任务已提交，请等待状态检查完成。'), { disabled: working || !status || pythonMissing, primary: !prepared })),
        h('section', { style: styles.section },
          h('h3', { style: styles.heading }, '2 · 模型配置（可跳过）'),
          h('p', { style: styles.muted }, '未配置模型时，基础原话保存和词法检索仍可使用。嵌入模型用于语义检索，整理模型用于后台派生；云端服务可能产生费用。服务密钥只提交到本机宿主，不会在此读回显示。'),
          h('p', null,
            '嵌入：', models.embedding_configured ? '已配置' : '未配置',
            '；整理：', models.derivation_configured ? '已配置' : '未配置',
            models.embed_key_saved ? '；嵌入密钥已保存' : '',
            models.llm_key_saved ? '；整理密钥已保存' : ''),
          legacy ? h('p', { style: styles.notice }, '现有本机安装继续使用原目录的模型配置；本页不会覆盖或迁移已有密钥。') : h('form', { onSubmit: saveModels, autoComplete: 'off' },
            h('div', { style: { ...styles.row, alignItems: 'stretch' } }, fields.map(([key, label, type, placeholder]) =>
              h('label', { key, style: styles.label }, h('span', null, label), h('input', {
                name: key, type, value: values[key], placeholder, disabled: working || !prepared,
                autoComplete: type === 'password' ? 'new-password' : 'off', spellCheck: false,
                maxLength: type === 'password' ? 4096 : 2048, style: styles.input,
                onChange: event => { const value = event.target.value; setValues(current => ({ ...current, [key]: value })); },
              })))),
            h('div', { style: styles.row },
              h('button', { type: 'submit', disabled: working || !status || !prepared, style: { ...styles.button, opacity: working || !status || !prepared ? 0.5 : 1 } }, '保存填写的配置'),
              button('暂时跳过模型配置', () => setNotice('可以先启用基础原话和词法检索，之后再回这里配置模型。'))))),
        h('section', { style: styles.section },
          h('h3', { style: styles.heading }, '3 · 启用并打开面板'),
          h('p', { style: styles.muted }, active ? 'DSH 接入已启用。面板可查看实际入库、索引和后台任务状态；这不代表历史对话已经自动迁入。' : '准备完成后启用。只有状态确认接入 DSH 后，才开始按当前设置处理新对话。'),
          runtime.restart_required ? h('p', { style: styles.notice }, '新模型配置尚未生效。请先结束当前对话，再点击“应用配置并重新连接”；只重启本插件自己的内核，不重启 DSH。') : null,
          h('div', { style: styles.row },
            button(runtime.restart_required ? '应用配置并重新连接' : active ? '检查并启用' : '启用忆核大脑', () => perform('start', {}, '启用任务已提交，正在检查实际接入状态。'), { primary: true, disabled: working || !status || !prepared }),
            button('打开 3D 大脑与控制面板', openPanel, { disabled: working || !status || runtime.running !== true })),
          panelLink ? h('p', null, h('a', { href: panelLink, target: '_blank', rel: 'noopener noreferrer', onClick: () => setTimeout(() => setPanelLink(''), 0) }, '浏览器阻止了弹窗，点击这里打开本机面板')) : null,
          status?.compatibility?.error ? h('p', { role: 'alert', style: styles.error }, message(status.compatibility.error, '当前 DSH 构建尚未通过兼容检查，暂未启用。')) : null));
    }

    return {
      inject: ['slots'],
      apply(ctx) {
        ctx.slots.inject('plugins.bundle.config', () => ctx.slots.register({
          name: 'plugins.bundle.config', key: 'dsh-memory-brain',
        }, (props = {}) => props.view === 'summary' ? null : h(SetupCard, props)));
        ctx.slots.inject('plugins.bundle.activation', () => ctx.slots.register({
          name: 'plugins.bundle.activation', key: 'dsh-memory-brain',
        }, props => h('div', { style: styles.notice },
          h('p', null, '首次运行请准备独立环境；也可在这里查看模型配置和实际接入状态。'),
          h('button', { type: 'button', onClick: props.onOpenDetails, style: styles.button }, '前往首次设置'))));
      },
    };
  },
});
