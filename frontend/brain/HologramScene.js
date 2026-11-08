import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { CATEGORIES, placeOnCortex, placeMemoriesOnCortex, seeded } from './BrainScene.js';
import { createFiberNetwork, createNodeFilaments, createHaze, createMirrorFloor } from './RadiantEffects.js';

const gold = new THREE.Color('#ffb43c'), violet = new THREE.Color('#d53bff'), cyan = new THREE.Color('#00dfff');
const pointVertex = `
attribute float aSize; attribute float aAlpha; attribute vec3 aColor;
varying vec3 vColor; varying float vAlpha; uniform float uDpr; uniform float uScreenSize;
void main(){vec4 mv=modelViewMatrix*vec4(position,1.);gl_Position=projectionMatrix*mv;
gl_PointSize=clamp(aSize*uDpr*(uScreenSize>.5?1.:9./max(1.,-mv.z)),1.,50.);vColor=aColor;vAlpha=aAlpha;}`;
const pointFragment = `
varying vec3 vColor; varying float vAlpha;
void main(){float d=length(gl_PointCoord-.5)*2.;if(d>1.||vAlpha<.005)discard;
float a=exp(-d*d*6.)*vAlpha;vec3 col=mix(vColor,vec3(2.),pow(max(0.,1.-d*3.),2.)*.10);
gl_FragColor=vec4(col,a);}`;
function pointsMaterial(dpr, depthTest = true, screenSize = false) {
  return new THREE.ShaderMaterial({ uniforms: { uDpr: { value: dpr },uScreenSize:{value:screenSize?1:0} }, vertexShader: pointVertex, fragmentShader: pointFragment,
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

// Visibility-only proxy, clustered from the actual cortex. It is never rendered.
// This avoids ray-testing all 579k original triangles for nine UI markers per frame.
function occlusionGeometry(positions, indices) {
  const cells=new Map(),sums=[],counts=[],mapping=new Uint32Array(positions.length/3),step=.10;
  for(let i=0;i<mapping.length;i++) {
    const at=i*3,key=`${Math.round(positions[at]/step)},${Math.round(positions[at+1]/step)},${Math.round(positions[at+2]/step)}`;
    let id=cells.get(key);if(id===undefined){id=counts.length;cells.set(key,id);counts.push(0);sums.push(0,0,0);}
    mapping[i]=id;counts[id]++;for(let k=0;k<3;k++)sums[id*3+k]+=positions[at+k];
  }
  for(let i=0;i<counts.length;i++)for(let k=0;k<3;k++)sums[i*3+k]/=counts[i];
  const faces=[],seen=new Set();
  for(let i=0;i<indices.length;i+=3){const a=mapping[indices[i]],b=mapping[indices[i+1]],c=mapping[indices[i+2]];
    if(a===b||b===c||a===c)continue;const key=[a,b,c].sort((x,y)=>x-y).join(',');if(seen.has(key))continue;seen.add(key);faces.push(a,b,c);}
  const geometry=new THREE.BufferGeometry();geometry.setAttribute('position',new THREE.Float32BufferAttribute(sums,3));geometry.setIndex(faces);geometry.computeBoundingSphere();return geometry;
}
const finiteClamp=(value,fallback,min,max)=>typeof value==='number'&&Number.isFinite(value)?Math.max(min,Math.min(max,value)):fallback;

export class HologramScene {
  constructor(canvas, getOptions, onSelect, onStatus, onHover = null) {
    this.canvas = canvas; this.getOptions = getOptions; this.onSelect = onSelect; this.onStatus = onStatus;this.onHover=onHover;
    this.dead = false; this.time = 0; this.nodes = []; this.edges = []; this.hover = null; this.selected = null; this.curves = []; this.surface = []; this.fiberMaterials = [];
    this.occluders=[];this.raycaster=new THREE.Raycaster();this.projected=new Map();this.markerEntries=new Map();this.dirty=true;
    this.abort = new AbortController(); this.width = 0; this.height = 0; this.lastMode = '';
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: false, alpha: false, powerPreference: 'high-performance' });
    this.dpr = Math.min(devicePixelRatio || 1, innerWidth < 760 ? 1 : 1.4); this.renderer.setPixelRatio(this.dpr);
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
    this.markerLayer=document.createElement('div');this.markerLayer.className='brain-node-layer';this.markerLayer.setAttribute('aria-label','大脑聚合功能区导航');
    Object.assign(this.markerLayer.style,{position:'absolute',inset:'0',pointerEvents:'none',zIndex:'3',overflow:'hidden'});canvas.parentElement?.appendChild(this.markerLayer);
    this.resize = () => {
      const r = canvas.getBoundingClientRect(); if (!r.width || !r.height) return;
      const wasPortrait = this.width <= 760 || this.width < this.height * .8, oldHeight = this.height;
      this.width = r.width; this.height = r.height; this.camera.aspect = r.width / r.height; this.camera.updateProjectionMatrix();
      this.renderer.setSize(r.width, r.height, false); this.composer.setSize(r.width, r.height);
      const compact = r.width <= 760 || r.width < r.height * .8;
      if (!this.started || wasPortrait !== compact || (compact && Math.abs(oldHeight-r.height)>12)) this.reset(); this.started = true;this.dirty=true;
    };
    this.observer = new ResizeObserver(this.resize); this.observer.observe(canvas); this.resize();
    this.pointerDown = e => { if(e.button!==0||e.isPrimary===false)return;this.dragStart = { x:e.clientX,y:e.clientY,id:e.pointerId };this.dragging=true;this.setHover(null);this.dirty=true; };
    this.pointerUp = e => { if(this.dragStart?.id!==e.pointerId)return;const start=this.dragStart;this.dragStart=null;this.dragging=false;
      this.projectMarkers();if(Math.hypot(e.clientX-start.x,e.clientY-start.y)<5){const n=this.pick(e);if(n){this.select(n.id);this.setHover(n,e.pointerType==='touch'?'touch':'canvas');if(e.pointerType==='touch')this.hoverExpiresAt=performance.now()+2200;this.onSelect?.(n);}}
      if(e.pointerType!=='touch')this.pointerMove(e);this.dirty=true; };
    this.pointerCancel = e => {if(this.dragStart?.id===e.pointerId){this.dragStart=null;this.dragging=false;this.setHover(null);this.dirty=true;}};
    this.pointerMove = e => {
      if (this.dragging) return;
      this.projectMarkers();this.setHover(this.pick(e),'canvas');
    };
    this.pointerLeave = e => {if(this.markerLayer.contains(e.relatedTarget))return;if(!this.focusedMarker)this.setHover(null);};
    this.controlsStart=()=>{this.controlInteracting=true;this.setHover(null);this.dirty=true;};
    this.controlsEnd=()=>{this.controlInteracting=false;this.dirty=true;};
    this.controlsChange=()=>{this.dirty=true;};
    this.controls.addEventListener('start',this.controlsStart);this.controls.addEventListener('end',this.controlsEnd);this.controls.addEventListener('change',this.controlsChange);
    this.windowBlur=()=>{this.dragStart=null;this.dragging=false;this.controlInteracting=false;this.focusedMarker=null;this.setHover(null);this.dirty=true;};window.addEventListener('blur',this.windowBlur);
    this.screenChange=()=>{this.dirty=true;};window.addEventListener('scroll',this.screenChange,true);window.addEventListener('resize',this.screenChange);
    canvas.addEventListener('pointerdown',this.pointerDown); canvas.addEventListener('pointerup',this.pointerUp);
    canvas.addEventListener('pointercancel',this.pointerCancel); canvas.addEventListener('pointermove',this.pointerMove); canvas.addEventListener('pointerleave',this.pointerLeave);
    this.contextLost = e => { e.preventDefault(); onStatus?.({state:'error',message:'三维画面连接中断，请刷新测试页。'}); };
    canvas.addEventListener('webglcontextlost',this.contextLost);
    this.loadCortex().catch(e => { if (e.name !== 'AbortError' && !this.dead) onStatus?.({state:'error',message:'大脑模型加载失败，请刷新测试页。'}); });
    this.tick = now => { if (this.dead) return; this.raf=requestAnimationFrame(this.tick);
      if (document.visibilityState !== 'visible' || now-(this.lastFrame||0)<33) return;
      const dt=Math.min(.06,(now-(this.lastFrame||now))/1000);this.lastFrame=now;
      const o=this.options(); if(o.mode==='reference'){this.canvas.style.visibility='hidden';this.markerLayer.hidden=true;this.setHover(null);return;}
      this.canvas.style.visibility='visible';if(o.running)this.time+=dt;
      if(this.hoverExpiresAt&&now>=this.hoverExpiresAt){this.hoverExpiresAt=0;this.setHover(null);}
      if(this.lastMode!==o.mode){this.lastMode=o.mode;if(this.pendingData)this.setData(this.pendingData);this.reset();}
      const moved=this.controls.update(dt);
      const turning=o.autoRotate&&o.rotationSpeed>0&&!this.hover&&!this.focusedMarker&&!this.dragging&&!this.controlInteracting;
      // Negative Y is clockwise when viewed from above (+Y), independent of light animation.
      if(turning){this.brain.rotation.y+=(o.rotationDirection==='ccw'?1:-1)*o.rotationSpeed*dt;this.brain.rotation.y%=Math.PI*2;}
      const visualKey=JSON.stringify([o.running,o.autoRotate,o.rotationDirection,o.rotationSpeed,o.workIntensity,o.query,o.active,o.density,o.glow,o.art,this.selected,this.hover?.id,this.dataRevision]);
      if(!o.running&&!turning&&!moved&&!this.dirty&&this.lastVisualKey===visualKey)return;
      this.lastVisualKey=visualKey;this.dirty=false;this.animate(o);this.projectMarkers(o);this.composer.render(dt);
    }; this.raf=requestAnimationFrame(this.tick);
  }
  options(){const o=this.getOptions()||{};return {...o,active:Array.isArray(o.active)?o.active:Object.keys(CATEGORIES),query:String(o.query||''),glow:finiteClamp(o.glow,55,0,100),density:finiteClamp(o.density,32,0,100),autoRotate:o.autoRotate!==false,rotationDirection:o.rotationDirection==='ccw'?'ccw':'cw',rotationSpeed:finiteClamp(o.rotationSpeed,.07,0,.3),workIntensity:finiteClamp(o.workIntensity,0,0,1)};}
  reset() { const compact = this.width <= 760 || this.width < this.height * .8; this.camera.position.set(0,compact?2.6:2.0,compact?17.0:10.0); this.controls.target.set(0,compact?.05:0,0); this.controls.update(); this.brain.rotation.set(0,0,0);this.dirty=true; }
  async loadCortex() {
    this.onStatus?.({state:'loading',message:'正在构建立体脑沟…'});
    const buffers=await Promise.all(['lh','rh'].map(async side=>{
      const r=await fetch(`/static/cortex/${side}.bin`,{signal:this.abort.signal});if(!r.ok)throw new Error('mesh');return r.arrayBuffer();
    })); if(this.dead)return;
    buffers.forEach((buffer,side)=>{
      const header=new DataView(buffer),count=header.getUint32(0,true),triangles=header.getUint32(4,true);
      const positions=new Float32Array(buffer,8,count*3),indices=new Uint32Array(buffer,8+count*12,triangles*3);
      const geometry=new THREE.BufferGeometry();geometry.setAttribute('position',new THREE.BufferAttribute(positions,3));geometry.setIndex(new THREE.BufferAttribute(indices,1));geometry.computeVertexNormals();
      const normals=geometry.attributes.normal.array;
      this.surface.push({positions,normals,side:side===0?-1:1});
      // The MRI mesh is now an invisible depth scaffold, never a solid skin.
      const depthMesh=new THREE.Mesh(geometry,new THREE.MeshBasicMaterial({colorWrite:false,depthWrite:true,polygonOffset:true,polygonOffsetFactor:1,polygonOffsetUnits:1}));
      depthMesh.renderOrder=-2;this.brain.add(depthMesh);
      const proxy=new THREE.Mesh(occlusionGeometry(positions,indices),new THREE.MeshBasicMaterial({side:THREE.DoubleSide}));this.occluders.push(proxy);
      this.brain.add(new THREE.Mesh(geometry,new THREE.ShaderMaterial({vertexShader:auraVertex,fragmentShader:auraFragment,uniforms:{uColor:{value:(side===0?gold:violet).clone()}},transparent:true,depthWrite:false,blending:THREE.AdditiveBlending})));
      const pp=[],cc=[],ss=[],aa=[];
      for(let i=0;i<count;i+=3){const j=i*3;pp.push(positions[j]+normals[j]*.008,positions[j+1]+normals[j+1]*.008,positions[j+2]+normals[j+2]*.008);
        const r=seeded(`${side}:${i}`),color=(r>.91?cyan:side===0?gold:violet).clone().multiplyScalar(r>.99?2.4:1.15);
        cc.push(color.r,color.g,color.b);ss.push(.65+r*1.6);aa.push(.24+r*.36);
      }
      const cloud=new THREE.Points(pointsGeometry(pp,cc,ss,aa),pointsMaterial(this.dpr));this.brain.add(cloud);
    });
    const fibers=await Promise.all(['lh','rh'].map(async side=>{const r=await fetch(`/static/cortex/${side}.hologram.bin?v=radiant-2`,{signal:this.abort.signal,cache:'no-store'});if(!r.ok)throw new Error('fibers');return r.arrayBuffer();}));
    if(this.dead)return;
    fibers.forEach((buffer,i)=>{const object=createFiberNetwork(buffer,i===0?gold:violet);this.brain.add(object);this.fiberMaterials.push(object.material);});
    if(this.pendingData)this.setData(this.pendingData);
    this.lastVisualKey=null;this.onStatus?.({state:'ready',message:'3D 全息已就绪'});
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
    const memoryMode=this.options().mode==='memories',signature=JSON.stringify([memoryMode,data]);
    if(signature===this.signature)return;this.signature=signature;this.dataRevision=(this.dataRevision||0)+1;this.projected.clear();
    this.memoryMode=memoryMode;this.clearGroup(this.live);this.clusterMaterials=[];this.pulseGeometry=null;this.edgeLines=null;this.edgeGeometry=null;this.curves=[];this.visibleCurves=[];
    const supplied=Array.isArray(data.nodes)?data.nodes:[];
    const memoryNodes=supplied.filter(n=>String(n.id).startsWith('memory:')&&n.documentId!=null).map(n=>n.restricted?{...n,label:'受限记忆',preview:''}:n);
    this.nodes=memoryMode?placeMemoriesOnCortex(memoryNodes,this.surface):placeOnCortex(supplied,this.surface);this.edges=memoryMode?[]:(data.edges||[]);
    this.byId=new Map(this.nodes.map(n=>[n.id,n]));
    if(this.selected&&!this.byId.has(this.selected))this.selected=null;
    if(this.hover)this.hover=this.byId.get(this.hover.id)||null;this.syncMarkers();
    if(!memoryMode){const clusters=createNodeFilaments(this.nodes,this.surface,[gold,violet]);this.live.add(clusters);this.clusterMaterials=clusters.children.map(c=>c.material);}
    const positions=[],colors=[],sizes=[],alphas=[];
    for(const n of this.nodes){positions.push(n.x,n.y,n.z);const col=memoryMode?new THREE.Color(n.color||CATEGORIES[n.category]?.color||'#a4edff'):n.side<0?gold:violet;colors.push(col.r*1.5,col.g*1.5,col.b*1.5);sizes.push(memoryMode?12:7+Math.min(5,Math.sqrt(n.degree||1)));alphas.push(.9);}
    this.nodeGeometry=pointsGeometry(positions,colors,sizes,alphas);this.nodePoints=new THREE.Points(this.nodeGeometry,pointsMaterial(this.dpr,true,memoryMode));this.nodePoints.renderOrder=5;this.live.add(this.nodePoints);
    this.highlight=new THREE.Points(pointsGeometry([0,0,0],[.2,1,1.6],[29],[0]),pointsMaterial(this.dpr,memoryMode,memoryMode));this.live.add(this.highlight);
    if(memoryMode){this.filterKey='';this.dirty=true;this.markerLayer.setAttribute('aria-label','真实记忆点：悬停预览，点击查看详情');return;}
    this.markerLayer.setAttribute('aria-label','大脑聚合功能区导航');
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
    this.filterKey='';this.dirty=true;
  }
  nodeMatches(n,query){return !query||(n.restricted?'受限记忆':`${n.label||''} ${n.preview||''}`).toLowerCase().includes(query);}
  nodeSummary(n){
    if(!n.functionalAnchor){const kind=n.source_kind==='imported_note'?'导入笔记（未核实）':n.source_kind==='owner_note'?'手写笔记':{fact:'事实',summary:'摘要',raw:'原话片段'}[n.kind]||'记忆',value=n.recorded_at;
      const timestamp=typeof value==='number'&&value>0?new Date(value*1000):null,time=timestamp&&!Number.isNaN(timestamp.getTime())?timestamp.toLocaleString('zh-CN',{hour12:false}):'时间未标注';
      const preview=n.restricted?'内容受使用限制，不在悬停中预览。':String(n.preview||n.label||'未提供预览').slice(0,220);
      return `${kind} · ${time}${n.pinned?' · 已置顶':''}\n${preview}\n${n.restricted?'受限记忆':'真实记忆'}${Number.isInteger(n.source_count)?` · ${n.source_count} 个来源`:''}`;
    }
    const label=String(n.label||CATEGORIES[n.category]?.name||n.id).slice(0,80),count=typeof n.count==='number'&&Number.isFinite(n.count)?n.count.toLocaleString():'—';
    const state=n.enabled===true?'已启用':n.enabled===false?'已关闭':'启用状态未报告';return `${label} · ${count}${n.unit?` ${String(n.unit).slice(0,20)}`:''} · ${state}（聚合功能区）`;}
  syncMarkers(){
    const marked=this.memoryMode?this.nodes.filter(n=>n.id===this.hover?.id||n.id===this.selected):this.nodes,ids=new Set(marked.map(n=>n.id));
    for(const [id,entry]of this.markerEntries)if(!ids.has(id)){entry.events?.abort();entry.button.remove();this.markerEntries.delete(id);}
    for(const n of marked){let entry=this.markerEntries.get(n.id);
      if(!entry){const button=document.createElement('button');button.type='button';button.className='brain-node-marker';button.dataset.nodeId=n.id;button.hidden=true;
        Object.assign(button.style,{left:'0',top:'0'});
        const dot=document.createElement('span');dot.setAttribute('aria-hidden','true');Object.assign(dot.style,{width:'7px',height:'7px',borderRadius:'50%',background:'currentColor',boxShadow:'0 0 9px currentColor'});button.appendChild(dot);
        const current=()=>this.byId.get(n.id),events=new AbortController(),eventOptions={signal:events.signal};
        button.addEventListener('pointerenter',()=>{if(!this.dragging)this.setHover(current(),'marker');},eventOptions);
        button.addEventListener('pointerleave',()=>{if(this.focusedMarker!==n.id)this.setHover(null);},eventOptions);
        button.addEventListener('focus',()=>{if(button.matches(':focus-visible')){this.focusedMarker=n.id;this.setHover(current(),'keyboard');}},eventOptions);
        button.addEventListener('blur',()=>{if(this.focusedMarker===n.id)this.focusedMarker=null;if(this.hover?.id===n.id)this.setHover(null);},eventOptions);
        button.addEventListener('pointerdown',e=>{e.stopPropagation();entry.pointerType=e.pointerType;this.focusedMarker=null;},eventOptions);
        button.addEventListener('pointercancel',()=>{if(document.activeElement===button)button.blur();this.setHover(null);},eventOptions);
        button.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){this.focusedMarker=n.id;this.setHover(current(),'keyboard');}},eventOptions);
        button.addEventListener('click',e=>{e.stopPropagation();const node=current();if(node){this.select(node.id);this.setHover(node,e.detail===0?'keyboard':'marker');if(entry.pointerType==='touch')this.hoverExpiresAt=performance.now()+2200;this.onSelect?.(node);}},eventOptions);
        this.markerLayer.appendChild(button);entry={button,events};this.markerEntries.set(n.id,entry);
      }
      entry.button.style.color=n.color||CATEGORIES[n.category]?.color||'#a4edff';entry.button.dataset.enabled=n.enabled===true?'true':n.enabled===false?'false':'unknown';entry.button.setAttribute('aria-label',this.nodeSummary(n));
    }
  }
  setHover(node,source='canvas'){
    const changed=this.hover?.id!==node?.id;this.hover=node||null;this.hoverExpiresAt=0;this.hoverSource=source;this.canvas.style.cursor=this.dragging?'grabbing':this.hover?'pointer':'grab';
    if(changed){this.filterKey='';this.dirty=true;this.lastHoverCallback=undefined;if(this.memoryMode)this.syncMarkers();}this.updateTooltip();
  }
  updateTooltip(){const tip=document.getElementById('node-tooltip'),p=this.hover&&this.projected.get(this.hover.id);
    if(!this.hover||!p){if(tip){tip.style.display='none';tip.textContent='';}if(this.lastHoverCallback!==null){this.onHover?.(null,{source:this.hoverSource||'canvas'});this.lastHoverCallback=null;}return;}
    if(tip){tip.textContent=this.nodeSummary(this.hover);tip.style.whiteSpace=this.memoryMode?'pre-line':'';tip.style.display='block';const width=tip.offsetWidth||280,height=tip.offsetHeight||50;
      tip.style.left=`${Math.max(8,Math.min(p.clientX+20,innerWidth-width-8))}px`;tip.style.top=`${Math.max(8,Math.min(p.clientY-height-12,innerHeight-height-8))}px`;}
    const callbackKey=`${this.hover.id}:${Math.round(p.clientX)}:${Math.round(p.clientY)}:${this.dataRevision}`;
    if(callbackKey!==this.lastHoverCallback){this.onHover?.(this.hover,{clientX:p.clientX,clientY:p.clientY,source:this.hoverSource||'canvas'});this.lastHoverCallback=callbackKey;}
  }
  hudOccludes(x,y){return (this.hudRects||[]).some(r=>x>=r.left&&x<=r.right&&y>=r.top&&y<=r.bottom);}
  lineOfSight(point){
    const cameraAt=this.camera.getWorldPosition(new THREE.Vector3()),world=point.world||new THREE.Vector3(point.node.x,point.node.y,point.node.z).applyMatrix4(this.brain.matrixWorld);
    this.raycaster.set(cameraAt,world.clone().sub(cameraAt).normalize());this.raycaster.far=Math.max(0,cameraAt.distanceTo(world)-.13);
    return this.raycaster.intersectObjects(this.occluders,false).length===0;
  }
  projectMarkers(o=this.options()){
    const rect=this.canvas.getBoundingClientRect();this.projected.clear();
    if(!rect.width||!rect.height||o.mode==='reference'){this.markerLayer.hidden=true;this.setHover(null);return;}
    this.markerLayer.hidden=false;this.brain.updateMatrixWorld(true);this.camera.updateMatrixWorld();
    // Test the actual HUD rectangles, not elementFromPoint (which may hit our own ring).
    // Mobile asides live below the canvas and consequently do not mask the brain.
    this.hudRects=[...document.querySelectorAll('.brain-left,.brain-right,.brain-header,.brain-footer,.visual-settings,.help-dialog[open],.login-overlay,.cinema-exit')].filter(e=>!e.hidden).map(e=>e.getBoundingClientRect()).filter(r=>r.width>0&&r.height>0);
    const normalMatrix=new THREE.Matrix3().getNormalMatrix(this.brain.matrixWorld),cameraAt=this.camera.getWorldPosition(new THREE.Vector3()),query=o.query.trim().toLowerCase();
    for(const proxy of this.occluders)proxy.matrixWorld.copy(this.brain.matrixWorld);
    const layerRect=this.markerLayer.getBoundingClientRect();
    for(const n of this.nodes){const entry=this.markerEntries.get(n.id);if(!this.memoryMode&&!entry)continue;
      const world=new THREE.Vector3(n.x,n.y,n.z).applyMatrix4(this.brain.matrixWorld),view=cameraAt.clone().sub(world).normalize();
      const normal=new THREE.Vector3(n.nx,n.ny,n.nz).applyMatrix3(normalMatrix).normalize(),p=world.clone().project(this.camera);
      let visible=o.active.includes(n.category)&&this.nodeMatches(n,query)&&normal.dot(view)>.12&&p.z>-1&&p.z<1;
      const x=rect.left+(p.x+1)*rect.width/2,y=rect.top+(1-p.y)*rect.height/2;
      visible=visible&&x>Math.max(rect.left,0)+18&&x<Math.min(rect.right,innerWidth)-18&&y>Math.max(rect.top,0)+18&&y<Math.min(rect.bottom,innerHeight)-18;
      const point={node:n,world,clientX:x,clientY:y,depthVerified:false};
      if(visible)visible=!this.hudOccludes(x,y)&&(!entry||(!this.hudOccludes(x-15,y)&&!this.hudOccludes(x+15,y)));
      // The GPU depth-tests every real point. CPU ray tests are limited to the
      // hovered/selected rings and pointer candidates, never all 600 documents.
      if(visible&&(!this.memoryMode||entry)){visible=this.lineOfSight(point);point.depthVerified=visible;}
      if(entry)entry.button.hidden=!visible;
      if(visible){if(entry){entry.button.style.left=`${x-layerRect.left}px`;entry.button.style.top=`${y-layerRect.top}px`;
        entry.button.setAttribute('aria-pressed',String(this.selected===n.id));entry.button.style.background=n.id===this.hover?.id||n.id===this.selected?'#19475beF':'#03131ed9';}
        this.projected.set(n.id,point);}
      else if(entry&&this.focusedMarker===n.id){this.focusedMarker=null;entry.button.blur();}
    }
    if(this.hover&&!this.projected.has(this.hover.id))this.setHover(null);
    this.updateTooltip();
  }
  select(id) {this.selected=id;this.filterKey='';this.dirty=true;if(this.memoryMode)this.syncMarkers();}
  rotate() {this.camera.position.applyAxisAngle(new THREE.Vector3(0,1,0),Math.PI/5);this.controls.update();this.dirty=true;}
  pick(event) {
    if(!this.nodes.length||this.options().mode==='reference'||this.hudOccludes(event.clientX,event.clientY))return null;
    if(this.memoryMode){const radius=event.pointerType==='touch'?24:13,candidates=[];
      for(const point of this.projected.values()){const distance=(point.clientX-event.clientX)**2+(point.clientY-event.clientY)**2;if(distance<=radius**2)candidates.push({point,distance});}
      candidates.sort((a,b)=>a.distance-b.distance);
      for(const {point}of candidates.slice(0,10))if(point.depthVerified||this.lineOfSight(point))return point.node;
      return null;
    }
    let best=null,dist=20**2;
    // Hover/click use exactly the same visible, search-filtered set as the rings.
    for(const p of this.projected.values()){
      const d=(p.clientX-event.clientX)**2+(p.clientY-event.clientY)**2;
      if(d<dist){best=p.node;dist=d;}
    }return best;
  }
  updateFilters(o) {
    if(!this.nodeGeometry)return;
    const requestedFocus=this.hover?.id||this.selected,focus=this.byId.has(requestedFocus)?requestedFocus:null,query=o.query.trim().toLowerCase(),key=[focus,query,o.active.join(','),o.density,o.glow].join('|');
    if(key===this.filterKey)return;this.filterKey=key;
    const neighbors=new Set();if(focus)this.edges.forEach(e=>{if(e.source===focus)neighbors.add(e.target);if(e.target===focus)neighbors.add(e.source);});
    const alpha=this.nodeGeometry.attributes.aAlpha.array,color=this.nodeGeometry.attributes.aColor.array,size=this.nodeGeometry.attributes.aSize.array;
    this.nodes.forEach((n,i)=>{const active=o.active.includes(n.category),match=this.nodeMatches(n,query),near=!focus||n.id===focus||neighbors.has(n.id),base=n.side<0?gold:violet;
      if(this.memoryMode){const importance=finiteClamp(n.importance,.5,0,1),co=new THREE.Color(n.color||CATEGORIES[n.category]?.color||'#a4edff'),power=1.35+importance*.55;
        alpha[i]=active&&match?1:0;color[i*3]=co.r*power;color[i*3+1]=co.g*power;color[i*3+2]=co.b*power;size[i]=(n.id===focus?20:11+importance*4+(n.pinned?1:0));
      }else{alpha[i]=!active||!match?0:!near?.25:1;const co=base;color[i*3]=co.r*1.8;color[i*3+1]=co.g*1.8;color[i*3+2]=co.b*1.8;size[i]=n.id===focus?34:22*(o.glow/130+.5);}
    });for(const name of ['aAlpha','aColor','aSize'])this.nodeGeometry.attributes[name].needsUpdate=true;
    const focused=this.byId.get(focus),showFocus=focused&&o.active.includes(focused.category)&&this.nodeMatches(focused,query);
    this.highlight.geometry.attributes.aAlpha.array[0]=showFocus?1:0;this.highlight.geometry.attributes.aAlpha.needsUpdate=true;if(showFocus)this.highlight.position.set(focused.x,focused.y,focused.z);
    if(this.memoryMode)return;
    this.edgeLines.material.opacity=focus?.48:.055;
    let at=0;this.visibleCurves=[];
    for(const item of this.curves){const {edge,phase,curve}=item,a=this.byId.get(edge.source),b=this.byId.get(edge.target),near=focus&&(edge.source===focus||edge.target===focus);
      const visible=o.active.includes(a.category)&&o.active.includes(b.category)&&(near||phase<o.density/100)&&(!query||a.label.toLowerCase().includes(query)||b.label.toLowerCase().includes(query));
      if(!visible)continue;this.visibleCurves.push(item);
      const col=near?new THREE.Color('#a9eaff').multiplyScalar(1.8):a.side===b.side?(a.side<0?gold:violet):cyan;
      for(let k=0;k<18;k++){for(const u of [k/18,(k+1)/18]){const p=curve.getPoint(u);this.edgePositions[at]=p.x;this.edgeColors[at++]=col.r;this.edgePositions[at]=p.y;this.edgeColors[at++]=col.g;this.edgePositions[at]=p.z;this.edgeColors[at++]=col.b;}}
    }
    this.edgeGeometry.attributes.position.needsUpdate=true;this.edgeGeometry.attributes.color.needsUpdate=true;this.edgeGeometry.setDrawRange(0,at/3);this.edgeGeometry.computeBoundingSphere();
  }
  animate(o) {
    this.stage.visible=o.art;this.bloom.strength=(.35+o.glow*.006)*(1+o.workIntensity*.07);this.rings.forEach((r,i)=>{r.rotation.y=this.time*(i===0?.04:-.055);});
    [...this.fiberMaterials,...(this.clusterMaterials||[])].forEach(m=>{m.uniforms.uTime.value=this.time;m.uniforms.uPower.value=(.60+o.glow*.008)*(1+o.workIntensity*.10);});
    this.haze.children.forEach(obj=>{obj.material.uniforms.uTime.value=this.time;});
    this.updateFilters(o);
    if(this.pulseGeometry){const p=this.pulseGeometry.attributes.position.array,alpha=this.pulseGeometry.attributes.aAlpha.array,curves=this.visibleCurves||[];
      for(let i=0;i<72;i++){const curve=curves[(i*7)%Math.max(1,curves.length)];if(!curve){alpha[i]=0;continue;}const q=curve.curve.getPoint((this.time*(.08+(i%5)*.01)+i*.271)%1);p[i*3]=q.x;p[i*3+1]=q.y;p[i*3+2]=q.z;alpha[i]=.7;}
      this.pulseGeometry.attributes.position.needsUpdate=true;this.pulseGeometry.attributes.aAlpha.needsUpdate=true;this.pulseGeometry.computeBoundingSphere();
    }
  }
  clearGroup(group) {const geometries=new Set(),materials=new Set();for(const child of [...group.children]){child.traverse(o=>{if(o.geometry)geometries.add(o.geometry);if(Array.isArray(o.material))o.material.forEach(m=>materials.add(m));else if(o.material)materials.add(o.material);});group.remove(child);}geometries.forEach(g=>g.dispose());materials.forEach(m=>m.dispose());}
  dispose() {this.dead=true;this.setHover(null);this.abort.abort();cancelAnimationFrame(this.raf);this.observer.disconnect();this.controls.removeEventListener('start',this.controlsStart);this.controls.removeEventListener('end',this.controlsEnd);this.controls.removeEventListener('change',this.controlsChange);this.controls.dispose();window.removeEventListener('blur',this.windowBlur);window.removeEventListener('scroll',this.screenChange,true);window.removeEventListener('resize',this.screenChange);this.markerEntries.forEach(entry=>entry.events?.abort());this.markerLayer.remove();this.markerEntries.clear();this.occluders.forEach(m=>{m.geometry.dispose();m.material.dispose();});this.occluders=[];if(this.mirror){this.mirror.parent?.remove(this.mirror);this.mirror.geometry.dispose();this.mirror.dispose();}this.clearGroup(this.scene);this.composer.dispose();this.bloom.dispose();this.renderer.dispose();
    for(const [name,fn]of [['pointerdown',this.pointerDown],['pointerup',this.pointerUp],['pointercancel',this.pointerCancel],['pointermove',this.pointerMove],['pointerleave',this.pointerLeave],['webglcontextlost',this.contextLost]])this.canvas.removeEventListener(name,fn);
  }
}
