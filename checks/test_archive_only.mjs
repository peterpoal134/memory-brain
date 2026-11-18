/** Host read-only DSH event-path regressions with a synthetic, authenticated receiver. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';
import { pathToFileURL } from 'node:url';
import { canUseArchiveSessionEvents, createAdapter } from '../adapters/dsh/xiaojiu-brain.mjs';

const tmp = fs.realpathSync(os.tmpdir());
const root = fs.mkdtempSync(path.join(tmp, 'brain-archive-only-'));
const instances = [];
const passed = [];
const cutoff = Date.now() - 1000;
const check = (name, action) => { action(); passed.push(name); console.log('PASS ' + name); };
const message = (id, text = id, extra = {}) => ({ id, role:'user', source:{kind:'user'},
  content:[{type:'text',text}], ...extra });
const response = (status, data) => ({ ok:status===200, status, json:async()=>data });
const event = (seq, type, data, time = cutoff + 10 + seq) => ({type,seq,time,data});

class PublicContext {
  constructor() {
    this.live = new Map();
    this.hooks = new Map();
    this.listCalls = 0;
    this.sessions = { list:()=>{ this.listCalls++; return [...this.live.values()]; }, get:id=>this.live.get(id) };
    this.logger = { info(){}, warn(){} };
  }
  get(key) { assert.equal(key,'sessions'); return this.sessions; }
  on(name, callback) {
    assert.notEqual(name,'agent/pre-step');
    if (!this.hooks.has(name)) this.hooks.set(name,new Set());
    const set = this.hooks.get(name);
    set.add(callback);
    return () => { set.delete(callback); if (!set.size) this.hooks.delete(name); };
  }
  emit(name, ...args) { for (const callback of this.hooks.get(name) ?? []) callback(...args); }
  attach(session) { this.live.set(session.id,session); this.emit('session/created',session); }
  release(session) { this.live.delete(session.id); this.emit('session/disposed',session); }
  inject() { throw new Error('archive_must_not_inject_tools_or_agent_services'); }
}

function session(id, log = [], extra = {}) {
  return Object.freeze({ id, snapshotEvents:()=>Object.freeze([...log]),
    append(){ throw new Error('archive_must_not_append'); }, ...extra });
}

const reply = response(200,{ok:true,data:{accepted:true}});
function receiver() {
  const sent = [];
  return { sent, online:true, fetch:async function(url, request) {
    assert.equal(url,'http://127.0.0.1:1/v1/ingest');
    assert.equal(request.headers.Authorization,'Bearer temporary-archive-only-token');
    if (!this.online) return response(503,{ok:false});
    sent.push(JSON.parse(request.body)); return reply;
  } };
}

async function adapter(ctx, dir, sink, extra = {}) {
  const unused = { status(){ throw new Error('unexpected_request_injection_status'); },
    stop(){ throw new Error('unexpected_context_or_budget_stop'); },
    dispose(){ throw new Error('unexpected_request_bridge_dispose'); } };
  const value = await createAdapter(ctx,{ enabled:true, archiveOnly:true, archiveCaptureAfter:cutoff,
    baseUrl:'http://127.0.0.1:1', token:'temporary-archive-only-token', fetchImpl:sink.fetch.bind(sink),
    queueDir:path.join(root,dir), sessionModule:path.join(root,'prohibited-session-import.mjs'),
    requestBudget:unused, requestBridge:unused, compactionSupport:unused, intervalMs:50, ...extra });
  instances.push(value);
  return value;
}

async function waitFor(condition, label) {
  const deadline = Date.now() + 2500;
  while (!condition() && Date.now()<deadline) await sleep(20);
  assert.ok(condition(),label);
}

try {
  const missing = new PublicContext();
  missing.sessions = undefined;
  const asyncList = new PublicContext();
  asyncList.sessions.list = async () => { throw new Error('later_failure'); };
  const asyncGet = new PublicContext();
  asyncGet.sessions.get = async () => { throw new Error('later_failure'); };
  const inactive = await adapter(missing,'no-service',receiver());
  const asynchronous = await adapter(asyncList,'async-service',receiver());
  const asynchronousLookup = await adapter(asyncGet,'async-lookup',receiver());
  check('missing or asynchronous public session service fails before queue creation', () => {
    assert.equal(canUseArchiveSessionEvents(missing),false);
    assert.equal(inactive.enabled,false);
    assert.equal(asynchronous.enabled,false);
    assert.equal(asynchronousLookup.enabled,false);
    assert.equal(fs.existsSync(path.join(root,'no-service')),false);
    assert.equal(fs.existsSync(path.join(root,'async-service')),false);
    assert.equal(fs.existsSync(path.join(root,'async-lookup')),false);
  });

  const ctx = new PublicContext(), sink = receiver();
  const events = [
    event(0,'user/message',message('fork-inherited')),
    event(1,'user/message',message('at-cutoff'),cutoff),
    event(2,'agent/inbox/spliced',{removedCount:1,target:'next-turn',inserted:[message('uncommitted')]}),
    event(3,'user/message',message('injected','no archive',{source:{kind:'user',form:'snapshot'}})),
    event(4,'tool/result',{sensitive:'no archive'}),
    event(5,'assistant/message',{interrupted:true,message:message('interrupted','omit',{role:'assistant',source:{kind:'model'}})}),
    event(6,'user/message',message('first-human')),
    event(7,'assistant/message',{message:message('visible-assistant','visible',{role:'assistant',source:{kind:'model'},
      content:[{type:'text',text:'visible'},{type:'reasoning',text:'do not archive'}]})}),
  ];
  let snapshots = 0;
  const visible = session('owned',events,{header:{isSeeded:true},inheritedEventCount:1,
    snapshotEvents:()=>{ snapshots++; return Object.freeze([...events]); }});
  const originalAppend = visible.append;
  ctx.attach(visible);
  const value = await adapter(ctx,'owned',sink);
  assert.ok(value.enabled);
  await waitFor(() => sink.sent.length===2,'expected committed originals');
  check('archive uses public sessions without import or append wrapping', () => {
    assert.equal(canUseArchiveSessionEvents(ctx),true);
    assert.equal(visible.append,originalAppend);
    assert.deepEqual(value.status().request_injection,{mode:'archive_only',active:false});
    assert.equal(value.status().capture.mode,'session_events');
    assert.equal(value.status().chat_usage.enabled,false);
    assert.equal(value.recall,undefined);
  });
  check('archive skips inherited, historical, uncommitted and synthetic events', () => {
    assert.deepEqual(sink.sent.map(row=>[row.conversation_id,row.message_id,row.text]),[
      ['owned','first-human','first-human'],['owned','visible-assistant','visible'],
    ]);
    assert.equal(value.queue.cursor('owned').index,7);
  });
  const scanned = snapshots, elapsedTicks = ctx.listCalls + 3;
  await waitFor(() => ctx.listCalls >= elapsedTicks,'idle ticks continue');
  check('idle archive ticks do not reread an already caught up session', () => assert.equal(snapshots,scanned));
  const next = event(events.length,'user/message',message('next-human'));
  events.push(next);
  ctx.emit('session/event',visible,next);
  await waitFor(() => sink.sent.length===3,'new committed event resumes processing');
  check('public event reactivates the session after idle', () => {
    assert.equal(sink.sent.at(-1).message_id,'next-human');
    assert.equal(snapshots,scanned+1);
  });

  const spoof = session('owned',[event(0,'user/message',message('same-id-spoof'))]);
  const foreign = session('foreign',[event(0,'user/message',message('foreign'))]);
  ctx.emit('session/event',spoof,spoof.snapshotEvents()[0]);
  ctx.emit('session/created',foreign);
  await sleep(80);
  check('session capability rejects foreign and same-ID spoof objects', () => {
    assert.equal(sink.sent.length,3);
    assert.equal(value.status().capture.attached_sessions,1);
  });

  const bad = session('bad',[],{snapshotEvents:async()=>{throw new Error('unsupported_snapshot');}});
  const badSequence = session('bad-seq',[event(23,'user/message',message('malformed'))]);
  const badSeed = session('bad-seed',[event(0,'user/message',message('unmarked-inheritance'))],{header:{isSeeded:true}});
  ctx.attach(bad); ctx.attach(badSequence); ctx.attach(badSeed);
  await waitFor(() => value.warnings.some(w=>w.code==='archive_session_async_api_unsupported') &&
    value.warnings.some(w=>w.code==='archive_session_event_unsupported') &&
    value.warnings.some(w=>w.code==='archive_session_snapshot_unsupported'),'diagnose invalid host data');
  check('malformed or asynchronous snapshots never reach the durable queue', () => {
    assert.equal(sink.sent.length,3);
    for (const name of ['bad','bad-seq','bad-seed']) assert.equal(value.queue.cursor(name),null);
  });
  await value.stop();
  check('unload unregisters archive observers and releases its writer', () => {
    assert.equal(ctx.hooks.size,0);
    assert.equal(value.status().capture.attached_sessions,0);
    assert.equal(fs.existsSync(path.join(root,'owned','writer.lock')),false);
    assert.equal(visible.append,originalAppend);
  });

  const quiet = new PublicContext(), quietSink = receiver();
  const capped = await adapter(quiet,'quiet',quietSink,{maxPending:1});
  quiet.attach(session('quiet-capacity',Array.from({length:3},(_,i)=>event(i,'user/message',message('q'+i)))));
  await waitFor(() => quietSink.sent.length===3,'dirty session retries after queue backpressure');
  check('capacity-limited background capture completes without another event', () => {
    assert.deepEqual(quietSink.sent.map(row=>row.message_id),['q0','q1','q2']);
    assert.equal(capped.queue.cursor('quiet-capacity').index,2);
  });
  await capped.stop();

  const offline = receiver(); offline.online = false;
  const resumed = new PublicContext();
  const overflow = session('backlog',[
    event(0,'user/message',message('queued')),event(1,'user/message',message('disposed')),
  ]);
  const first = await adapter(resumed,'backlog',offline,{maxPending:1});
  resumed.attach(overflow);
  await waitFor(() => first.queue.stats().pending===1,'offline event must remain pending');
  resumed.release(overflow);
  check('disposal persists overflow without overwriting the pending original', () => {
    assert.equal(first.queue.stats().pending,1);
    assert.equal(first.queue.cursor('backlog').index,1);
    assert.equal(fs.readdirSync(path.join(root,'backlog','session-recovery')).length,1);
    assert.equal(first.status().capture.attached_sessions,0);
  });
  await first.stop();
  offline.online = true;
  const second = await adapter(new PublicContext(),'backlog',offline,{maxPending:1});
  await waitFor(() => offline.sent.length===2,'both offline originals delivered after restart');
  check('offline retry and disposal recovery survive restart in order', () => {
    assert.deepEqual(offline.sent.map(row=>row.message_id),['queued','disposed']);
    assert.equal(second.queue.stats().pending,0);
  });

  if (process.env.DSH_RUNTIME) {
    const load = name => import(pathToFileURL(path.join(process.env.DSH_RUNTIME,'node_modules','@deepseek-ai',name,'lib','index.js')).href);
    const [{Context},{SessionStore}] = await Promise.all([load('cordis'),load('dsh-session')]);
    const nativeContext = new Context();
    const fiber = nativeContext.plugin(SessionStore); await fiber.await();
    const nativeSink = receiver();
    const prepare = SessionStore.prototype.prepare;
    const native = await adapter(nativeContext,'native',nativeSink);
    try {
      const live = nativeContext.sessions.prepare('native-archive');
      const append = live.append;
      const release = nativeContext.sessions.enter(live);
      nativeContext.sessions.announce(live);
      live.append('user/message',message('native-human'),{surfaceOp:'append'});
      release();
      await waitFor(() => nativeSink.sent.length===1,'real session delivery');
      check('real Cordis SessionStore archive leaves host prepare and append untouched', () => {
        assert.equal(live.append,append);
        assert.equal(SessionStore.prototype.prepare,prepare);
        assert.equal(nativeSink.sent[0].message_id,'native-human');
        assert.equal(native.status().capture.attached_sessions,0);
      });
    } finally {
      await native.stop();
      await nativeContext.fiber.dispose(); await nativeContext.fiber.await();
    }
  }
  console.log(JSON.stringify({passed:passed.length, network_calls:0, production_mutations:0}));
} finally {
  for (const value of instances) await value.stop();
  const owned = fs.realpathSync(root);
  if (path.dirname(owned)===tmp && path.basename(owned).startsWith('brain-archive-only-')) {
    fs.rmSync(owned,{recursive:true,force:true});
  }
}
