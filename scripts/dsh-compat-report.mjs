#!/usr/bin/env node
/** Print only the public, read-only compatibility projection. No disk scan. */
import {probeRuntime} from '../adapters/dsh/host-compat.mjs';

const usage='node scripts/dsh-compat-report.mjs --runtime <absolute-runtime-directory>';
const args=process.argv.slice(2);
if(!args.length||args.length===1&&['--help','-h'].includes(args[0])){
  console.log(JSON.stringify({usage,read_only:true},null,2));
}else if(args.length!==2||args[0]!=='--runtime'){
  console.log(JSON.stringify({error:'invalid_arguments',usage},null,2));process.exitCode=2;
}else{
  try{console.log(JSON.stringify(probeRuntime(args[1]).compatibility,null,2));}
  catch{console.log(JSON.stringify({error:'dsh_runtime_absolute_path_required',usage},null,2));process.exitCode=2;}
}
