/** Native async Fetch-disposer regression; no service, model, or network. */
import assert from 'node:assert/strict';
import path from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {spawnSync} from 'node:child_process';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const target=path.join(root,'adapters/dsh/onboarding.mjs');
const child=String.raw`
import assert from 'node:assert/strict';
const {mountOnboarding}=await import(process.argv[1]);
const scenario=process.argv[2],removed=[],warnings=[];
let registrations=0,caught=false;
const ctx={logger:{warn(code){warnings.push(code);if(scenario==='rollback-warning-throws')throw Error('synthetic_logger_failure');}},
 get(){return{fetch:{register(){const index=registrations++;
  if(scenario.startsWith('rollback')&&index===2)throw Error('synthetic_registration_failure');
  if(scenario==='teardown'&&index===1)return()=>{removed.push(index);throw Error('synthetic_sync_cleanup_failure');};
  return async()=>{if(scenario==='delayed'&&index===5)await new Promise(resolve=>setTimeout(resolve,30));removed.push(index);if(index===0&&scenario!=='normal')throw Error('synthetic_async_cleanup_failure');};
 }}};}};
let dispose;
try{dispose=mountOnboarding(ctx,{handle(){return Response.json({ok:true});}});}
catch(error){assert.equal(error.message,'synthetic_registration_failure');caught=true;}
if(scenario.startsWith('rollback')){
 assert(caught,'original_registration_error_must_remain_synchronous');
 await new Promise(resolve=>setTimeout(resolve,40));
 assert.deepEqual(removed.sort((a,b)=>a-b),[0,1]);assert.equal(warnings.length,1);
}else{
 assert.equal(typeof dispose,'function');
 const first=dispose(),second=dispose();assert.equal(first,second,'cleanup_must_be_idempotent');
 await first;
 assert.deepEqual(removed.sort((a,b)=>a-b),[0,1,2,3,4,5]);
 assert.equal(warnings.length,scenario==='normal'?0:1);
}
console.log(JSON.stringify({ok:true,scenario,registrations,cleanup_attempts:removed.length,warnings:warnings.length,primary_registration_error_preserved:caught}));
`;
function run(file,scenario){
 const value=spawnSync(process.execPath,['--unhandled-rejections=strict','--input-type=module','-e',child,pathToFileURL(file).href,scenario],
  {encoding:'utf8',timeout:4000,windowsHide:true,maxBuffer:131072});
 const json=value.stdout?.trim().split('\n').filter(Boolean).at(-1);
 let observation;try{observation=JSON.parse(json);}catch{}
 return {exit_code:value.status,signal:value.signal,observation,
  synthetic_unhandled_cleanup_error:!!value.stderr?.includes('synthetic_async_cleanup_failure')};
}

// Optional released-source control; a default test run makes no baseline claim.
let baseline={tested:false};
if(process.env.MEMORY_BRAIN_ONBOARDING_BASELINE){
 const file=process.env.MEMORY_BRAIN_ONBOARDING_BASELINE;
 assert(path.isAbsolute(file),'baseline_must_be_an_explicit_absolute_module_path');
 const control=run(file,'rollback');
 assert.notEqual(control.exit_code,0,'baseline_source_must_reproduce_failure');
 assert(control.synthetic_unhandled_cleanup_error,'baseline_failure_must_be_cleanup_rejection_not_module_loading');
 baseline={tested:true,...control};
}
const results=[];
for(const scenario of ['rollback','rollback-warning-throws','teardown','delayed','normal']){
 results.push({scenario,...run(target,scenario)});
}
const report={schema_version:1,baseline,
 passed:results.filter(row=>row.exit_code===0&&row.observation?.ok===true).length,
 failed:results.filter(row=>row.exit_code!==0||row.observation?.ok!==true).length,
 results,network_calls:0,model_calls:0};
console.log(JSON.stringify(report));
if(report.failed)process.exitCode=1;
