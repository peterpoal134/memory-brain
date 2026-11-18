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
    const contextFlags = {
      contextSummaryEnabled: '启用简洁中文摘要（保留宿主已有自定义摘要）',
      contextToolPruneEnabled: '裁剪过长的工具结果',
      historyEnabled: '启用当前会话历史回查工具',
      historyAutoEnabled: '明确询问历史且记忆召回为空时，自动按字面回查',
    };
    const contextNumbers = [
      ['contextSummaryMaxChars', '摘要目标字数', 200, 20000, 2000],
      ['contextSummaryMaxTokens', '摘要输出上限（tokens）', 128, 65536, 4096],
      ['contextPressureCooldownMs', '压力整理冷却（毫秒；0 为关闭）', 0, 300000, 60000],
      ['contextPressureThresholdRatio', '压力整理窗口比例（0.01～0.99）', 0.01, 0.99, 0.8],
      ['contextPressureRetainRatio', '压力整理原文保留比例（小于触发比例）', 0.01, 0.99, 0.16],
      ['contextPressureHeadroomTokens', '压力整理预留余量（tokens）', 128, 65536, 4096],
      ['contextToolPruneThresholdChars', '工具结果裁剪起点（字符）', 128, 1048576, 4096],
      ['contextToolPruneHeadChars', '工具结果保留开头（字符）', 0, 1048575, 2048],
      ['contextToolPruneTailChars', '工具结果保留结尾（字符）', 0, 1048575, 512],
      ['historyTimeoutMs', '历史回查等待上限（毫秒）', 100, 10000, 2000],
    ];
    const extraContextDefaults = { ...Object.fromEntries(Object.keys(contextFlags).map(key => [key, true])),
      ...Object.fromEntries(contextNumbers.map(([key, , , , value]) => [key, String(value)])) };
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
        running_kernel_version_mismatch: '运行中的内核版本与插件预期不一致，请按安装文档的升级步骤处理。',
        ambiguous_kernel_wheel: '安装包含有多份匹配当前版本的内核文件，请重新获取完整发行包。',
        kernel_wheel_version_mismatch: '安装包缺少与当前插件版本匹配的内核文件，请重新获取完整发行包。',
        kernel_wheel_invalid: '内核文件类型异常，请重新获取完整发行包。',
        no_free_local_kernel_port: '忆核的本机端口范围已占满，请核对已有实例后重试。',
        kernel_identity_not_confirmed: '配置端口已被占用，尚未确认它属于这个忆核实例。请核对占用端口的应用后重试。',
        archive_only_session_contract_required: '当前 DSH 未提供已核验的会话事件与日志读取能力，暂未开始归档。',
        runtime_not_initialized: '请先完成第一步“准备环境”。',
        kernel_initialization_required: '请先完成运行环境准备，再启用。',
        setup_busy: '另一项操作仍在进行，请等待状态更新后重试。',
        model_url_invalid: '服务地址需要使用 HTTP 或 HTTPS，不能在地址中携带密钥、账号或查询参数。',
        model_keys_require_environment_on_this_platform: '此平台请使用环境变量提供密钥；本页的加密密钥保存目前仅支持 Windows。',
        context_settings_fields_invalid: '上下文设置包含无法识别的项目，请重新检查页面。',
        context_settings_value_invalid: '请按输入框标注的范围填写整数。',
        context_keep_must_be_below_trigger: '保留轮数需要小于触发整理的轮数。',
        context_tool_prune_retention_invalid: '工具结果保留的开头、结尾，加上39字符的裁剪提示，不能超过裁剪起点。',
        context_pressure_retention_invalid: '压力整理的原文保留比例，需要小于触发比例。',
        context_settings_schema_invalid: '已有上下文设置文件格式异常，已保留现场，请检查后重试。',
        context_settings_symlink_refused: '上下文设置位置使用了不受支持的链接，已暂停保存。',
        context_settings_file_invalid: '上下文设置文件类型或大小异常，已暂停读取和保存。',
        context_settings_io_failed: '无法读取或保存上下文设置，请检查本机用户目录的权限。',
        finish_current_chat_before_applying_context: '请先等当前对话结束，再应用上下文设置。',
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
    function compatibilityMessage(compatibility) {
      const reason = compatibility?.reason;
      if (reason === 'dsh_runtime_not_resolved' || reason === 'runtime_unavailable')
        return '未找到当前 DSH 的运行模块，尚未完成构建核验。采集和召回未启用。';
      if (reason === 'asar_runtime_unavailable')
        return '检测到桌面打包目录，但当前进程无法读取内部运行模块。可能是打包方式或访问环境不匹配，不一定是权限问题。采集和召回未启用。';
      if (reason === 'runtime_access_denied')
        return '已找到宿主位置，但系统拒绝读取运行文件。请核实当前账号的文件访问权限，并复制安全诊断。采集和召回未启用。';
      if (reason === 'required_modules_missing')
        return '已找到宿主位置，但所需运行模块不完整。采集和召回未启用。';
      if (reason === 'required_modules_unreadable')
        return '已找到宿主位置，但一个或多个必需模块读取失败。安全诊断会列出失败阶段和错误类别。采集和召回未启用。';
      if (reason === 'host_module_identity_mismatch')
        return '发现不同来源的宿主模块，暂未启用采集和召回，以免连接到另一份运行环境。请完成当前任务后重新加载插件，并复制安全诊断。';
      if (reason === 'node_22_or_later_required')
        return '宿主的 Node 版本需要为 22 或更新版本。采集和召回未启用。';
      return '已找到宿主模块，但当前构建尚未完成核验。采集和召回未启用。';
    }
    function safeDiagnostic(compatibility) {
      // Never serialize the whole status, module paths, arbitrary extra fields,
      // credentials or runtime home. Only the public compatibility facts travel.
      compatibility = compatibility && typeof compatibility === 'object' && !Array.isArray(compatibility) ? compatibility : {};
      const version = value => typeof value === 'string' && value.length <= 80 && /^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/.test(value) ? value : null;
      const digest = value => typeof value === 'string' && /^[a-fA-F0-9]{64}$/.test(value) ? value : null;
      const reasons = ['runtime_unavailable', 'asar_runtime_unavailable', 'runtime_access_denied', 'required_modules_missing',
        'required_modules_unreadable', 'loop_source_invalid_utf8', 'node_22_or_later_required', 'native_request_messages_markers',
        'verified_native_snapshot', 'verified_syntax_equivalent',
        'verified_stock_source', 'verified_stock_source_formatting', 'unknown_loop_fingerprint', 'host_module_identity_mismatch',
        'dsh_runtime_not_resolved', 'host_build_unverified'];
      const errorCodes = new Set(['ENOENT', 'ENOTDIR', 'EACCES', 'EPERM', 'EISDIR', 'EINVAL', 'ELOOP', 'ENAMETOOLONG', 'EIO',
        'EMFILE', 'ENFILE', 'ENOMEM', 'ENOTSUP', 'ENOSYS', 'EBUSY', 'EROFS', 'ETIMEDOUT', 'MODULE_NOT_FOUND', 'ERR_MODULE_NOT_FOUND',
        'ERR_PACKAGE_PATH_NOT_EXPORTED', 'ERR_INVALID_PACKAGE_CONFIG', 'ERR_INVALID_PACKAGE_TARGET', 'ERR_UNSUPPORTED_DIR_IMPORT']);
      const attemptCodes = new Set([...errorCodes, ...reasons, 'resolved', 'not_directory', 'modules_not_found', 'same_module_set',
        'module_identity_mismatch', 'module_resolution_failed', 'candidate_limit', 'unknown']);
      const sources = ['environment', 'desktop_host_argument', 'host_module_anchor', 'asar_host_entry', 'electron_resources',
        'electron_executable', 'plugin_module_anchor', 'working_directory_anchor', 'host_module_anchor_search',
        'plugin_module_anchor_search', 'working_directory_anchor_search', 'unresolved'];
      const moduleNames = ['loop', 'session', 'llm', 'tools', 'connection', 'compaction', 'basicCompaction', 'toolResultPruner', 'tokenMeter', 'cordis'];
      const layout = value => ['asar', 'directory', 'unknown'].includes(value) ? value : 'unknown';
      const result = { supported: false, mode: 'diagnostic_only',
        reason: reasons.includes(compatibility.reason) ? compatibility.reason : 'host_build_unverified',
        plugin_version: version(compatibility.plugin_version), node_version: version(compatibility.node_version),
        platform: ['aix', 'android', 'darwin', 'freebsd', 'linux', 'openbsd', 'sunos', 'win32'].includes(compatibility.platform) ? compatibility.platform : null,
        arch: ['arm', 'arm64', 'ia32', 'loong64', 'mips', 'mipsel', 'ppc', 'ppc64', 'riscv64', 's390', 's390x', 'x64'].includes(compatibility.arch) ? compatibility.arch : null,
        modules: {} };
      for (const name of moduleNames) {
        const item = compatibility.modules?.[name];
        if (!item || typeof item !== 'object') continue;
        result.modules[name] = { version: version(item.version), sha256: digest(item.sha256) };
        if (name === 'loop' && digest(item.canonical_sha256)) result.modules[name].canonical_sha256 = digest(item.canonical_sha256);
      }
      for (const name of ['loop_sha256', 'loop_canonical_sha256']) if (digest(compatibility[name])) result[name] = digest(compatibility[name]);
      if (Array.isArray(compatibility.read_errors)) result.read_errors = compatibility.read_errors.slice(0, 24)
        .filter(item => item && typeof item === 'object' && ['runtime', ...moduleNames].includes(item.module) && ['realpath', 'stat', 'read'].includes(item.stage))
        .map(item => ({ module: item.module, stage: item.stage, code: errorCodes.has(item.code) ? item.code : 'unknown' }));
      const discovery = compatibility.discovery;
      if (discovery && typeof discovery === 'object' && !Array.isArray(discovery)) result.discovery = {
        source: sources.includes(discovery.source) ? discovery.source : null,
        layout: layout(discovery.layout),
        candidate_count: Number.isInteger(discovery.candidate_count) && discovery.candidate_count >= 0 && discovery.candidate_count <= 64 ? discovery.candidate_count : null,
        electron_version: version(discovery.electron_version),
        asar_enabled: typeof discovery.asar_enabled === 'boolean' ? discovery.asar_enabled : null,
        attempts: Array.isArray(discovery.attempts) ? discovery.attempts.slice(0, 32)
          .filter(item => item && typeof item === 'object' && sources.includes(item.source) && ['resolve', 'candidate', 'identity'].includes(item.stage))
          .map(item => ({ source: item.source, layout: layout(item.layout), stage: item.stage, code: attemptCodes.has(item.code) ? item.code : 'unknown' })) : [],
      };
      return result;
    }

    function SetupCard(props = {}) {
      const [status, setStatus] = useState(null);
      const [error, setError] = useState('');
      const [networkError, setNetworkError] = useState('');
      const [notice, setNotice] = useState('');
      const [pending, setPending] = useState(false);
      const [values, setValues] = useState(() => Object.fromEntries(fields.map(([key]) => [key, ''])));
      const [contextValues, setContextValues] = useState({ contextEnabled: true, contextTriggerTurns: '12', contextKeepTurns: '4', contextMaxInputTokens: '', contextSafetyTokens: '2048', ...extraContextDefaults });
      const [panelLink, setPanelLink] = useState('');
      const [manualDiagnosticCopy, setManualDiagnosticCopy] = useState(false);
      const [diagnosticCopyNotice, setDiagnosticCopyNotice] = useState('');
      const alive = useRef(true);
      const statusFlight = useRef(null);
      const pendingRef = useRef(false);
      const statusRef = useRef(null);
      const statusAbort = useRef(null);
      const contextDirty = useRef(false);

      const refresh = useCallback(() => {
        if (statusFlight.current) return statusFlight.current;
        const controller = new AbortController();
        statusAbort.current = controller;
        const timeout = setTimeout(() => controller.abort(), 15000);
        const flight = request('status', { signal: controller.signal }).then(data => {
          if (!alive.current) return;
          statusRef.current = data;
          setStatus(data);
          if (!contextDirty.current && data.context?.settings) {
            const policy = data.context.settings;
            setContextValues({ contextEnabled: policy.contextEnabled === true,
              contextTriggerTurns: String(policy.contextTriggerTurns), contextKeepTurns: String(policy.contextKeepTurns),
              contextMaxInputTokens: policy.contextMaxInputTokens === null ? '' : String(policy.contextMaxInputTokens),
              contextSafetyTokens: String(policy.contextSafetyTokens),
              ...Object.fromEntries(Object.keys(contextFlags).map(key => [key, policy[key] === true])),
              ...Object.fromEntries(contextNumbers.map(([key]) => [key, String(policy[key])])) });
          }
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
      const prepared = runtime.prepared ?? (runtime.state === 'ready' || Boolean(runtime.installed_version));
      const active = status?.adapter?.enabled === true;
      const archiveOnly = status?.archive_only === true;
      const contextPolicy = status?.context;
      const contextObserved = status?.adapter?.context_management || {};
      const compression = contextObserved.compression || {};
      const budget = contextObserved.budget || {};
      const support = contextObserved.support || {};
      const history = status?.adapter?.session_history || {};
      const restartRequired = runtime.restart_required === true || contextPolicy?.restart_required === true;
      const operationText = typeof operation === 'string' ? ({ initialize: '正在准备运行环境与依赖…', models: '正在保存本机模型配置…', context: '正在保存上下文设置…', start: '正在连接并检查记忆插件…' }[operation] || operation) : message(operation, '');
      const versionMismatch = runtime.expected_version && runtime.installed_version && runtime.expected_version !== runtime.installed_version
        || runtime.error === 'running_kernel_version_mismatch';
      const phaseText = active ? (archiveOnly ? '仅归档已启用' : '已接入 DSH') : versionMismatch ? '需要维护以匹配内核版本' :
        runtime.running ? '内核已启动，等待接入 DSH' : prepared ? '环境已准备，等待启用' :
          runtime.installed_version ? '等待完成环境准备' : '等待首次准备';
      const legacy = status?.legacy === true;
      const readOnlyDiagnostic = !archiveOnly && diagnosticOnly(status?.compatibility);

      async function perform(action, body, success) {
        if (statusRef.current?.archive_only !== true && diagnosticOnly(statusRef.current?.compatibility)) {
          setError(compatibilityMessage(statusRef.current?.compatibility));
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
          if (action === 'context') contextDirty.current = false;
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

      async function saveContext(event) {
        event.preventDefault();
        const integer = value => /^\d+$/.test(value) ? Number(value) : NaN;
        const decimal = value => /^\d+(?:\.\d+)?$/.test(value) ? Number(value) : NaN;
        const body = { contextEnabled: contextValues.contextEnabled,
          contextTriggerTurns: integer(contextValues.contextTriggerTurns), contextKeepTurns: integer(contextValues.contextKeepTurns),
          contextMaxInputTokens: contextValues.contextMaxInputTokens === '' ? null : integer(contextValues.contextMaxInputTokens),
          contextSafetyTokens: integer(contextValues.contextSafetyTokens),
          ...Object.fromEntries(Object.keys(contextFlags).map(key => [key, contextValues[key]])),
          ...Object.fromEntries(contextNumbers.map(([key]) => [key, key.endsWith('Ratio') ? decimal(contextValues[key]) : integer(contextValues[key])])) };
        if (!Number.isInteger(body.contextTriggerTurns) || body.contextTriggerTurns < 2 || body.contextTriggerTurns > 200 ||
            !Number.isInteger(body.contextKeepTurns) || body.contextKeepTurns < 1 || body.contextKeepTurns >= body.contextTriggerTurns ||
            body.contextMaxInputTokens !== null && (!Number.isInteger(body.contextMaxInputTokens) || body.contextMaxInputTokens < 256 || body.contextMaxInputTokens > 4194304) ||
            !Number.isInteger(body.contextSafetyTokens) || body.contextSafetyTokens < 256 || body.contextSafetyTokens > 65536) {
          setError('请检查轮数与输入预算：触发 2～200 轮，保留至少 1 轮且少于触发轮数；预算需在标注范围内。'); return;
        }
        if (contextNumbers.some(([key, , min, max]) => !(key.endsWith('Ratio') ? Number.isFinite(body[key]) : Number.isInteger(body[key])) || body[key] < min || body[key] > max)) {
          setError('请按标注范围填写压缩、工具裁剪与历史回查设置。'); return;
        }
        if (body.contextToolPruneHeadChars + 39 + body.contextToolPruneTailChars > body.contextToolPruneThresholdChars) {
          setError('工具结果保留的开头、结尾，加上39字符的裁剪提示，不能超过裁剪起点。'); return;
        }
        if (body.contextPressureRetainRatio >= body.contextPressureThresholdRatio) {
          setError('压力整理的原文保留比例，需要小于触发比例。'); return;
        }
        await perform('context', body, '上下文设置已提交。保存后先结束当前对话，再应用配置并重新连接。');
      }

      function contextInput(label, key, min, max, placeholder) {
        return h('label', { key, style: styles.label }, h('span', null, label), h('input', {
          type: 'number', name: key, min, max, step: key.endsWith('Ratio') ? 0.01 : 1, value: contextValues[key], placeholder,
          disabled: working || !contextPolicy, style: styles.input,
          onChange: event => { contextDirty.current = true; const value = event.target.value; setContextValues(current => ({ ...current, [key]: value })); },
        }));
      }
      function contextToggle(label, key) {
        return h('label', { key, style: { ...styles.row, marginBottom: 10 } }, h('input', {
          type: 'checkbox', checked: contextValues[key], disabled: working || !contextPolicy,
          onChange: event => { contextDirty.current = true; const checked = event.target.checked; setContextValues(current => ({ ...current, [key]: checked })); },
        }), label);
      }
      function contextState(value) {
        const states = { waiting_agent: '等待实际对话确认能力', ready: '已确认，可按设置整理', disabled: '已关闭', stopped: '已停止',
          unsupported_service: '当前对话未提供原生压缩服务', unsupported_module: '当前宿主边界接口尚未核验', unsupported: '当前对话暂不支持',
          waiting_archive: '等待原话归档确认，暂不压缩', archive_pending: '等待原话归档确认，暂不压缩',
          compacting: '正在整理历史', compressing: '正在整理历史', failed: '本次整理失败，保留原历史',
          waiting_complete_turns: '等待足够的完整回合', changed: '历史发生变化，本次暂缓', busy: '宿主正在处理其它维护，本次暂缓',
          timed_out: '本次整理超时，保留原历史', cancelled: '本次整理已取消', not_started: '等待插件启用',
          error: '本次处理未完成，保留原历史', budget_blocked: '输出预留与安全余量已占满模型窗口，本轮暂停', blocked: '输出预留与安全余量已占满模型窗口，本轮暂停',
          within_budget: '预计输入在预算内', allowed: '预计输入在预算内', unknown: '等待本轮估算', unavailable: '当前无法确认',
          unmeasured: '尚无本轮估算', waiting_request: '等待本轮请求估算', awaiting_request: '等待本轮请求估算', checked: '已完成本轮估算',
          needs_capacity: '缺少模型窗口信息，预算校验不完整', needs_output_reserve: '缺少输出预留信息，预算校验不完整',
          partial_non_text: '包含非文本材料，估算不完整', estimated_text_json_bytes: '旧版字节估算，需更新插件',
          advisory_text_json: '输入量仅供估算参考，未据此拦截请求', invalid_output_reserve: '输出预留与安全余量已占满模型窗口',
          not_observed: '等待实际对话确认', short_chinese: '简洁中文摘要已接入', host_custom: '保留宿主自定义策略',
          host_existing: '沿用宿主已有服务', owned_native: '已接入包内配套的原生服务',
          host_auto_disabled: '宿主已关闭自动压力整理', cooldown: '压力整理冷却中',
          needs_config: '模型窗口或预留不足，需要调整配置', partial: '部分能力可用，请查看具体状态',
          waiting_tools: '等待宿主工具服务', no_authorized_match: '本次未找到获准回读的原话', matched: '已找到获准回读的原话' };
        return states[value] || '尚未完成本轮确认';
      }

      async function openPanel() {
        if (statusRef.current?.archive_only !== true && diagnosticOnly(statusRef.current?.compatibility)) return;
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
          h('p', { role: 'status', style: styles.notice }, compatibilityMessage(status.compatibility)),
          h('p', { style: styles.muted }, '这里只读取兼容性信息。模块未找到、读取失败和构建未核验需要分别处理。请先完成当前任务，再重新加载插件，并复制下方安全诊断交给维护者；已有记忆保持不变。版本号本身不足以确认兼容。'),
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
          h('p', { style: styles.muted }, '诊断只包含版本、平台、模块指纹、宿主布局、发现阶段和错误类别，不包含目录、模型密钥或对话正文。'));
      }
      return h('div', { style: styles.root },
        h('h2', { style: { ...styles.heading, fontSize: 21 } }, '忆核大脑 · 首次设置'),
        h('p', { style: styles.muted }, archiveOnly ? '仅归档模式。启用后保存此后正式提交的原话，可在面板手动查找。对话内自动召回、注入和上下文整理均关闭。' : '在这里准备独立内核、选择模型并接入 DSH。准备完成前，插件尚未开始保存或召回你的对话。'),
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
        !archiveOnly ? h('section', { style: styles.section },
          h('h3', { style: styles.heading }, '3 · 上下文管理'),
          h('p', { style: styles.muted }, '默认累计 12 轮真实用户对话后尝试整理较早历史，保留最近 4 个完整回合。原话归档确认后才压缩，失败时保留原历史。'),
          h('p', { style: styles.muted }, '自带简洁中文摘要、压力整理冷却与工具结果裁剪，已有自定义宿主策略优先保留。摘要可能额外调用模型并计费；能力以实际状态为准。'),
          h('p', { role: 'status' }, '原生压缩：', contextState(compression.state || contextObserved.state || 'waiting_agent')),
          h('p', { role: 'status', style: styles.muted }, '摘要：', contextState(support.summary_state || 'not_observed'),
            '；压力策略：', contextState(support.pressure_state || 'not_observed'), '；工具裁剪：', contextState(support.pruner_state || 'not_observed')),
          h('p', { role: 'status', style: styles.muted }, '当前会话历史回查：', contextState(history.state || 'not_observed')),
          h('p', { role: 'status', style: styles.muted }, '请求容量：', contextState(budget.last_reason === 'memory_brain_context_budget_exceeded' ? 'budget_blocked' : budget.state || budget.scope || 'awaiting_request'),
            Number.isFinite(budget.estimated_input_tokens ?? budget.estimated_tokens) ? `；参考估算 ${budget.estimated_input_tokens ?? budget.estimated_tokens} tokens` : '',
            Number.isFinite(budget.limit_tokens ?? budget.input_limit_tokens ?? budget.limit) ? `；参考额度 ${budget.limit_tokens ?? budget.input_limit_tokens ?? budget.limit} tokens` : '',
            budget.estimated_over_limit ? '；估算高于参考额度，实际用量以模型分词与回报为准' : ''),
          !contextPolicy ? h('p', { style: styles.notice }, '上下文设置暂未就绪，请重新检查状态。') : h('form', { onSubmit: saveContext },
            h('label', { style: { ...styles.row, marginBottom: 12 } }, h('input', {
              type: 'checkbox', checked: contextValues.contextEnabled, disabled: working,
              onChange: event => { contextDirty.current = true; const checked = event.target.checked; setContextValues(current => ({ ...current, contextEnabled: checked })); },
            }), '启用轮数整理与输入预算检查'),
            h('div', { style: styles.row }, contextInput('触发整理轮数（2～200）', 'contextTriggerTurns', 2, 200),
              contextInput('保留完整回合数（小于触发轮数）', 'contextKeepTurns', 1, 199)),
            h('details', null, h('summary', { style: { cursor: 'pointer', marginBottom: 10 } }, '高级：输入预算'),
              h('p', { style: styles.muted }, '上限留空时使用宿主提供的限制；额度为估算值，仍需遵守所配模型的实际窗口。'),
              h('div', { style: styles.row }, contextInput('输入参考额度（256～4194304 tokens，可留空）', 'contextMaxInputTokens', 256, 4194304, '根据宿主窗口估算'),
                contextInput('安全余量（256～65536 tokens）', 'contextSafetyTokens', 256, 65536))),
            h('details', null, h('summary', { style: { cursor: 'pointer', marginBottom: 10 } }, '压缩细节与历史回查'),
              Object.entries(contextFlags).map(([key, label]) => contextToggle(label, key)),
              h('p', { style: styles.muted }, '短摘要字数是模型目标，不能保证精确。工具裁剪使用原生机制并保留原日志；历史回查遵守接收范围、删除与披露设置。已有宿主自定义策略可能继续使用其自身参数。'),
              h('div', { style: styles.row }, contextNumbers.map(([key, label, min, max]) => contextInput(label, key, min, max)))),
            h('button', { type: 'submit', disabled: working || !status, style: { ...styles.button, opacity: working || !status ? 0.5 : 1 } }, '保存上下文设置'))) : null,
        h('section', { style: styles.section },
          h('h3', { style: styles.heading }, archiveOnly ? '3 · 启用归档并打开面板' : '4 · 启用并打开面板'),
          h('p', { style: styles.muted }, archiveOnly ? (active ? '请发一条新消息，再到面板确认原话入库。' : '准备完成后，点击启用归档。') : active ? 'DSH 接入已启用。面板可查看实际入库、索引和后台任务状态；这不代表历史对话已经自动迁入。' : '准备完成后启用。只有状态确认接入 DSH 后，才开始按当前设置处理新对话。'),
          runtime.restart_required ? h('p', { style: styles.notice }, '新模型配置尚未生效。请先结束当前对话，再点击“应用配置并重新连接”；只重启本插件自己的内核，不重启 DSH。') : null,
          contextPolicy?.restart_required && !runtime.restart_required ? h('p', { style: styles.notice }, '新的上下文设置尚未应用。请先结束当前对话，再点击“应用配置并重新连接”；本次只重新连接记忆插件。') : null,
          h('div', { style: styles.row },
            button(restartRequired ? '应用配置并重新连接' : archiveOnly ? '启用归档' : active ? '检查并启用' : '启用忆核大脑', () => perform('start', {}, '启用任务已提交，正在检查实际接入状态。'), { primary: true, disabled: working || !status || !prepared }),
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
