/** Bounded discovery of the running host, including Electron's virtual ASAR FS.
 * No extraction, package download, disk scan or compatibility bypass happens here.
 */
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';

const safeVersion = value => typeof value === 'string' && /^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$/.test(value) ? value : null;
const asarPath = value => /\.asar(?:[\\/]|$)/i.test(value);
function absolute(value) {
  if (typeof value !== 'string' || !value || value.includes('\0')) return null;
  try { if (value.startsWith('file:')) value = fileURLToPath(value); } catch { return null; }
  return path.isAbsolute(value) ? path.resolve(value) : null;
}
function desktopRuntimeArgument(argv) {
  const entry = absolute(argv[1]), declared = absolute(argv[2]);
  if (!entry || !declared || !/[\\/]node_modules[\\/]@deepseek-ai[\\/]dsh-desktop-host[\\/]lib[\\/](?:index|cli)\.js$/i.test(entry)) return null;
  const expected = path.resolve(path.dirname(entry), '../../../..');
  const equal = process.platform === 'win32' ? expected.toLowerCase() === declared.toLowerCase() : expected === declared;
  return equal ? declared : null;
}

export function discoverHostRuntime({allowUnverified = false, probeRuntime, inspectRuntime, pluginUrl,
  environment = process.env, argv = process.argv, cwd = process.cwd(), execPath = process.execPath,
  resourcesPath = process.resourcesPath, electronVersion = process.versions.electron} = {}) {
  if (typeof probeRuntime !== 'function' || typeof inspectRuntime !== 'function') throw new TypeError('runtime_discovery_inspectors_required');
  const inspect = allowUnverified ? probeRuntime : inspectRuntime;
  const electron = safeVersion(electronVersion), seen = new Set();let count = 0;
  const decorate = (result, source, candidate) => {
    result.compatibility = {...result.compatibility, discovery:{source,
      layout: candidate ? asarPath(candidate) ? 'asar' : 'directory' : 'unknown', candidate_count:count, electron_version:electron}};
    return result;
  };
  const available = root => {
    try { return fs.statSync(root).isDirectory() && ['dsh-agent-loop','dsh-session'].some(name =>
      fs.existsSync(path.join(root,'node_modules','@deepseek-ai',name,'lib','index.js'))); }
    catch { return false; }
  };
  const candidate = (value, source) => {
    const root = absolute(value);if (!root) return null;
    const key = process.platform === 'win32' ? root.toLowerCase() : root;
    if (seen.has(key)) return null;seen.add(key);count++;
    if (!available(root)) return null;
    return decorate(inspect(root),source,root);
  };
  const moduleAnchor = (value, source) => {
    const anchor = absolute(value);if (!anchor) return null;
    let loop;try { loop = createRequire(anchor).resolve('@deepseek-ai/dsh-agent-loop'); } catch { return null; }
    return candidate(path.resolve(path.dirname(loop),'../../../..'),source);
  };
  const configured = environment?.XIAOJIU_DSH_RUNTIME;
  if (configured) {
    const root = path.resolve(configured);count++;
    return decorate(inspect(root),'environment',root);
  }
  // Desktop's shipped Host passes its immutable runtime as argv[2]. Accept it
  // only when the actual desktop-host entry names that exact sibling tree.
  let found = candidate(desktopRuntimeArgument(argv),'desktop_host_argument');if (found) return found;
  found = moduleAnchor(argv[1],'host_module_anchor');if (found) return found;
  const entry = absolute(argv[1]);
  if (entry) {
    const match = entry.match(/^(.*?\.asar)(?:[\\/]|$)/i);
    if (match) for (const root of [path.join(match[1],'dsh'),match[1]]) {
      found = candidate(root,'asar_host_entry');if (found) return found;
    }
  }
  const resources = absolute(resourcesPath);
  if (electron && resources) for (const root of [path.join(resources,'app.asar','dsh'),path.join(resources,'app.asar')]) {
    found = candidate(root,'electron_resources');if (found) return found;
  }
  const executable = absolute(execPath);
  if (electron && executable) for (const base of [path.join(path.dirname(executable),'resources'),path.resolve(path.dirname(executable),'../Resources')]) {
    for (const root of [path.join(base,'app.asar','dsh'),path.join(base,'app.asar')]) {
      found = candidate(root,'electron_executable');if (found) return found;
    }
  }
  found = moduleAnchor(pluginUrl,'plugin_module_anchor');if (found) return found;
  found = moduleAnchor(path.join(cwd,'package.json'),'working_directory_anchor');if (found) return found;
  if (allowUnverified) {
    const result = probeRuntime(path.resolve(cwd));result.supported = false;result.injectionMode = 'diagnostic_only';
    result.compatibility = {...result.compatibility,supported:false,mode:'diagnostic_only',reason:'dsh_runtime_not_resolved'};
    return decorate(result,'unresolved',null);
  }
  throw new Error('dsh_runtime_not_resolved_use_the_connection_launcher');
}
