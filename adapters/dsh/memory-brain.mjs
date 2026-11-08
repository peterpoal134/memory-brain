/** Public DSH entry: load local-only configuration, check the host, start the
 * independent kernel if requested, then install the existing evidence adapter.
 * No host patches, personal presets or credentials are shipped in this module.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';
import { createAdapter, getAdapter } from './xiaojiu-brain.mjs';
import { createRequestBridge, STOCK_LOOP_SHA256 } from './request-bridge.mjs';

export const name = 'memory-brain';
export const inject = [];
export { getAdapter };
const execute = promisify(execFile);
const packageRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');

export function discoverRuntime() {
  if (process.env.XIAOJIU_DSH_RUNTIME) return inspectRuntime(path.resolve(process.env.XIAOJIU_DSH_RUNTIME));
  const anchors = [process.argv[1], fileURLToPath(import.meta.url), path.join(process.cwd(), 'package.json')];
  for (const anchor of anchors) {
    if (!anchor || !path.isAbsolute(anchor)) continue;
    let loop;
    try { loop = createRequire(anchor).resolve('@deepseek-ai/dsh-agent-loop'); } catch { continue; }
    // Resolve only from the host's own module search scope; never scan disks or
    // silently download a second copy of the DSH runtime.
    const candidate = path.resolve(path.dirname(loop), '../../../..');
    if (fs.existsSync(path.join(candidate, 'node_modules/@deepseek-ai/dsh-session/lib/index.js'))) return inspectRuntime(candidate);
  }
  throw new Error('dsh_runtime_not_resolved_use_the_connection_launcher');
}

export function inspectRuntime(runtimePath) {
  if (typeof runtimePath !== 'string' || !path.isAbsolute(runtimePath)) throw new Error('dsh_runtime_absolute_path_required');
  const runtime = fs.realpathSync(runtimePath);
  const moduleFile = name => path.join(runtime, 'node_modules', '@deepseek-ai', name, 'lib', 'index.js');
  const loop = moduleFile('dsh-agent-loop');
  for (const name of ['dsh-agent-loop', 'dsh-session', 'dsh-tools', 'dsh-llm']) if (!fs.existsSync(moduleFile(name))) throw new Error('dsh_runtime_modules_missing');
  const code = fs.readFileSync(loop, 'utf8');
  const nativeSnapshot = /\bwithRequestMessages\s*\(/.test(code) && /this\.withRequestMessages\s*\(/.test(code) &&
      /decision\.requestMessages\b/.test(code);
  const stockBridge = createHash('sha256').update(code).digest('hex') === STOCK_LOOP_SHA256;
  if (!nativeSnapshot && !stockBridge) throw new Error('当前DSH构建尚未通过记忆请求兼容验证（dsh_request_messages_hook_required）；没有修改宿主或开始采集。');
  return { runtime, sessionModule: pathToFileURL(moduleFile('dsh-session')).href,
    toolsModule: pathToFileURL(moduleFile('dsh-tools')).href,
    llmModule: pathToFileURL(moduleFile('dsh-llm')).href,
    injectionMode: nativeSnapshot ? 'native_snapshot' : 'scoped_agent_bridge' };
}

export function resolveConfiguration(config = {}) {
  if (config.enabled !== true) return { enabled: false };
  if (config.requestBridge !== undefined) throw new Error('request_bridge_is_owned_by_the_verified_public_entry');
  if (Number(process.versions.node.split('.')[0]) < 22) throw new Error('node_22_or_later_required');
  if (config.token !== undefined || config.baseUrl !== undefined) throw new Error('use_local_brain_config_instead_of_inline_token_or_url');
  const configFile = path.resolve(config.brainConfigFile || path.join(packageRoot, 'private', 'config.json'));
  const local = JSON.parse(fs.readFileSync(configFile, 'utf8').replace(/^\uFEFF/, ''));
  if (!['127.0.0.1', 'localhost', '::1'].includes(local.host) || !Number.isInteger(local.port) || local.port < 1024 || local.port > 65535) throw new Error('local_brain_address_invalid');
  if (typeof local.api_token !== 'string' || local.api_token.length < 24) throw new Error('local_brain_token_invalid');
  const runtime = config.runtimePath ? inspectRuntime(config.runtimePath) : discoverRuntime();
  const data = path.resolve(path.dirname(configFile), local.data_dir || './data');
  const namespace = config.queueNamespace || 'default';
  if (!/^[a-zA-Z0-9_-]{1,80}$/.test(namespace)) throw new Error('queue_namespace_invalid');
  const options = { ...config, ...runtime, configFile, packageRoot,
    baseUrl: `http://${local.host === '::1' ? '[::1]' : local.host}:${local.port}`, token: local.api_token,
    queueDir: path.join(data, 'dsh-queue', namespace) };
  delete options.runtime;
  return options;
}

async function ready(options) {
  try {
    const response = await fetch(options.baseUrl + '/v1/status', { redirect: 'error', signal: AbortSignal.timeout(1500),
      headers: { Authorization: `Bearer ${options.token}` } });
    const value = await response.json();
    return response.ok && value.ok === true && typeof value.data?.version === 'string' && typeof value.data?.build_id === 'string';
  } catch { return false; }
}

export async function apply(ctx, config = {}) {
  if (config.enabled !== true) return;
  // Exceptions here fail this plugin visibly, without modifying the host or
  // quietly enabling capture on an unsupported request-injection contract.
  const options = resolveConfiguration(config);
  // The legacy bridge is per-agent, version/fingerprint gated and process local.
  // It never edits the host bundle, prototype or durable message history.
  const bridge = options.injectionMode === 'scoped_agent_bridge' ?
    createRequestBridge({ llm: await import(options.llmModule) }) : null;
  if (bridge) options.requestBridge = bridge;
  try {
  if (!await ready(options)) {
    if (config.autoStart === false) throw new Error('memory_brain_not_ready_start_the_local_launcher');
    if (process.platform !== 'win32') throw new Error('automatic_start_windows_only_start_kernel_manually');
    const expectedConfig = path.join(packageRoot, 'private', 'config.json');
    if (path.resolve(options.configFile).toLowerCase() !== expectedConfig.toLowerCase()) throw new Error('custom_config_requires_manual_kernel_start');
    try {
      await execute('powershell.exe', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
        '-File', path.join(packageRoot, 'scripts', 'start-local.ps1'), '-NoOpen'],
      { cwd: packageRoot, windowsHide: true, timeout: 30000, maxBuffer: 65536 });
    } catch { throw new Error('memory_brain_start_failed_see_local_service_log'); }
    if (!await ready(options)) throw new Error('memory_brain_readiness_not_confirmed');
  }
  const controller = await createAdapter(ctx, options);
  if (!controller.enabled) throw new Error('memory_brain_adapter_not_enabled_see_dsh_log');
  // Unloading removes the adapter only. The kernel may serve another host or
  // an open panel, so it is never killed or purged by a DSH plugin disposer.
  } catch (error) { bridge?.dispose(); throw error; }
}
