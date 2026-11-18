/** Reviewed module equivalence tests. Host files are treated only as source here. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import test from 'node:test';
import {probeRuntime} from '../adapters/dsh/host-compat.mjs';
import {syntaxFingerprint, methodSyntaxFingerprint} from '../adapters/dsh/host-source-fingerprint.mjs';
import {matchVerifiedLoop, NATIVE_LOOP_SHA256} from '../adapters/dsh/request-bridge.mjs';
import {supportsRequestMessages} from '../scripts/register-dsh.mjs';

const runtime = process.env.DSH_COMPAT_RUNTIME || process.env.DSH_RUNTIME;
assert(runtime, 'Set DSH_COMPAT_RUNTIME to the reviewed official rc.3 fixture');
const stock = fs.readFileSync(path.join(runtime, 'node_modules', '@deepseek-ai', 'dsh-agent-loop', 'lib', 'index.js'), 'utf8');
const root = fs.mkdtempSync(path.join(os.tmpdir(), 'memory-reviewed-syntax-'));
const files = {};
for (const label of ['dsh-agent-loop','dsh-session','dsh-llm','dsh-tools']) {
  const base = path.join(root, 'node_modules', '@deepseek-ai', label, 'lib');
  fs.mkdirSync(base, {recursive: true});
  files[label] = path.join(base, 'index.js');
  fs.writeFileSync(files[label], 'throw new Error("probe_must_not_execute_fixture");');
}
function probe(source) {
  fs.writeFileSync(files['dsh-agent-loop'], source);
  const result = probeRuntime(root);
  const publicInfo = JSON.stringify(result.compatibility);
  assert(!publicInfo.includes(root) && !publicInfo.includes('probe_must_not_execute_fixture'));
  return result;
}

test('reviewed exact source retains the fast raw match', ()=>{
  const result = probe(stock);
  assert.equal(result.compatibility.reason, 'verified_stock_source');
  assert.equal(result.bridgeSyntaxEquivalent, false);
});

test('changed module and method bytes with the same complete syntax are admitted', ()=>{
  const recolored = '// synthetic exporter banner\n' + stock
    .replace('async step(decision) {', 'async  step ( decision ) {\n  /* harmless in-method comment */')
    .replace('buildRequest(config, preparedCall, tools, position, startsRequestSeries, signal) {',
             'buildRequest (config, preparedCall, tools, position, startsRequestSeries, signal) {\n /* visual layout only */');
  assert(recolored.includes('harmless in-method comment'));
  assert(recolored.includes('visual layout only'));
  const result = probe(recolored);
  assert.equal(result.compatibility.reason, 'verified_syntax_equivalent');
  assert.equal(result.injectionMode, 'scoped_agent_bridge');
  assert.equal(result.bridgeSyntaxEquivalent, true);
  assert.notEqual(result.compatibility.loop_canonical_sha256, result.bridgeSourceSha256);
  assert.equal(result.bridgeSourceSha256, matchVerifiedLoop(stock).sourceSha256);
});

test('real syntax changes in a reviewed source still require an audit', ()=>{
  const changed = probe(stock + '\nvoid 0;\n');
  assert.equal(changed.compatibility.reason, 'unknown_loop_fingerprint');
  assert.equal(changed.supported, false);
  assert.equal(changed.bridgeSourceSha256, undefined);
});

test('comments, strings and methods that mimic native markers remain unverified', ()=>{
  for (const sample of [
    '// withRequestMessages(x) this.withRequestMessages(x) decision.requestMessages\n',
    'const fake = "withRequestMessages(x) this.withRequestMessages(x) decision.requestMessages";',
    'class Loop { withRequestMessages(x) { return x; } step(decision) { return this.withRequestMessages(decision.requestMessages); } }',
  ]) {
    assert.equal(probe(sample).compatibility.reason, 'unknown_loop_fingerprint');
    assert.equal(supportsRequestMessages(sample), false);
  }
});

test('automatic semicolon insertion, literals, templates, directives and parse errors are distinguished', ()=>{
  assert.notEqual(methodSyntaxFingerprint('step(){return 1}'), methodSyntaxFingerprint('step(){return\n1}'));
  assert.notEqual(syntaxFingerprint("const x = '/* inside data */';"), syntaxFingerprint("const x = '';"));
  assert.notEqual(syntaxFingerprint('const rx = /foo/u;'), syntaxFingerprint('const rx = /foo/i;'));
  assert.notEqual(syntaxFingerprint('const x = `a ${value}`;'), syntaxFingerprint('const x = `b ${value}`;'));
  assert.notEqual(syntaxFingerprint('"use strict"; func();'), syntaxFingerprint('func();'));
  assert.equal(syntaxFingerprint('var = ;'), null);
  assert.equal(syntaxFingerprint(' '.repeat(512 * 1024 + 1)), null);
});

if (process.env.DSH_NATIVE_COMPAT_LOOP) test('reviewed native implementation and its layout variant retain native injection', ()=>{
  const source = fs.readFileSync(process.env.DSH_NATIVE_COMPAT_LOOP, 'utf8');
  const checked = probe(source);
  assert.equal(checked.compatibility.reason, 'verified_native_snapshot');
  assert.equal(checked.bridgeSourceSha256, NATIVE_LOOP_SHA256);
  const equivalent = '/* native layout comment */\n' + source.replace('withRequestMessages(request, messages) {',
    'withRequestMessages(request, messages) { /* per-request helper comment */ ');
  const altered = probe(equivalent);
  assert.equal(altered.compatibility.reason, 'verified_syntax_equivalent');
  assert.equal(altered.injectionMode, 'native_snapshot');
  assert.equal(altered.bridgeSourceSha256, NATIVE_LOOP_SHA256);
});
