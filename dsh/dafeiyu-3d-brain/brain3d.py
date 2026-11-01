"""3D 全息脑：真实皮层网格 + 光纤 + 记忆节点。

来源：一个 React/Vite 原型项目，已改写成纯浏览器 JS（无构建步骤）。
    HologramScene.js  (226 行)  皮层点云 + 光晕
    RadiantEffects.js (81 行)   光纤网络 + 节点丝线 + 雾面 + 镜面地板
    BrainScene.js     (28 行)   数据 → 节点位置的映射

改动只有三处，都是「因为环境不同」必须改的：
1. import 从 npm 包改成 ESM URL —— 由页面里的 importmap 解析，
   默认指向本目录下的 three/（不依赖 CDN，离线能跑）
2. 模型路径从 /assets/cortex/ 改成 CORTEX_BASE + /cortex/
   CORTEX_BASE 在下面定义，改一行就能换
3. 输入数据从 React 的 payload.graph 改成 setData(graph)

渲染逻辑一行没动。皮层、纤维、雾、镜面地板、光柱、星尘、脉冲全在。
"""
from __future__ import annotations

# 静态资源前缀。你的 cortex/ 和 three/ 挂在哪个路径下，就改这里。
# 默认 /panel-assets，对应 build() 生成的脚本里的 fetch 路径。
CORTEX_BASE = "/panel-assets"

SCRIPT = r"""

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { Reflector } from 'three/addons/objects/Reflector.js';
// ============================================================
// 3D 全息脑 —— 从 brain-hologram-preview 照抄
// 原文件：HologramScene.js / RadiantEffects.js / BrainScene.js
// 只改了 import 路径和模型 URL，渲染逻辑一行没动。
// ============================================================

// 注意：改成静态 import，跟原版一样。
// 原来用动态 await import()，但下面顶层就有 new THREE.Color(...)，
// 那时 THREE 还是 undefined，整个模块直接抛错、一行都不执行，
// 页面上连 console.log 都看不到（实测踩过）。
// 静态 import 由浏览器在模块求值前解析，THREE 一定就绪。
// 裸模块名靠 panel.html 里的 importmap 解析。

let ready = true;

async function loadDeps() {
  return true;
}

// ========== BrainScene.js（照抄）==========
const CATEGORIES = {
  person: { name: '人物', color: '#54e7ff' }, thing: { name: '物件', color: '#ffc66c' },
  topic: { name: '话题', color: '#bd96ff' }, place: { name: '地点', color: '#79f3c8' },
  skill: { name: '技能', color: '#f9a0df' }, memory: { name: '记忆', color: '#92caff' },
};
const KIND_NAMES = { episode: '经历', fact: '事实', preference: '偏好', relation: '关系', skill: '技能记忆', self: '自我记忆', selfview: '自我认知', review: '回顾', reflection: '反思' };
const kindColors = { skill: '#fca2ee', fact: '#ffcc74', episode: '#6be7ff', preference: '#9ef6d0', relation: '#c39eff', self: '#bc9aff', selfview: '#f3a5ef' };
function seeded(s) { let n = 2166136261; for (let i = 0; i < String(s).length; i++) { n ^= String(s).charCodeAt(i); n = Math.imul(n, 16777619); } n^=n>>>16;n=Math.imul(n,0x85ebca6b);n^=n>>>13;n=Math.imul(n,0xc2b2ae35);n^=n>>>16;return (n >>> 0) / 4294967296; }
function buildData(g) {
  const entities = (g?.nodes || []).map(n => ({ id: `e${n.id}`, sourceId: n.id, label: String(n.name || '未命名实体'), category: CATEGORIES[n.type] ? n.type : 'thing', degree: n.degree || 0, color: CATEGORIES[n.type]?.color || '#ffc66c' }));
  const memories = (g?.memories || []).map(m => ({ id: `m${m.id}`, sourceId: m.id, label: String(m.text || ''), category: 'memory', kind: m.kind, importance: m.importance ?? .4, entities: m.entities || [], color: kindColors[m.kind] || '#91d9ff' }));
  const nodes = [...entities, ...memories], ids = new Set(nodes.map(n => n.id)), seen = new Set(), edges = [];
  const edge = (a, b, kind) => { if (!ids.has(a) || !ids.has(b) || a === b) return; const key = [a, b].sort().join(':'); if (seen.has(key)) return; seen.add(key); edges.push({ source: a, target: b, kind, phase: seeded(key) }); };
  (g?.edges || []).forEach(e => edge(`e${e.src ?? e.src_id}`, `e${e.dst ?? e.dst_id}`, e.kind || 'relation'));
  memories.forEach(m => m.entities.forEach(e => edge(`e${e}`, m.id, 'memory')));
  return { nodes, edges };
}
function placeOnCortex(nodes, surfaces) {
  if (!surfaces?.length) return [];
  return nodes.map(n => {
    const surface = surfaces[Math.floor(seeded(`side:${n.id}`) * surfaces.length)];
    const count = surface.positions.length / 3, index = Math.floor(seeded(`vertex:${n.id}`) * count), at = index * 3;
    const nx = surface.normals[at], ny = surface.normals[at+1], nz = surface.normals[at+2];
    return { ...n, x: surface.positions[at] + nx * .025, y: surface.positions[at+1] + ny * .025, z: surface.positions[at+2] + nz * .025, nx, ny, nz, side: surface.side };
  });
}

// ========== RadiantEffects.js（照抄）==========
const fiberVertex = `
attribute float aAlong;attribute float aSeed;
varying float vAlong;varying float vSeed;varying float vDepth;
void main(){vec4 mv=modelViewMatrix*vec4(position,1.);vAlong=aAlong;vSeed=aSeed;vDepth=-mv.z;gl_Position=projectionMatrix*mv;}`;
const fiberFragment = `
uniform float uTime;uniform float uPower;uniform vec3 uColor;
varying float vAlong;varying float vSeed;varying float vDepth;
void main(){float wave=fract(vAlong*.65-uTime*(.08+vSeed*.045)+vSeed);
float pulse=pow(max(0.,1.-abs(wave-.5)*8.),6.);
float tip=pow(max(0.,sin(vAlong*3.14159)),.35);
float depth=clamp(1.40-vDepth*.065,.40,1.);
vec3 color=vSeed>.96?vec3(.015,.74,1.3):uColor;
float opacity=(.018+pulse*.19)*depth*(.35+tip*.65)*uPower;
gl_FragColor=vec4(color*(1.1+pulse*1.2),opacity);}`;

function createFiberNetwork(buffer, color) {
  const count=new DataView(buffer).getUint32(0,true);
  const g=new THREE.BufferGeometry();
  g.setAttribute('position',new THREE.BufferAttribute(new Float32Array(buffer,4,count*3),3));
  g.setAttribute('aAlong',new THREE.BufferAttribute(new Float32Array(buffer,4+count*12,count),1));
  g.setAttribute('aSeed',new THREE.BufferAttribute(new Float32Array(buffer,4+count*16,count),1));
  const m=new THREE.ShaderMaterial({uniforms:{uTime:{value:0},uPower:{value:1},uColor:{value:color.clone().multiplyScalar(1.7)}},vertexShader:fiberVertex,fragmentShader:fiberFragment,transparent:true,depthWrite:false,depthTest:false,blending:THREE.AdditiveBlending});
  const object=new THREE.LineSegments(g,m);object.renderOrder=2;return object;
}

function createNodeFilaments(nodes,surfaces,colors) {
  const group=new THREE.Group();
  for(let sideIndex=0;sideIndex<surfaces.length;sideIndex++){
    const surface=surfaces[sideIndex],p=surface.positions,normals=surface.normals,count=p.length/3;
    const hubs=nodes.filter(n=>n.side===surface.side&&(seeded(n.id)>.74||(n.degree||0)>10||(n.importance||0)>.85)).slice(0,48);
    const positions=[],along=[],seeds=[];
    hubs.forEach(hub=>{
      const start=new THREE.Vector3(hub.x,hub.y,hub.z),normal=new THREE.Vector3(hub.nx,hub.ny,hub.nz);
      for(let branch=0;branch<24;branch++){
        let index=0,end,distance=0;
        for(let attempt=0;attempt<100;attempt++){
          index=Math.floor(seeded(`${hub.id}:${branch}:${attempt}`)*count)*3;
          end=new THREE.Vector3(p[index],p[index+1],p[index+2]);distance=start.distanceTo(end);
          if(distance>.16&&distance<.95)break;
        }
        if(distance<.12||distance>1.25)continue;
        const endNormal=new THREE.Vector3(normals[index],normals[index+1],normals[index+2]);end.addScaledVector(endNormal,.025);
        const delta=end.clone().sub(start),a=start.clone().addScaledVector(delta,.27).addScaledVector(normal,.12+distance*.17);
        const b=end.clone().addScaledVector(delta,-.27).addScaledVector(endNormal,.10+distance*.10);
        const curve=new THREE.CubicBezierCurve3(start,a,b,end),seed=seeded(`cluster:${hub.id}:${branch}`);
        for(let k=0;k<30;k++)for(const t of [k/30,(k+1)/30]){const q=curve.getPoint(t);positions.push(q.x,q.y,q.z);along.push(t);seeds.push(seed);}
      }
    });
    const g=new THREE.BufferGeometry();g.setAttribute('position',new THREE.Float32BufferAttribute(positions,3));g.setAttribute('aAlong',new THREE.Float32BufferAttribute(along,1));g.setAttribute('aSeed',new THREE.Float32BufferAttribute(seeds,1));
    const m=new THREE.ShaderMaterial({uniforms:{uTime:{value:0},uPower:{value:1},uColor:{value:colors[sideIndex].clone().multiplyScalar(1.7)}},vertexShader:fiberVertex,fragmentShader:fiberFragment,transparent:true,depthWrite:false,depthTest:false,blending:THREE.AdditiveBlending});
    const object=new THREE.LineSegments(g,m);object.renderOrder=2;group.add(object);
  }return group;
}

const hazeVertex=`varying vec2 vUv;void main(){vUv=uv;gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.);}`;
const hazeFragment=`varying vec2 vUv;uniform vec3 uColor;uniform float uOpacity;uniform float uTime;
void main(){vec2 p=(vUv-.5)*2.;float radius=length(p);float field=exp(-dot(p,p)*3.8)*smoothstep(1.,.30,radius);
float breath=.88+.12*sin(uTime*.55);gl_FragColor=vec4(uColor,field*uOpacity*breath);}`;
function createHaze() {
  const group=new THREE.Group();
  for(const [x,y,z,size,color,opacity] of [[-.75,.10,-.30,4.0,'#ff9c36',.034],[.75,.10,-.30,4.1,'#9e38ff',.044],[0,-.35,.2,3.6,'#0099ff',.032],[0,-2.14,0,3.2,'#00ccff',.055]]){
    const m=new THREE.ShaderMaterial({uniforms:{uColor:{value:new THREE.Color(color)},uOpacity:{value:opacity},uTime:{value:0}},vertexShader:hazeVertex,fragmentShader:hazeFragment,transparent:true,depthWrite:false,depthTest:false,blending:THREE.AdditiveBlending});
    const plane=new THREE.Mesh(new THREE.PlaneGeometry(size,size),m);plane.position.set(x,y,z);plane.renderOrder=1;
    plane.onBeforeRender=(_renderer,_scene,camera)=>plane.quaternion.copy(camera.quaternion);
    group.add(plane);
  }return group;
}

function createMirrorFloor() {
  const base=Reflector.ReflectorShader;
  const shader={...base,uniforms:{...base.uniforms},
    vertexShader:base.vertexShader.replace('varying vec4 vUv;','varying vec4 vUv; varying vec2 vFloor;').replace('vUv = textureMatrix','vFloor = position.xy; vUv = textureMatrix'),
    fragmentShader:base.fragmentShader.replace('varying vec4 vUv;','varying vec4 vUv; varying vec2 vFloor;').replace('gl_FragColor = vec4( blendOverlay( base.rgb, color ), 1.0 );','float edge = 1.0-smoothstep(3.0,5.5,length(vFloor)); gl_FragColor = vec4(base.rgb * color * edge * .37,1.0);')};
  const floor=new Reflector(new THREE.CircleGeometry(5.5,96),{textureWidth:768,textureHeight:512,multisample:0,color:'#b1c9e6',clipBias:.004,shader});
  floor.rotation.x=-Math.PI/2;floor.position.y=-2.68;floor.material.toneMapped=false;
  return floor;
}

// ========== HologramScene.js（照抄）==========
const gold = new THREE.Color('#ffb43c'), violet = new THREE.Color('#d53bff'), cyan = new THREE.Color('#00dfff');
const pointVertex = `
attribute float aSize; attribute float aAlpha; attribute vec3 aColor;
varying vec3 vColor; varying float vAlpha; uniform float uDpr;
void main(){vec4 mv=modelViewMatrix*vec4(position,1.);gl_Position=projectionMatrix*mv;
gl_PointSize=clamp(aSize*uDpr*9./max(1.,-mv.z),1.,50.);vColor=aColor;vAlpha=aAlpha;}`;
const pointFragment = `
varying vec3 vColor; varying float vAlpha;
void main(){float d=length(gl_PointCoord-.5)*2.;if(d>1.||vAlpha<.005)discard;
float a=exp(-d*d*6.)*vAlpha;vec3 col=mix(vColor,vec3(2.),pow(max(0.,1.-d*3.),2.)*.10);
gl_FragColor=vec4(col,a);}`;
function pointsMaterial(dpr, depthTest = true) {
  return new THREE.ShaderMaterial({ uniforms: { uDpr: { value: dpr } }, vertexShader: pointVertex, fragmentShader: pointFragment,
    transparent: true, depthWrite: false, depthTest, blending: THREE.AdditiveBlending });
}
function pointsGeometry(positions, colors, sizes, alphas) {
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
  g.setAttribute('aColor', new THREE.Float32BufferAttribute(colors, 3));
  g.setAttribute('aSize', new THREE.Float32BufferAttribute(sizes, 1));
  g.setAttribute('aAlpha', new THREE.Float32BufferAttribute(alphas, 1));
  return g;
}
const auraVertex=`varying vec3 vNormal;varying vec3 vView;
void main(){vec4 mv=modelViewMatrix*vec4(position+normal*.012,1.);vView=-mv.xyz;vNormal=normalize(normalMatrix*normal);gl_Position=projectionMatrix*mv;}`;
const auraFragment=`varying vec3 vNormal;varying vec3 vView;uniform vec3 uColor;
void main(){float rim=pow(1.-abs(dot(normalize(vNormal),normalize(vView))),2.8);
gl_FragColor=vec4(mix(uColor,vec3(.08,.7,1.),.32)*(.5+rim*.9),rim*.035);}`;

class HologramScene {
  constructor(canvas, getOptions, onSelect, onStatus) {
    this.canvas = canvas; this.getOptions = getOptions; this.onSelect = onSelect; this.onStatus = onStatus;
    this.dead = false; this.time = 0; this.nodes = []; this.edges = []; this.hover = null; this.selected = null; this.curves = []; this.surface = []; this.fiberMaterials = [];
    this.abort = new AbortController(); this.width = 0; this.height = 0; this.lastMode = '';
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: false, alpha: false, powerPreference: 'high-performance' });
    this.dpr = Math.min(devicePixelRatio || 1, 1.5); this.renderer.setPixelRatio(this.dpr);
    this.renderer.setClearColor('#020810'); this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping; this.renderer.toneMappingExposure = 1.15;
    this.scene = new THREE.Scene(); this.scene.background = new THREE.Color('#010810');
    this.camera = new THREE.PerspectiveCamera(35, 1, .1, 70);
    this.controls = new OrbitControls(this.camera, canvas); this.controls.enableDamping = true; this.controls.dampingFactor = .09;
    this.controls.enablePan = false; this.controls.rotateSpeed = .6; this.controls.zoomSpeed = .75;
    this.controls.minDistance = 5.4; this.controls.maxDistance = 19;
    this.controls.minPolarAngle = .12; this.controls.maxPolarAngle = Math.PI - .12;
    this.composer = new EffectComposer(this.renderer); this.composer.addPass(new RenderPass(this.scene, this.camera));
    this.bloom = new UnrealBloomPass(new THREE.Vector2(1280, 720), .72, .48, .50); this.composer.addPass(this.bloom); this.composer.addPass(new OutputPass());
    this.brain = new THREE.Group(); this.brain.position.y = .08; this.brain.scale.set(1.15,.94,1.0); this.scene.add(this.brain);
    this.live = new THREE.Group(); this.brain.add(this.live);
    this.stage = new THREE.Group(); this.scene.add(this.stage); this.rings = [];
    this.makeStage();
    this.haze=createHaze();this.scene.add(this.haze);
    this.mirror=createMirrorFloor();this.stage.add(this.mirror);
    this.resize = () => {
      const r = canvas.getBoundingClientRect(); if (!r.width || !r.height) return;
      const wasPortrait = this.width <= 760 || this.width < this.height * .8, oldHeight = this.height;
      this.width = r.width; this.height = r.height; this.camera.aspect = r.width / r.height; this.camera.updateProjectionMatrix();
      this.renderer.setSize(r.width, r.height, false); this.composer.setSize(r.width, r.height);
      const compact = r.width <= 760 || r.width < r.height * .8;
      if (!this.started || wasPortrait !== compact || (compact && Math.abs(oldHeight-r.height)>12)) this.reset(); this.started = true;
    };
    this.observer = new ResizeObserver(this.resize); this.observer.observe(canvas); this.resize();
    this.pointerDown = e => { this.dragStart = { x: e.clientX, y: e.clientY }; this.dragging = true; };
    this.pointerUp = e => { if (this.dragStart && Math.hypot(e.clientX-this.dragStart.x,e.clientY-this.dragStart.y)<5) {
      const n = this.pick(e); if (n) { this.select(n.id); this.onSelect(n); }
    } this.dragStart = null; this.dragging = false; };
    this.pointerMove = e => {
      if (this.dragging) return;
      this.hover = this.pick(e); this.canvas.style.cursor = this.hover ? 'pointer' : 'grab';
      const tip = document.getElementById('node-tooltip');
      if (tip) { tip.textContent = this.hover ? `${CATEGORIES[this.hover.category].name} · ${this.hover.label.slice(0,72)}` : '';
        tip.style.display = this.hover ? 'block' : 'none'; tip.style.left = `${Math.max(8,Math.min(e.clientX+14,innerWidth-300))}px`; tip.style.top = `${Math.max(8,e.clientY-36)}px`; }
    };
    this.pointerLeave = () => { this.hover = null; const tip = document.getElementById('node-tooltip'); if (tip) tip.style.display='none'; };
    canvas.addEventListener('pointerdown',this.pointerDown); canvas.addEventListener('pointerup',this.pointerUp);
    canvas.addEventListener('pointercancel',this.pointerUp); canvas.addEventListener('pointermove',this.pointerMove); canvas.addEventListener('pointerleave',this.pointerLeave);
    this.contextLost = e => { e.preventDefault(); onStatus?.({state:'error',message:'三维画面连接中断，请刷新页面。'}); };
    canvas.addEventListener('webglcontextlost',this.contextLost);
    this.loadCortex().catch(e => { if (e.name !== 'AbortError' && !this.dead) onStatus?.({state:'error',message:'大脑模型加载失败，请刷新页面。'}); });
    this.tick = now => { if (this.dead) return; this.raf=requestAnimationFrame(this.tick);
      if (document.visibilityState !== 'visible' || now-(this.lastFrame||0)<33) return;
      const dt=Math.min(.06,(now-(this.lastFrame||now))/1000);this.lastFrame=now;
      const o=this.getOptions(); if(o.mode==='reference'){this.canvas.style.visibility='hidden';return;}
      this.canvas.style.visibility='visible';if(o.running)this.time+=dt;
      if(this.lastMode!==o.mode){this.lastMode=o.mode;this.reset();}
      this.controls.update(dt);this.animate(o);this.composer.render(dt);
    }; this.raf=requestAnimationFrame(this.tick);
  }
  reset() { const compact = this.width <= 760 || this.width < this.height * .8; this.camera.position.set(0,compact?2.6:2.0,compact?17.0:10.0); this.controls.target.set(0,compact?.28+Math.max(0,760-this.height)*.0055:0,0); this.controls.update(); this.brain.rotation.set(0,0,0); }
  async loadCortex() {
    this.onStatus?.({state:'loading',message:'正在构建立体脑沟…'});
    const buffers=await Promise.all(['lh','rh'].map(async side=>{
      const url=`__CORTEX_BASE__/cortex/${side}.bin`;
      const r=await fetch(url,{signal:this.abort.signal});
      if(!r.ok)throw new Error(`皮层数据加载失败：${url} 返回 ${r.status}。检查静态资源路由有没有配好。`);
      return r.arrayBuffer();
    })); if(this.dead)return;
    buffers.forEach((buffer,side)=>{
      const header=new DataView(buffer),count=header.getUint32(0,true),triangles=header.getUint32(4,true);
      const positions=new Float32Array(buffer,8,count*3),indices=new Uint32Array(buffer,8+count*12,triangles*3);
      const geometry=new THREE.BufferGeometry();geometry.setAttribute('position',new THREE.BufferAttribute(positions,3));geometry.setIndex(new THREE.BufferAttribute(indices,1));geometry.computeVertexNormals();
      const normals=geometry.attributes.normal.array;
      this.surface.push({positions,normals,side:side===0?-1:1});
      const depthMesh=new THREE.Mesh(geometry,new THREE.MeshBasicMaterial({colorWrite:false,depthWrite:true,polygonOffset:true,polygonOffsetFactor:1,polygonOffsetUnits:1}));
      depthMesh.renderOrder=-2;this.brain.add(depthMesh);
      this.brain.add(new THREE.Mesh(geometry,new THREE.ShaderMaterial({vertexShader:auraVertex,fragmentShader:auraFragment,uniforms:{uColor:{value:(side===0?gold:violet).clone()}},transparent:true,depthWrite:false,blending:THREE.AdditiveBlending})));
      const pp=[],cc=[],ss=[],aa=[];
      for(let i=0;i<count;i+=3){const j=i*3;pp.push(positions[j]+normals[j]*.008,positions[j+1]+normals[j+1]*.008,positions[j+2]+normals[j+2]*.008);
        const r=seeded(`${side}:${i}`),color=(r>.91?cyan:side===0?gold:violet).clone().multiplyScalar(r>.99?2.4:1.15);
        cc.push(color.r,color.g,color.b);ss.push(.65+r*1.6);aa.push(.24+r*.36);
      }
      const cloud=new THREE.Points(pointsGeometry(pp,cc,ss,aa),pointsMaterial(this.dpr));this.brain.add(cloud);
    });
    const fibers=await Promise.all(['lh','rh'].map(async side=>{
      const url=`__CORTEX_BASE__/cortex/${side}.hologram.bin?v=radiant-2`;
      const r=await fetch(url,{signal:this.abort.signal,cache:'no-store'});
      if(!r.ok)throw new Error(`光纤数据加载失败：${url} 返回 ${r.status}。`);
      return r.arrayBuffer();
    }));
    if(this.dead)return;
    fibers.forEach((buffer,i)=>{const object=createFiberNetwork(buffer,i===0?gold:violet);this.brain.add(object);this.fiberMaterials.push(object.material);});
    if(this.pendingData)this.setData(this.pendingData);
    this.onStatus?.({state:'ready',message:'3D 全息已就绪'});
  }
  makeStage() {
    const lineMat=(color,opacity=.5)=>new THREE.LineBasicMaterial({color:new THREE.Color(color).multiplyScalar(1.8),transparent:true,opacity,depthWrite:false,blending:THREE.AdditiveBlending});
    const ring=(radius,y,color,opacity=.5,tilt=0)=>{
      if(y<-2)radius*=radius<=2.4?.72:.88;
      const p=[];for(let i=0;i<=256;i++){const a=i/256*Math.PI*2;p.push(Math.cos(a)*radius,y+Math.sin(a)*radius*Math.sin(tilt),Math.sin(a)*radius*Math.cos(tilt));}
      const g=new THREE.BufferGeometry();g.setAttribute('position',new THREE.Float32BufferAttribute(p,3));const l=new THREE.Line(g,lineMat(color,opacity));this.stage.add(l);return l;
    };
    for(const radius of [1.0,1.16,1.65,1.74,2.13,2.28,2.68,3.1,3.55]) ring(radius,-2.42,cyan,radius<2.3?.62:.25);
    ring(1.87,2.35,cyan,.7,.18);ring(2.15,2.35,cyan,.35,.18);ring(1.55,2.27,gold,.45,.18);
    ring(2.55,-.05,cyan,.24);ring(2.85,-.03,gold,.25);ring(2.65,0,cyan,.12,.85);ring(2.65,0,cyan,.12,-.85);
    const base=new THREE.Mesh(new THREE.CylinderGeometry(2.3*.72,2.38*.72,.12,128),new THREE.MeshBasicMaterial({color:'#061e32'}));base.position.y=-2.52;this.stage.add(base);
    for(const [radius,y]of [[2.3,-2.45],[2.38,-2.58],[.58,-2.38]]) {
      const t=new THREE.Mesh(new THREE.TorusGeometry(radius*.72,.009,6,192),new THREE.MeshBasicMaterial({color:new THREE.Color('#13baff').multiplyScalar(1.8)}));t.rotation.x=Math.PI/2;t.position.y=y;this.stage.add(t);
    }
    for(let layer=0;layer<2;layer++){
      const group=new THREE.Group(),y=layer===0?-2.4:2.35,r=layer===0?2.05*.72:1.85;
      group.position.y=y;if(layer===1)group.rotation.x=-.18;
      for(let k=0;k<70;k++) {const a=k/70*Math.PI*2,arc=new THREE.Mesh(new THREE.TorusGeometry(r,.012,5,6,.028),new THREE.MeshBasicMaterial({color:(k%11===0?gold:cyan).clone().multiplyScalar(2.1),transparent:true,opacity:k%3===0?.8:.27}));arc.rotation.x=Math.PI/2;arc.rotation.z=a;group.add(arc);}
      this.stage.add(group);this.rings.push(group);
    }
    const beam=new THREE.Mesh(new THREE.CylinderGeometry(.014,.014,5.3,12),new THREE.MeshBasicMaterial({color:new THREE.Color('#46dfff').multiplyScalar(1.5),transparent:true,opacity:.85,depthWrite:false,blending:THREE.AdditiveBlending}));beam.position.y=.15;this.stage.add(beam);
    const envelope=new THREE.Mesh(new THREE.CylinderGeometry(.09,.15,5.3,20,1,true),new THREE.MeshBasicMaterial({color:'#0fbfff',transparent:true,opacity:.025,depthWrite:false,blending:THREE.AdditiveBlending,side:THREE.DoubleSide}));envelope.position.y=.15;this.stage.add(envelope);
    const stars=[],colors=[],sizes=[],alphas=[];
    for(let i=0;i<650;i++){stars.push((seeded(`sx${i}`)-.5)*22,(seeded(`sy${i}`)-.5)*16,-4-seeded(`sz${i}`)*12);colors.push(.12,.45,.7);sizes.push(.8+seeded(`ss${i}`)*1.4);alphas.push(.16+seeded(`sa${i}`)*.4);}
    this.stage.add(new THREE.Points(pointsGeometry(stars,colors,sizes,alphas),pointsMaterial(this.dpr,false)));
    const pos=[],columnColors=[];
    for(let i=0;i<34;i++){const a=i/34*Math.PI*2,r=2.6+seeded(`r${i}`)*.8,x=Math.cos(a)*r,z=Math.sin(a)*r,y=-2.42+seeded(`start${i}`)*.9,end=-.8+seeded(`h${i}`)*3.8;pos.push(x,y,z,x,end,z);const c=i%9===0?gold:cyan;columnColors.push(c.r,c.g,c.b,c.r,c.g,c.b);}
    const columns=new THREE.BufferGeometry();columns.setAttribute('position',new THREE.Float32BufferAttribute(pos,3));columns.setAttribute('color',new THREE.Float32BufferAttribute(columnColors,3));this.stage.add(new THREE.LineSegments(columns,new THREE.LineBasicMaterial({vertexColors:true,transparent:true,opacity:.07,depthWrite:false,blending:THREE.AdditiveBlending})));
  }
  setData(data) {
    this.pendingData=data;if(!this.surface.length)return;
    const signature=JSON.stringify(data);
    if(signature===this.signature)return;this.signature=signature;
    this.clearGroup(this.live);this.nodes=placeOnCortex(data.nodes,this.surface);this.edges=data.edges;
    this.byId=new Map(this.nodes.map(n=>[n.id,n]));
    if(this.selected&&!this.byId.has(this.selected))this.selected=null;
    const clusters=createNodeFilaments(this.nodes,this.surface,[gold,violet]);this.live.add(clusters);this.clusterMaterials=clusters.children.map(c=>c.material);
    const positions=[],colors=[],sizes=[],alphas=[];
    for(const n of this.nodes){positions.push(n.x,n.y,n.z);const col=n.side<0?gold:violet;colors.push(col.r*1.5,col.g*1.5,col.b*1.5);sizes.push(7+Math.min(5,Math.sqrt(n.degree||1)));alphas.push(.9);}
    this.nodeGeometry=pointsGeometry(positions,colors,sizes,alphas);this.nodePoints=new THREE.Points(this.nodeGeometry,pointsMaterial(this.dpr));this.nodePoints.renderOrder=5;this.live.add(this.nodePoints);
    this.curves=this.edges.map(e=>{const a=this.byId.get(e.source),b=this.byId.get(e.target);if(!a||!b)return null;
      const av=new THREE.Vector3(a.x,a.y,a.z),bv=new THREE.Vector3(b.x,b.y,b.z),mid=av.clone().add(bv).multiplyScalar(.5);
      const n=new THREE.Vector3(a.nx+b.nx,a.ny+b.ny,a.nz+b.nz).normalize();mid.addScaledVector(n,.3+av.distanceTo(bv)*.16);
      return {edge:e,curve:new THREE.QuadraticBezierCurve3(av,mid,bv),phase:seeded(e.source+e.target)};
    }).filter(Boolean);
    this.edgeGeometry=new THREE.BufferGeometry();this.edgePositions=new Float32Array(this.curves.length*18*2*3);this.edgeColors=new Float32Array(this.edgePositions.length);
    this.edgeGeometry.setAttribute('position',new THREE.BufferAttribute(this.edgePositions,3));this.edgeGeometry.setAttribute('color',new THREE.BufferAttribute(this.edgeColors,3));
    this.edgeLines=new THREE.LineSegments(this.edgeGeometry,new THREE.LineBasicMaterial({vertexColors:true,transparent:true,opacity:.14,depthWrite:false,depthTest:false,blending:THREE.AdditiveBlending}));this.live.add(this.edgeLines);
    const p=[],c=[],s=[],a=[];for(let i=0;i<72;i++){p.push(0,0,0);c.push(.3,1,1.5);s.push(4);a.push(.7);}
    this.pulseGeometry=pointsGeometry(p,c,s,a);this.live.add(new THREE.Points(this.pulseGeometry,pointsMaterial(this.dpr)));
    this.highlight=new THREE.Points(pointsGeometry([0,0,0],[.2,1,1.6],[29],[0]),pointsMaterial(this.dpr,false));this.live.add(this.highlight);
    this.filterKey='';
  }
  select(id) {this.selected=id;this.filterKey='';}
  rotate() {this.camera.position.applyAxisAngle(new THREE.Vector3(0,1,0),Math.PI/5);this.controls.update();}
  pick(event) {
    if(!this.nodes.length||this.getOptions().mode==='reference')return null;
    const box=this.canvas.getBoundingClientRect(),x=event.clientX-box.left,y=event.clientY-box.top,opts=this.getOptions();
    this.brain.updateMatrixWorld();let best=null,dist=155;const view=this.camera.position.clone().normalize();
    for(const n of this.nodes){if(!opts.active.includes(n.category))continue;
      const normal=new THREE.Vector3(n.nx,n.ny,n.nz).transformDirection(this.brain.matrixWorld);if(normal.dot(view)<-.25)continue;
      const p=new THREE.Vector3(n.x,n.y,n.z).applyMatrix4(this.brain.matrixWorld).project(this.camera);
      const d=((p.x+1)*this.width/2-x)**2+((1-p.y)*this.height/2-y)**2;
      if(d<dist){best=n;dist=d;}
    }return best;
  }
  updateFilters(o) {
    if(!this.nodeGeometry)return;
    const focus=this.hover?.id||this.selected,query=o.query.trim().toLowerCase(),key=[focus,query,o.active.join(','),o.density,o.glow].join('|');
    if(key===this.filterKey)return;this.filterKey=key;
    const neighbors=new Set();if(focus)this.edges.forEach(e=>{if(e.source===focus)neighbors.add(e.target);if(e.target===focus)neighbors.add(e.source);});
    const alpha=this.nodeGeometry.attributes.aAlpha.array,color=this.nodeGeometry.attributes.aColor.array,size=this.nodeGeometry.attributes.aSize.array;
    this.nodes.forEach((n,i)=>{const active=o.active.includes(n.category),match=!query||n.label.toLowerCase().includes(query),near=!focus||n.id===focus||neighbors.has(n.id),base=n.side<0?gold:violet;
      const major=seeded(n.id)>.74||(n.degree||0)>10||(n.importance||0)>.85;alpha[i]=!active?0:!match||!near?.05:(major?1:.40);const co=seeded('tint:'+n.id)>.90?cyan:base;color[i*3]=co.r*1.8;color[i*3+1]=co.g*1.8;color[i*3+2]=co.b*1.8;size[i]=n.id===focus?34:(major?27:4.0)*(o.glow/130+.5);
    });for(const name of ['aAlpha','aColor','aSize'])this.nodeGeometry.attributes[name].needsUpdate=true;
    this.edgeLines.material.opacity=focus?.48:.055;
    let at=0;this.visibleCurves=[];
    for(const item of this.curves){const {edge,phase,curve}=item,a=this.byId.get(edge.source),b=this.byId.get(edge.target),near=focus&&(edge.source===focus||edge.target===focus);
      const visible=o.active.includes(a.category)&&o.active.includes(b.category)&&(near||phase<o.density/100)&&(!query||a.label.toLowerCase().includes(query)||b.label.toLowerCase().includes(query));
      if(!visible)continue;this.visibleCurves.push(item);
      const col=near?new THREE.Color('#a9eaff').multiplyScalar(1.8):a.side===b.side?(a.side<0?gold:violet):cyan;
      for(let k=0;k<18;k++){for(const u of [k/18,(k+1)/18]){const p=curve.getPoint(u);this.edgePositions[at]=p.x;this.edgeColors[at++]=col.r;this.edgePositions[at]=p.y;this.edgeColors[at++]=col.g;this.edgePositions[at]=p.z;this.edgeColors[at++]=col.b;}}
    }
    this.edgeGeometry.attributes.position.needsUpdate=true;this.edgeGeometry.attributes.color.needsUpdate=true;this.edgeGeometry.setDrawRange(0,at/3);this.edgeGeometry.computeBoundingSphere();
    this.highlight.geometry.attributes.aAlpha.array[0]=focus?1:0;this.highlight.geometry.attributes.aAlpha.needsUpdate=true;if(focus){const n=this.byId.get(focus);if(n)this.highlight.position.set(n.x,n.y,n.z);}
  }
  animate(o) {
    this.stage.visible=o.art;this.bloom.strength=.35+o.glow*.006;this.brain.rotation.y=Math.sin(this.time*.13)*.12;this.rings.forEach((r,i)=>{r.rotation.y=this.time*(i===0?.04:-.055);});
    [...this.fiberMaterials,...(this.clusterMaterials||[])].forEach(m=>{m.uniforms.uTime.value=this.time;m.uniforms.uPower.value=.60+o.glow*.008;});
    this.haze.children.forEach(obj=>{obj.material.uniforms.uTime.value=this.time;});
    this.updateFilters(o);
    if(this.pulseGeometry){const p=this.pulseGeometry.attributes.position.array,alpha=this.pulseGeometry.attributes.aAlpha.array,curves=this.visibleCurves||[];
      for(let i=0;i<72;i++){const curve=curves[(i*7)%Math.max(1,curves.length)];if(!curve){alpha[i]=0;continue;}const q=curve.curve.getPoint((this.time*(.08+(i%5)*.01)+i*.271)%1);p[i*3]=q.x;p[i*3+1]=q.y;p[i*3+2]=q.z;alpha[i]=.7;}
      this.pulseGeometry.attributes.position.needsUpdate=true;this.pulseGeometry.attributes.aAlpha.needsUpdate=true;this.pulseGeometry.computeBoundingSphere();
    }
  }
  clearGroup(group) {const geometries=new Set(),materials=new Set();for(const child of [...group.children]){child.traverse(o=>{if(o.geometry)geometries.add(o.geometry);if(Array.isArray(o.material))o.material.forEach(m=>materials.add(m));else if(o.material)materials.add(o.material);});group.remove(child);}geometries.forEach(g=>g.dispose());materials.forEach(m=>m.dispose());}
  dispose() {this.dead=true;this.abort.abort();cancelAnimationFrame(this.raf);this.observer.disconnect();this.controls.dispose();if(this.mirror){this.mirror.parent?.remove(this.mirror);this.mirror.geometry.dispose();this.mirror.dispose();}this.clearGroup(this.scene);this.composer.dispose();this.bloom.dispose();this.renderer.dispose();
    for(const [name,fn]of [['pointerdown',this.pointerDown],['pointerup',this.pointerUp],['pointercancel',this.pointerUp],['pointermove',this.pointerMove],['pointerleave',this.pointerLeave],['webglcontextlost',this.contextLost]])this.canvas.removeEventListener(name,fn);
  }
}

// ========== 挂到 window，给 panel.html 的 2D 逻辑用 ==========
window.CortexBrain3D = {
  scene: null,
  options: { mode: 'hologram', running: true, query: '', active: Object.keys(CATEGORIES), density: 32, glow: 60, art: true },
  async mount(canvasId) {
    if (!await loadDeps()) return false;
    const canvas = document.getElementById(canvasId || 'cortex-canvas');
    if (!canvas) return false;
    this.canvas = canvas;
    try {
      this.scene = new HologramScene(canvas,
        () => this.options,
        (n) => { if (window.__onSelect) window.__onSelect(n); },
        (s) => {
          console.log('3D', s.message || s.state);
          const el = document.getElementById('model-loading');
          if (el && s.message) { el.textContent = s.message; el.style.display = 'block'; }
          if (s.state === 'ready' && el) el.style.display = 'none';
        });
    } catch (e) {
      console.error('3D 创建失败', e);
      const el = document.getElementById('model-loading');
      if (el) { el.textContent = '三维场景创建失败：' + (e.message || e); el.style.color = '#ffb0b0'; }
      return false;
    }
    // 场景建好之后，把之前缓存的数据补上
    if (this.pendingGraph) {
      try { this.scene.setData(buildData(this.pendingGraph)); } catch (e) { console.warn(e); }
    }
    return true;
  },
  setData(graph) {
    // 注意：3D 是异步挂载的，UI 的 load() 可能比它先跑。
    // 原来这里直接 return，数据就丢了——脑出来了但没有记忆点和线。
    // 现在先缓存，等场景就绪再补上。
    this.pendingGraph = graph;
    if (!this.scene) return;
    try { this.scene.setData(buildData(graph)); } catch (e) { console.warn('3D setData 失败', e); }
  },
  setOptions(patch) { Object.assign(this.options, patch); },
  rotate() { this.scene?.rotate(); },
  reset() { this.scene?.reset(); },
  dispose() { this.scene?.dispose(); this.scene = null; },
};

// 自动挂载：panel.html 里 canvas 的 id 是 cortex-canvas
// 注意：每一步都往页面上写状态，出错能看见是卡在哪一步。
// 之前只 console.warn，用户在页面上什么也看不到，只能看到「正在构建立体脑沟…」。
function _status(msg, isError) {
  const el = document.getElementById('model-loading');
  if (!el) return;
  el.textContent = msg;
  el.style.display = 'block';
  if (isError) { el.style.color = '#ffb0b0'; el.style.borderColor = '#7a3030'; }
}
(async () => {
  try {
    _status('正在加载三维引擎…');
    const deps = await loadDeps();
    if (!deps) { _status('三维引擎加载失败：three.js 下载不了（检查网络或 CDN）', true); return; }
    _status('正在创建渲染器…');
    const canvas = document.getElementById('cortex-canvas');
    if (!canvas) { _status('找不到画布元素', true); return; }
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) {
      _status('画布尺寸为 0，等待布局…');
      await new Promise(r => setTimeout(r, 300));
    }
    // WebGL 检测：显卡不支持时给一句人话，别只报「创建失败」
    try {
      const probe = document.createElement('canvas');
      const gl = probe.getContext('webgl2') || probe.getContext('webgl');
      if (!gl) {
        _status('这个浏览器/显卡不支持 WebGL，3D 跑不起来。'
              + '试试 Chrome 或 Edge，或者检查显卡驱动。', true);
        return;
      }
    } catch (e) {
      _status('WebGL 检测失败：' + (e && e.message ? e.message : e), true);
      return;
    }

    const ok = await window.CortexBrain3D.mount('cortex-canvas');
    if (!ok) { _status('三维场景创建失败（可能是 WebGL 不可用）', true); return; }
    _status('正在加载皮层模型（约 10MB）…');
  } catch (e) {
    console.error(e);
    _status('出错：' + (e && e.message ? e.message : e), true);
  }
})();
"""


def build(base: str | None = None) -> str:
    """返回要注入页面的模块代码。

    base: 静态资源前缀。默认用模块级的 CORTEX_BASE。
          你的 cortex/ 和 three/ 挂在别的路径下时传进来：
              build("/static/brain")
    """
    b = str(base if base is not None else CORTEX_BASE).rstrip("/")
    return SCRIPT.replace("__CORTEX_BASE__", b)


if __name__ == "__main__":
    print(len(build()), "字符")
