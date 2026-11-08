/** Build the existing cortical scene offline; never call a model or publish. */
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const modules=process.env.MEMORY_BRAIN_NODE_MODULES || path.join(root,'frontend','node_modules');
const require=createRequire(path.join(modules,'_memory_brain_build.cjs'));
const {build}=require('esbuild');
await build({entryPoints:[path.join(root,'frontend/brain/HologramScene.js')],
  outfile:path.join(root,'xiaojiu_brain/static/brain-renderer.js'),bundle:true,
  format:'esm',target:'es2020',minify:true,legalComments:'eof',nodePaths:[modules]});
console.log('Memory Brain renderer built with local dependencies.');
