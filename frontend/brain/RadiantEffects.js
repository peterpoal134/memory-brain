import * as THREE from 'three';
import { Reflector } from 'three/addons/objects/Reflector.js';
import { seeded } from './BrainScene.js';

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

export function createFiberNetwork(buffer, color) {
  const count=new DataView(buffer).getUint32(0,true);
  const g=new THREE.BufferGeometry();
  g.setAttribute('position',new THREE.BufferAttribute(new Float32Array(buffer,4,count*3),3));
  g.setAttribute('aAlong',new THREE.BufferAttribute(new Float32Array(buffer,4+count*12,count),1));
  g.setAttribute('aSeed',new THREE.BufferAttribute(new Float32Array(buffer,4+count*16,count),1));
  const m=new THREE.ShaderMaterial({uniforms:{uTime:{value:0},uPower:{value:1},uColor:{value:color.clone().multiplyScalar(1.7)}},vertexShader:fiberVertex,fragmentShader:fiberFragment,transparent:true,depthWrite:false,depthTest:false,blending:THREE.AdditiveBlending});
  const object=new THREE.LineSegments(g,m);object.renderOrder=2;return object;
}

export function createNodeFilaments(nodes,surfaces,colors) {
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
export function createHaze() {
  const group=new THREE.Group();
  for(const [x,y,z,size,color,opacity] of [[-.75,.10,-.30,4.0,'#ff9c36',.034],[.75,.10,-.30,4.1,'#9e38ff',.044],[0,-.35,.2,3.6,'#0099ff',.032],[0,-2.14,0,3.2,'#00ccff',.055]]){
    const m=new THREE.ShaderMaterial({uniforms:{uColor:{value:new THREE.Color(color)},uOpacity:{value:opacity},uTime:{value:0}},vertexShader:hazeVertex,fragmentShader:hazeFragment,transparent:true,depthWrite:false,depthTest:false,blending:THREE.AdditiveBlending});
    const plane=new THREE.Mesh(new THREE.PlaneGeometry(size,size),m);plane.position.set(x,y,z);plane.renderOrder=1;
    plane.onBeforeRender=(_renderer,_scene,camera)=>plane.quaternion.copy(camera.quaternion);
    group.add(plane);
  }return group;
}

export function createMirrorFloor() {
  const base=Reflector.ReflectorShader;
  const shader={...base,uniforms:{...base.uniforms},
    vertexShader:base.vertexShader.replace('varying vec4 vUv;','varying vec4 vUv; varying vec2 vFloor;').replace('vUv = textureMatrix','vFloor = position.xy; vUv = textureMatrix'),
    fragmentShader:base.fragmentShader.replace('varying vec4 vUv;','varying vec4 vUv; varying vec2 vFloor;').replace('gl_FragColor = vec4( blendOverlay( base.rgb, color ), 1.0 );','float edge = 1.0-smoothstep(3.0,5.5,length(vFloor)); gl_FragColor = vec4(base.rgb * color * edge * .37,1.0);')};
  const floor=new Reflector(new THREE.CircleGeometry(5.5,96),{textureWidth:768,textureHeight:512,multisample:0,color:'#b1c9e6',clipBias:.004,shader});
  floor.rotation.x=-Math.PI/2;floor.position.y=-2.68;floor.material.toneMapped=false;
  return floor;
}
