/** Exact build/method selection only. No AgentLoop method body is executed.
 * Supply external, read-only host sources; complete third-party sources are not
 * embedded in this package. This is not an Android/device integration test.
 *
 * DSH_VARIANT_LOOP_FILE=<known lib/index.js>
 * DSH_LLM_MODULE=<compatible host llm lib/index.js>
 * node tests/test_verified_build_selection.mjs
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import vm from 'node:vm';
import { pathToFileURL } from 'node:url';
import { createRequestBridge, VERIFIED_LOOP_BUILDS } from '../adapters/dsh/request-bridge.mjs';
import { probeRuntime } from '../adapters/dsh/host-compat.mjs';

const sha256 = value => crypto.createHash('sha256').update(value).digest('hex');
const canonical = value => value.replace(/^\uFEFF/, '').replace(/\r\n/g, '\n');
const methodHash = fn => sha256(Function.prototype.toString.call(fn).replace(/\r\n/g, '\n'));

async function main() {
  const loopFile = process.env.DSH_VARIANT_LOOP_FILE, llmFile = process.env.DSH_LLM_MODULE;
  if (!loopFile || !llmFile) {
    console.log('SKIP verified build selection: set DSH_VARIANT_LOOP_FILE and DSH_LLM_MODULE to existing external host files.');
    console.log(JSON.stringify({ passed: 0, failed: 0, skipped: 1, method_bodies_executed: 0, real_model_calls: 0 }));
    return;
  }

  const bytes = fs.readFileSync(path.resolve(loopFile));
  const source = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
  const sourceSha256 = sha256(bytes), canonicalSha256 = sha256(canonical(source));
  const selectedSha256 = Object.hasOwn(VERIFIED_LOOP_BUILDS, sourceSha256) ? sourceSha256 : canonicalSha256;
  assert.ok(Object.hasOwn(VERIFIED_LOOP_BUILDS, selectedSha256), 'external source is not a verified exact build or BOM/CRLF representation');
  const selected = VERIFIED_LOOP_BUILDS[selectedSha256];
  const llm = await import(llmFile.startsWith('file:') ? llmFile : pathToFileURL(path.resolve(llmFile)).href);
  for (const key of ['createUserMessage', 'markAgentLoopRequest', 'isAgentLoopRequest']) assert.equal(typeof llm[key], 'function');

  let executedBodies = 0;
  const forbidden = () => { executedBodies++; throw new Error('AgentLoop body execution is forbidden in this test'); };
  const globals = Object.freeze({ forbidden, executeToolCalls: forbidden, canonicalHeader: forbidden,
    renderPrompt: forbidden, renderContextSections: forbidden, assembleContextFor: forbidden,
    joinContextSections: forbidden, createAssistantMessage: forbidden, createDeveloperMessage: forbidden,
    markAgentLoopRequest: forbidden, deepFreeze: forbidden, AssistantStreamAttempt: forbidden, LlmError: forbidden });

  function methodsFromSource(text) {
    const normalized = canonical(text);
    const buildStart = normalized.indexOf('\tbuildRequest(config, preparedCall, tools, position, startsRequestSeries, signal) {');
    const buildEnd = normalized.indexOf('\n\t}\n', buildStart);
    const stepStart = normalized.indexOf('\tasync step(decision) {');
    const stepEnd = normalized.indexOf('\n\t}\n', stepStart);
    assert.ok(buildStart >= 0 && buildEnd > buildStart && stepStart >= 0 && stepEnd > stepStart, 'known method boundaries required');
    const snippets = {
      buildRequest: normalized.slice(buildStart + 1, buildEnd + '\n\t}'.length),
      step: normalized.slice(stepStart + 1, stepEnd + '\n\t}'.length),
    };
    const methods = {};
    for (const [name, snippet] of Object.entries(snippets)) {
      // Creating these function objects does not invoke their bodies. Keep the
      // actual CRLF representation so the bridge's narrow normalizer is tested.
      const represented = text.includes('\r\n') ? snippet.replace(/\n/g, '\r\n') : snippet;
      methods[name] = vm.runInNewContext('({' + represented + '})', globals, {
        timeout: 1000, contextCodeGeneration: { strings: false, wasm: false },
      })[name];
    }
    return methods;
  }

  const actualMethods = methodsFromSource(source);
  function fixture(methods = actualMethods) {
    const controller = new AbortController(), agent = Object.create(methods);
    agent.id = 'synthetic-build-selection';
    agent.phase = { kind: 'running', turn: 1, step: 1, abort: controller };
    const payload = { agent, turn: 1, step: 1, signal: controller.signal };
    return { agent, controller, payload, decision: { kind: 'enter', requestMessages: ['synthetic evidence'] } };
  }

  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'memory-brain-build-selection-'));
  const names = { loop: 'dsh-agent-loop', session: 'dsh-session', llm: 'dsh-llm', tools: 'dsh-tools', connection: 'dsh-client-connection' };
  function runtime(code) {
    const directory = fs.mkdtempSync(path.join(temporary, 'host-'));
    for (const [key, name] of Object.entries(names)) {
      const module = path.join(directory, 'node_modules', '@deepseek-ai', name);
      fs.mkdirSync(path.join(module, 'lib'), { recursive: true });
      fs.writeFileSync(path.join(module, 'package.json'), JSON.stringify({ name: '@deepseek-ai/' + name, version: '0.0.0-synthetic' }));
      fs.writeFileSync(path.join(module, 'lib/index.js'), key === 'loop' ? code : 'throw new Error("HOST_FIXTURE_MUST_NOT_EXECUTE");\n');
    }
    return directory;
  }
  const cases = [], check = (name, run) => cases.push({ name, run });

  check('external exact source selects its own independently verified method pair', () => {
    assert.equal(methodHash(actualMethods.step), selected.methods.step);
    assert.equal(methodHash(actualMethods.buildRequest), selected.methods.buildRequest);
    const result = probeRuntime(runtime(bytes));
    assert.equal(result.supported, true);
    assert.equal(result.injectionMode, 'scoped_agent_bridge');
    assert.equal(result.compatibility.loop_canonical_sha256, selectedSha256);
  });

  check('actual method pair can stage without running either method body', () => {
    const f = fixture(), bridge = createRequestBridge({ llm, sourceSha256: selectedSha256 });
    try {
      assert.equal(bridge.stage(f.payload, f.decision).staged, true);
      assert.equal(bridge.status().instances, 1);
      assert.equal(bridge.status().pending, 1);
      assert.equal(executedBodies, 0);
      assert.notEqual(f.agent.step, actualMethods.step);
      assert.equal(Object.getPrototypeOf(f.agent).step, actualMethods.step);
    } finally { bridge.dispose(); }
    assert.equal(Object.hasOwn(f.agent, 'step'), false);
    assert.equal(Object.hasOwn(f.agent, 'buildRequest'), false);
  });

  check('an unknown build hash refuses the actual otherwise-known pair', () => {
    const f = fixture(), bridge = createRequestBridge({ llm, sourceSha256: '0'.repeat(64) });
    try {
      const result = bridge.stage(f.payload, f.decision);
      assert.equal(result.staged, false); assert.equal(result.reason, 'host_loop_build_unverified');
      assert.equal(bridge.status().active, false);
      assert.equal(Object.hasOwn(f.agent, 'step'), false);
    } finally { bridge.dispose(); }
  });

  check('another known source hash cannot borrow this build-specific step method', () => {
    const wrong = Object.keys(VERIFIED_LOOP_BUILDS).find(key => VERIFIED_LOOP_BUILDS[key].methods.step !== selected.methods.step);
    assert.ok(wrong, 'the fixture needs a distinct verified method pair');
    const f = fixture(), bridge = createRequestBridge({ llm, sourceSha256: wrong });
    try {
      const result = bridge.stage(f.payload, f.decision);
      assert.equal(result.staged, false); assert.equal(result.reason, 'unknown_step_method');
      assert.equal(Object.hasOwn(f.agent, 'step'), false);
    } finally { bridge.dispose(); }
  });

  check('one unknown method body is rejected before it could execute', () => {
    const altered = vm.runInNewContext('({ async step(decision) { forbidden(); } })', globals).step;
    const f = fixture({ ...actualMethods, step: altered }), bridge = createRequestBridge({ llm, sourceSha256: selectedSha256 });
    try {
      assert.equal(bridge.stage(f.payload, f.decision).reason, 'unknown_step_method');
      assert.equal(executedBodies, 0);
      assert.equal(Object.hasOwn(f.agent, 'step'), false);
    } finally { bridge.dispose(); }
  });

  check('one unknown buildRequest method is independently rejected', () => {
    const altered = vm.runInNewContext('({ buildRequest() { forbidden(); } })', globals).buildRequest;
    const f = fixture({ ...actualMethods, buildRequest: altered }), bridge = createRequestBridge({ llm, sourceSha256: selectedSha256 });
    try { assert.equal(bridge.stage(f.payload, f.decision).reason, 'unknown_buildRequest_method'); assert.equal(executedBodies, 0); }
    finally { bridge.dispose(); }
  });

  check('only initial BOM and CRLF representation variants retain this build identity', () => {
    const raw = canonical(source);
    for (const text of ['\uFEFF' + raw, raw.replace(/\n/g, '\r\n'), '\uFEFF' + raw.replace(/\n/g, '\r\n')]) {
      const report = probeRuntime(runtime(text)).compatibility;
      assert.equal(report.supported, true); assert.equal(report.mode, 'scoped_agent_bridge');
      assert.equal(report.reason, 'verified_stock_source_formatting');
      assert.notEqual(report.loop_sha256, selectedSha256);
      assert.equal(report.loop_canonical_sha256, selectedSha256);
      const representedMethods = methodsFromSource(text);
      const f = fixture(representedMethods), bridge = createRequestBridge({ llm, sourceSha256: report.loop_canonical_sha256 });
      try { assert.equal(bridge.stage(f.payload, f.decision).staged, true); assert.equal(executedBodies, 0); }
      finally { bridge.dispose(); }
    }
  });

  check('comments, extra spaces, bare CR and changed code never pass the full source probe', () => {
    const raw = canonical(source);
    for (const text of [raw + ' ', raw + '\n// synthetic extra comment\n', raw.replace('\n', '\r'),
      raw.replace('\tasync step(decision) {', '\tasync step(decision) { '),
      raw.replace('\tbuildRequest(config,', '\tdifferentRequest(config,')]) {
      const report = probeRuntime(runtime(text)).compatibility;
      assert.equal(report.supported, false); assert.equal(report.mode, 'diagnostic_only');
      assert.equal(report.reason, 'unknown_loop_fingerprint');
    }
  });

  check('diagnostic projection exposes safe version/hash metadata without fixture paths or source text', () => {
    const directory = runtime(bytes), report = probeRuntime(directory).compatibility;
    assert.equal(report.modules.loop.version, '0.0.0-synthetic');
    assert.match(report.modules.loop.sha256, /^[a-f0-9]{64}$/);
    const serialized = JSON.stringify(report);
    assert.ok(!serialized.includes(directory));
    assert.ok(!serialized.includes('async step'));
    assert.ok(!serialized.includes('HOST_FIXTURE_MUST_NOT_EXECUTE'));
  });

  check('stage-only test never executes host methods and never changes the supplied source', () => {
    assert.equal(executedBodies, 0);
    assert.equal(sha256(fs.readFileSync(path.resolve(loopFile))), sourceSha256);
  });

  let failed = 0;
  try {
    for (const item of cases) {
      try { await item.run(); console.log('PASS ' + item.name); }
      catch (error) { failed++; console.error('FAIL ' + item.name + '\n' + error.stack); }
    }
    console.log(JSON.stringify({ passed: cases.length - failed, failed, skipped: 0, verified_build: selected.name,
      source_sha256: sourceSha256, selected_canonical_sha256: selectedSha256,
      method_bodies_executed: executedBodies, real_model_calls: 0, staged_only: true, real_device_verified: false }));
    if (failed) process.exitCode = 1;
  } finally {
    assert.equal(path.dirname(temporary), path.resolve(os.tmpdir()));
    assert.ok(path.basename(temporary).startsWith('memory-brain-build-selection-'));
    fs.rmSync(temporary, { recursive: true, force: true });
  }
}

await main();
