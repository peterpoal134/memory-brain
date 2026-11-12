/** Offline unit-contract regression: byte diagnostics are never token authority. */
import assert from 'node:assert/strict';
import {createRequestBudget,ContextBudgetError,CONTEXT_BUDGET_ERROR_CODE} from '../adapters/dsh/request-budget.mjs';

const checks=[];
const test=(name,fn)=>{fn();checks.push({name,passed:true});};
const request=(text='synthetic text',maxTokens=1024)=>({provider:'synthetic',model:'synthetic-model',
 messages:[{role:'user',content:[{type:'text',text}]}],tools:[],toolHistory:[],
 ...(maxTokens===null?{}:{maxTokens})});
const context=capacity=>({preparedCall:{context:{contextWindow:capacity}}});
function freeze(value){if(value&&typeof value==='object'){for(const child of Object.values(value))freeze(child);Object.freeze(value);}return value;}
function jsonBytes(value){return Buffer.byteLength(JSON.stringify({messages:value.messages,tools:value.tools,toolHistory:value.toolHistory}),'utf8');}
const valid=context(65536);

test('Chinese and English use actual distinct UTF8-byte diagnostics',()=>{
 const english=freeze(request('A'.repeat(1000))),chinese=freeze(request('汉'.repeat(1000)));
 const a=createRequestBudget(),b=createRequestBudget();
 assert.equal(a.check(english,valid),english);assert.equal(b.check(chinese,valid),chinese);
 const first=a.status(),second=b.status();
 assert.equal(first.estimated_input_bytes,jsonBytes(english));assert.equal(second.estimated_input_bytes,jsonBytes(chinese));
 assert.equal(second.estimated_input_bytes-first.estimated_input_bytes,2000);
 assert.equal(first.estimated_input_tokens,Math.ceil(first.estimated_input_bytes/3)+528);
 assert.equal(second.estimated_input_tokens,Math.ceil(second.estimated_input_bytes/3)+528);
 assert.equal(second.estimation_basis,'utf8_json_bytes_div3_heuristic');
 assert(second.input_estimate_advisory);assert.equal(second.active,false);
});

test('Large Chinese input exceeding approximate window stays advisory',()=>{
 const input=freeze(request('只是合成中文'.repeat(4000)));
 const budget=createRequestBudget({safetyTokens:2048});
 assert.equal(budget.check(input,context(8192)),input);
 const state=budget.status();assert(state.estimated_input_tokens>state.input_limit_tokens);
 assert.equal(state.estimated_over_limit,true);assert.equal(state.blocked,0);assert.equal(state.passed,1);
 assert.equal(state.scope,'advisory_text_json');assert.equal(state.active,false);
 assert.equal(state.last_reason,'input_estimate_over_reference');
});

test('Large English JSON and tool schemas are advisory without mutation',()=>{
 const input=freeze({...request('quoted " JSON \\ syntax '.repeat(1800)),
  tools:[{name:'synthetic_tool',description:'A'.repeat(10000),parameters:{type:'object',properties:{x:{type:'string'}}}}]});
 const before=JSON.stringify(input),budget=createRequestBudget({safetyTokens:512});
 assert.equal(budget.check(input,context(8192)),input);assert.equal(JSON.stringify(input),before);
 assert.equal(budget.status().estimated_over_limit,true);assert.equal(budget.status().blocked,0);
});

test('Configured maxInputTokens is a reference quota rather than byte rejection',()=>{
 const input=freeze(request('中文English'.repeat(100))),budget=createRequestBudget({maxInputTokens:1,safetyTokens:0});
 assert.equal(budget.check(input,valid),input);assert.equal(budget.status().input_limit_tokens,1);
 assert.equal(budget.status().estimated_over_limit,true);assert.equal(budget.status().blocked,0);
});

test('Declared output reservation equal to known window is rejected',()=>{
 const budget=createRequestBudget({safetyTokens:0});
 assert.throws(()=>budget.check(request('x',4096),context(4096)),error=>error instanceof ContextBudgetError&&error.code===CONTEXT_BUDGET_ERROR_CODE);
 assert.equal(budget.status().scope,'invalid_output_reserve');assert.equal(budget.status().blocked,1);
 assert.equal(budget.status().reserve_check_applied,true);
});

test('Known output plus safety equal to window leaves no input space',()=>{
 const budget=createRequestBudget({safetyTokens:1024});
 assert.throws(()=>budget.check(request('x',3072),context(4096)),ContextBudgetError);
 assert.equal(budget.status().input_limit_tokens,0);assert.equal(budget.status().active,false);
});

test('Safety alone exceeding window is a known configuration error',()=>{
 const budget=createRequestBudget({safetyTokens:4097});
 assert.throws(()=>budget.check(request('x',0),context(4096)),ContextBudgetError);
 assert.equal(budget.status().reserved_output_tokens,0);assert.equal(budget.status().reserve_check_applied,true);
});

test('Safety alone can exhaust a window even with unknown output reserve',()=>{
 const budget=createRequestBudget({safetyTokens:4096});
 assert.throws(()=>budget.check(request('x',null),context(4096)),ContextBudgetError);
 assert.equal(budget.status().reserved_output_tokens,null);assert.equal(budget.status().reserve_check_applied,true);
});

test('One remaining token does not authorize heuristic rejection',()=>{
 const input=freeze(request('synthetic input longer than a token',3071)),budget=createRequestBudget({safetyTokens:1024});
 assert.equal(budget.check(input,context(4096)),input);assert.equal(budget.status().input_limit_tokens,1);
 assert.equal(budget.status().estimated_over_limit,true);assert.equal(budget.status().blocked,0);
});

test('Unknown capacity remains partial with a configured advisory quota',()=>{
 const input=freeze(request('synthetic'.repeat(1000))),budget=createRequestBudget({maxInputTokens:100});
 assert.equal(budget.check(input),input);const state=budget.status();
 assert.equal(state.scope,'needs_capacity');assert.equal(state.context_window_tokens,null);
 assert.equal(state.estimated_over_limit,true);assert.equal(state.reserve_check_applied,false);
 assert.equal(state.partial,1);assert.equal(state.active,false);
});

test('Unknown output reservation stays partial and is not invented',()=>{
 const input=freeze(request('synthetic'.repeat(1000),null)),budget=createRequestBudget({safetyTokens:128});
 assert.equal(budget.check(input,context(8192)),input);const state=budget.status();
 assert.equal(state.scope,'needs_output_reserve');assert.equal(state.reserved_output_tokens,null);
 assert.equal(state.reserve_check_applied,false);assert.equal(state.partial,1);assert.equal(state.active,false);
});

test('Images remain explicitly unpriced and never become exact text coverage',()=>{
 const input=freeze({...request('synthetic'),messages:[{role:'user',content:[{type:'image',data:'A'.repeat(6000),mimeType:'image/png'}]}]});
 const budget=createRequestBudget({maxInputTokens:1});
 assert.equal(budget.check(input,valid),input);const state=budget.status();
 assert.equal(state.scope,'partial_non_text');assert.equal(state.partial,1);assert.equal(state.active,false);
 assert(state.estimated_input_bytes<6000);assert.equal(state.blocked,0);
});

test('Capacity fallback only accepts matching actual provider and model',()=>{
 const input=freeze(request('synthetic')),budget=createRequestBudget({safetyTokens:0});
 const agent={session:{requestContext:()=>({provider:'different',model:'synthetic-model',contextWindow:500})}};
 assert.equal(budget.check(input,{agent}),input);assert.equal(budget.status().context_window_tokens,null);
 const matching={session:{requestContext:()=>({provider:input.provider,model:input.model,contextWindow:65536})}};
 assert.equal(budget.check(input,{agent:matching}),input);assert.equal(budget.status().context_window_tokens,65536);
});

test('Unsupported or unserializable shape remains unchanged and unguarded',()=>{
 const budget=createRequestBudget();const unknown={messages:'unsupported'};
 assert.equal(budget.check(unknown,valid),unknown);assert.equal(budget.status().scope,'unavailable');
 assert.equal(budget.status().estimated_input_bytes,null);assert.equal(budget.status().estimation_basis,null);
 const cyclic=request();cyclic.messages[0].content[0].cycle=cyclic;
 assert.equal(budget.check(cyclic,valid),cyclic);assert.equal(budget.status().blocked,0);
});

test('Aborted request signal is untouched and no new abort or retry is introduced',()=>{
 const aborted=new AbortController();aborted.abort();const input={...request('汉'.repeat(9000)),signal:aborted.signal};
 const budget=createRequestBudget({maxInputTokens:1,safetyTokens:0});
 assert.equal(budget.check(input,valid),input);assert.equal(input.signal,aborted.signal);
 assert.equal(input.signal.aborted,true);assert.equal(budget.status().blocked,0);
});

test('Stop and disabled gates keep returning the same request without checks',()=>{
 const input=request('synthetic',65536),disabled=createRequestBudget({enabled:false});
 assert.equal(disabled.check(input,context(65536)),input);assert.equal(disabled.status().checks,0);
 const stopped=createRequestBudget();stopped.stop();const before=stopped.status();
 assert.equal(stopped.check(input,context(65536)),input);assert.deepEqual(stopped.status(),before);
 assert.equal(stopped.status().active,false);assert.equal(stopped.status().scope,'stopped');
});

test('No request body or provider usage is retained or rewritten',()=>{
 const marker='SYNTHETIC_PRIVATE_MARKER_NOT_FOR_STATUS';
 const usage={inputTokens:2000,cacheReadTokens:800,outputTokens:500,totalTokens:3300};
 const input=freeze({...request(marker),usage});const before=JSON.stringify(input),warnings=[];
 const budget=createRequestBudget({maxInputTokens:1,onWarning:(code,state)=>warnings.push({code,state})});
 assert.equal(budget.check(input,valid),input);assert.equal(JSON.stringify(input),before);assert.equal(input.usage,usage);
 const diagnostic=JSON.stringify({status:budget.status(),warnings});
 assert(!diagnostic.includes(marker));assert(!diagnostic.includes('inputTokens'));assert(!diagnostic.includes('synthetic-model'));
 assert(!diagnostic.includes('messages'));assert.equal(budget.status().blocked,0);
});

console.log(JSON.stringify({passed:checks.length,failed:0,checks,model_calls:0,network_calls:0,production_mutations:0}));
