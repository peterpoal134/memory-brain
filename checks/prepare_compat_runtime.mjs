/** Prepare a disposable official DSH runtime for the compatibility checks.
 * No installed DSH, user configuration, credentials or session files are read.
 * Run: node checks/prepare_compat_runtime.mjs --out /tmp/memory-brain-dsh-rc3
 * Requires Node >=22, npm, and network access to the official npm registry.
 */
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {spawnSync} from 'node:child_process';

const DSH_VERSION='0.2.2-rc.3';
const LOOP_SHA256='f09f60814d4e96a880f41b9fc3832f33c56a28f954f48810d8439ce83ad6ffa8';
const dshPackages=['dsh-agent-loop','dsh-llm','dsh-agent','dsh-session',
  'dsh-session-projection','dsh-system-prompt','dsh-tool-executor-native',
  'dsh-client-connection','dsh-tools','dsh-brand','dsh-util-values','dsh-scope',
  'dsh-session-persistence','dsh-util-crypto','dsh-timeout','dsh-typert-protocol',
  'dsh-error','dsh-credentials','dsh-user-approval'];
const dependencies=Object.fromEntries(dshPackages.map(name=>['@deepseek-ai/'+name,DSH_VERSION]));
Object.assign(dependencies,{'@deepseek-ai/cordis':'4.1.0','@deepseek-ai/schemastery':'3.19.0',
  '@deepseek-ai/cosmokit':'1.9.0','@standard-schema/spec':'1.1.0',zod:'4.6.6'});

function findNpmCli(){
  const candidates=[process.env.npm_execpath,
    path.join(path.dirname(process.execPath),'node_modules/npm/bin/npm-cli.js'),
    path.resolve(path.dirname(process.execPath),'../lib/node_modules/npm/bin/npm-cli.js')];
  for(const directory of (process.env.PATH||'').split(path.delimiter)){
    candidates.push(path.join(directory,'node_modules/npm/bin/npm-cli.js'));
    try{candidates.push(fs.realpathSync(path.join(directory,'npm')));}catch{}
  }
  return candidates.find(file=>typeof file==='string'&&file.endsWith('npm-cli.js')&&fs.existsSync(file));
}
const args=process.argv.slice(2);
if(args.length===1&&args[0]==='--help'){
  console.log('Usage: node checks/prepare_compat_runtime.mjs --out <new-or-empty-directory>');
  process.exit(0);
}
if(args.length!==2||args[0]!=='--out'||!args[1])throw new Error('Expected --out <new-or-empty-directory>.');
if(Number(process.versions.node.split('.')[0])<22)throw new Error('Node 22 or later is required.');
const output=path.resolve(args[1]);
if(fs.existsSync(output)&&fs.readdirSync(output).length)throw new Error('Output directory must be empty; existing installations are never overwritten.');
const npm=findNpmCli();
if(!npm)throw new Error('npm-cli.js was not found. Install a standard Node.js + npm distribution.');
fs.mkdirSync(output,{recursive:true});
fs.writeFileSync(path.join(output,'package.json'),JSON.stringify({name:'memory-brain-compat-fixture',
  version:'0.0.0',private:true,type:'module',dependencies},null,2)+'\n');
const cache=path.join(output,'.npm-cache');
const install=spawnSync(process.execPath,[npm,'install','--prefix',output,'--cache',cache,
  '--registry=https://registry.npmjs.org','--ignore-scripts','--no-audit','--no-fund','--omit=dev'],
  {stdio:'inherit',windowsHide:true,timeout:300000});
if(install.error)throw install.error;
if(install.status!==0)throw new Error('Official runtime dependency installation failed (exit '+install.status+').');
const lock=JSON.parse(fs.readFileSync(path.join(output,'package-lock.json'),'utf8'));
const records=[];
for(const [entry,value] of Object.entries(lock.packages||{})){
  if(!entry)continue;
  if(!entry.startsWith('node_modules/')||!value.resolved?.startsWith('https://registry.npmjs.org/'))
    throw new Error('Unexpected dependency location: '+entry);
  const integrity=value.integrity;
  if(typeof integrity!=='string'||!integrity.startsWith('sha512-'))throw new Error('Missing sha512 integrity: '+entry);
  const digest=Buffer.from(integrity.slice(7),'base64').toString('hex');
  const archive=path.join(cache,'_cacache/content-v2/sha512',digest.slice(0,2),digest.slice(2,4),digest.slice(4));
  const actual='sha512-'+crypto.createHash('sha512').update(fs.readFileSync(archive)).digest('base64');
  if(actual!==integrity)throw new Error('Downloaded archive integrity mismatch: '+entry);
  const installed=JSON.parse(fs.readFileSync(path.join(output,entry,'package.json'),'utf8'));
  if(installed.version!==value.version||dependencies[installed.name]!==installed.version)
    throw new Error('Unpinned or unexpected installed package: '+entry);
  records.push({name:installed.name,version:installed.version,integrity,archive_verified:true});
}
const loop=path.join(output,'node_modules/@deepseek-ai/dsh-agent-loop/lib/index.js');
const loopSha256=crypto.createHash('sha256').update(fs.readFileSync(loop)).digest('hex');
if(loopSha256!==LOOP_SHA256)throw new Error('Official loop source fingerprint differs from the audited fixture.');
fs.writeFileSync(path.join(output,'COMPAT-RUNTIME-MANIFEST.json'),JSON.stringify({schema_version:1,
  dsh_version:DSH_VERSION,loop_sha256:loopSha256,package_count:records.length,records},null,2)+'\n');
console.log(JSON.stringify({ready:true,dsh_version:DSH_VERSION,packages:records.length,loop_sha256:loopSha256}));
