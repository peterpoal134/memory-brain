export const CATEGORIES = {
 raw:{name:'原话证据',color:'#66e8ff'},facts:{name:'派生事实索引',color:'#ffcf83'},
 summaries:{name:'摘要索引',color:'#c09bff'},semantic:{name:'语义索引',color:'#8dc7ff'},
 self:{name:'表达档案',color:'#f0b3ff'},emotion:{name:'情绪信号',color:'#ffb0c9'},
 life:{name:'生活状态',color:'#8be0c2'},queue:{name:'后台队列',color:'#f4d28b'},
 imports:{name:'导入笔记',color:'#8fb6d4'},
 memory_fact:{name:'事实记忆',color:'#80f2e6'},memory_summary:{name:'摘要记忆',color:'#dab5ff'},
 memory_raw:{name:'原话记忆',color:'#ffc981'}
};
export function seeded(s) { let n = 2166136261; for (let i = 0; i < String(s).length; i++) { n ^= String(s).charCodeAt(i); n = Math.imul(n, 16777619); } n^=n>>>16;n=Math.imul(n,0x85ebca6b);n^=n>>>13;n=Math.imul(n,0xc2b2ae35);n^=n>>>16;return (n >>> 0) / 4294967296; }
// These are navigation targets, not anatomical assignments or individual memories.
// Each target is snapped to an outward-facing vertex of the licensed MRI surface.
// Six face the initial camera; three are revealed when the brain is turned.
export const FUNCTIONAL_ANCHOR_TARGETS = Object.freeze({
  raw: [-.58, 1.02, 1.05], facts: [.61, 1.02, 1.05],
  summaries: [-.98, .05, 1.08], semantic: [.98, .05, 1.08],
  life: [-.57, -.94, .88], self: [.57, -.94, .88],
  emotion: [-.82, .78, -.96], queue: [.91, .05, -.98],
  imports: [-.53, -.96, -.86]
});
export function placeOnCortex(nodes, surfaces) {
  if (!surfaces?.length) return [];
  const occupied = [];
  return nodes.map(n => {
    const target = FUNCTIONAL_ANCHOR_TARGETS[n.id] || [
      (seeded(`side:${n.id}`) > .5 ? 1 : -1) * (.55 + seeded(n.id) * .4),
      (seeded(`height:${n.id}`) - .5) * 2, .95
    ];
    const side = target[0] < 0 ? -1 : 1;
    const candidates = surfaces.filter(s => s.side === side);
    const surface = candidates[0] || surfaces[0], p = surface.positions, normals = surface.normals;
    // Prefer the outward front/back skin over a similarly located sulcus wall.
    const dz = target[2] < 0 ? -1 : 1, dy = .18;
    let index = 0, best = Infinity;
    for (let pass = 0; pass < 2 && !Number.isFinite(best); pass++) {
      for (let i = 0; i < p.length / 3; i++) {
        const at = i * 3, facing = normals[at + 2] * dz + normals[at + 1] * dy;
        if (!pass && (facing < .55 || p[at + 2] * dz < .25)) continue;
        if (occupied.some(v => (p[at]-v[0])**2+(p[at+1]-v[1])**2+(p[at+2]-v[2])**2 < .25)) continue;
        const distance = (p[at]-target[0])**2+(p[at+1]-target[1])**2+(p[at+2]-target[2])**2;
        const score = distance + Math.max(0, 1 - facing) * .12;
        if (score < best) { best = score; index = i; }
      }
    }
    const at = index * 3;
    occupied.push([p[at], p[at+1], p[at+2]]);
    const nx = surface.normals[at], ny = surface.normals[at+1], nz = surface.normals[at+2];
    return { ...n, x: p[at] + nx * .035, y: p[at+1] + ny * .035, z: p[at+2] + nz * .035,
      nx, ny, nz, side: surface.side, vertexIndex: index, functionalAnchor: true };
  });
}

const memoryVertexPools = new WeakMap();
function outwardVertexPools(surface) {
  if(memoryVertexPools.has(surface))return memoryVertexPools.get(surface);
  const front=[],back=[],all=[],p=surface.positions,n=surface.normals;
  for(let index=0;index<p.length/3;index++){
    const at=index*3,r=Math.hypot(p[at],p[at+1],p[at+2]);all.push(index);
    // Avoid the medial wall and deep inward-facing sulci, using the real vertex normal.
    if(!r||(p[at]*n[at]+p[at+1]*n[at+1]+p[at+2]*n[at+2])/r<.42)continue;
    if(p[at+2]>.08)front.push(index);else if(p[at+2]<-.08)back.push(index);
  }
  const pools={front:front.length?front:all,back:back.length?back:all};memoryVertexPools.set(surface,pools);return pools;
}
// One supplied document maps to one actual cortical vertex. Hashes only determine
// an illustrative, repeatable layout; they encode no semantic or anatomical relation.
export function placeMemoriesOnCortex(nodes, surfaces) {
  if(!surfaces?.length)return [];
  const placed=new Map(),occupied=new Set();
  for(const node of [...nodes].sort((a,b)=>String(a.id).localeCompare(String(b.id)))){
    if(placed.has(node.id))continue;
    const side=seeded(`memory-side:${node.id}`)<.5?-1:1;
    const surface=surfaces.find(s=>s.side===side)||surfaces[0],pool=outwardVertexPools(surface)[seeded(`memory-face:${node.id}`)<.5?'front':'back'];
    let offset=Math.floor(seeded(`memory-vertex:${node.id}`)*pool.length),index=pool[offset];
    for(let retry=0;occupied.has(`${surface.side}:${index}`)&&retry<pool.length;retry++){offset=(offset+1)%pool.length;index=pool[offset];}
    occupied.add(`${surface.side}:${index}`);const at=index*3,p=surface.positions,n=surface.normals;
    placed.set(node.id,{...node,x:p[at]+n[at]*.045,y:p[at+1]+n[at+1]*.045,z:p[at+2]+n[at+2]*.045,
      nx:n[at],ny:n[at+1],nz:n[at+2],side:surface.side,vertexIndex:index,functionalAnchor:false});
  }
  return [...new Set(nodes.map(n=>n.id))].map(id=>placed.get(id));
}
