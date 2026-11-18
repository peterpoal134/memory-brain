/** Run the real integration from a disposable, prepared host lookup scope.
 * No installed application is modified; the owned launcher is removed on exit.
 */
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {fileURLToPath,pathToFileURL} from 'node:url';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const runtime=process.env.DSH_RUNTIME;
if(!runtime||!path.isAbsolute(runtime))throw new Error('DSH_RUNTIME must be a disposable prepared compatibility runtime.');
const manifest=JSON.parse(fs.readFileSync(path.join(runtime,'COMPAT-RUNTIME-MANIFEST.json'),'utf8'));
if(manifest.schema_version!==1||manifest.dsh_version!=='0.2.2-rc.3'||
   manifest.loop_sha256!=='f09f60814d4e96a880f41b9fc3832f33c56a28f954f48810d8439ce83ad6ffa8')
  throw new Error('Only the disposable runtime created by prepare_compat_runtime.mjs is accepted.');
const launcher=path.join(runtime,'memory-brain-integration-'+crypto.randomUUID()+'.mjs');
const entry=pathToFileURL(path.join(root,'checks/integration/run.mjs')).href;
let owned=false;
try{
  fs.writeFileSync(launcher,'import '+JSON.stringify(entry)+';\n',{flag:'wx'});owned=true;
  const result=spawnSync(process.execPath,['--unhandled-rejections=strict',launcher],{cwd:root,stdio:'inherit',windowsHide:true,timeout:60000,
    env:{...process.env,MEMORY_BRAIN_AUTO_DISCOVERY:'1',MEMORY_BRAIN_ENTRY:process.env.MEMORY_BRAIN_ENTRY||path.join(root,'adapters/dsh/memory-brain.mjs')}});
  if(result.error)throw result.error;
  if(result.status!==0)throw new Error('Automatic discovery integration failed (exit '+result.status+').');
}finally{if(owned)fs.unlinkSync(launcher);}
