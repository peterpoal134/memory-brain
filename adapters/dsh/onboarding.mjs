/** Authenticated owner-only setup routes, registered through DSH Connection.
 * Merely loading the plugin, fetching status, or rendering the card never installs.
 */
import crypto from 'node:crypto';

export const SETUP_PREFIX='/api/memory-brain/setup/';
const ACTIONS=new Set(['status','initialize','models','start','panel']);
const MODEL_FIELDS=new Set(['embed_url','embed_model','embed_key','llm_url','llm_model','llm_key']);
const LIMIT=16384;
const failure=error=>{
  const candidate=error?.code||error?.message;
  return typeof candidate==='string'&&/^[a-zA-Z][a-zA-Z0-9_:-]{0,100}$/.test(candidate)?candidate:'setup_operation_failed';
};
const reply=(status,data,ok=true)=>Response.json(ok?{ok:true,data}:{ok:false,error:data},
  {status,headers:{'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}});
function invalid(code){const error=new Error(code);error.code=code;return error;}
async function readBody(request){
  if(request.headers.get('content-type')?.split(';')[0].trim().toLowerCase()!=='application/json')throw invalid('json_required');
  const declared=request.headers.get('content-length');
  if(declared!==null&&(!/^\d+$/.test(declared)||Number(declared)>LIMIT))throw invalid('body_too_large');
  const reader=request.body?.getReader();let size=0,chunks=[];
  if(reader)try{
    while(true){const {done,value}=await reader.read();if(done)break;size+=value.byteLength;
      if(size>LIMIT){await reader.cancel();throw invalid('body_too_large');}chunks.push(value);}
  }finally{reader.releaseLock();}
  let body;try{body=JSON.parse(Buffer.concat(chunks).toString('utf8'));}catch{throw invalid('invalid_json');}
  if(!body||typeof body!=='object'||Array.isArray(body))throw invalid('object_required');
  return body;
}
function publicRuntime(value={}){
  const text=value=>typeof value==='string'?value.slice(0,100):null;
  return {state:text(value.state),expected_version:text(value.expected_version),installed_version:text(value.installed_version),
    python:{available:value.python?.available===true,version:text(value.python?.version)},
    models:{embedding_configured:value.models?.embedding_configured===true,derivation_configured:value.models?.derivation_configured===true,
      embed_key_saved:value.models?.embed_key_saved===true,llm_key_saved:value.models?.llm_key_saved===true},
    running:value.running===true,restart_required:value.restart_required===true,error:value.error?failure({code:value.error}):null};
}

export function createOnboarding({manager,onStart,home='',compatibility='',legacy=false,getAdapterStatus}={}){
  const csrfToken=crypto.randomBytes(32).toString('base64url');
  const diagnosticOnly=compatibility?.supported===false;
  let disposed=false,busy=false,operation=null,lastError=null,connected=false,task=null;
  async function status(){
    let runtime;try{runtime=publicRuntime(await manager.status());}catch(error){runtime=publicRuntime({state:'error',error:failure(error)});}
    const adapter=typeof getAdapterStatus==='function'?getAdapterStatus():null;
    const enabled=!diagnosticOnly&&(typeof getAdapterStatus==='function'?adapter?.enabled===true:connected);
    return {phase:disposed?'closed':diagnosticOnly?'compatibility_required':busy?'working':enabled?'connected':runtime.installed_version?'ready':'not_initialized',
      operation,busy,error:lastError||runtime.error,csrfToken,runtime,home,compatibility,legacy,
      adapter:{enabled}};
  }
  function enqueue(name,action){
    if(disposed)return reply(410,'setup_closed',false);
    if(busy)return reply(409,'setup_busy',false);
    busy=true;operation=name;lastError=null;
    task=Promise.resolve().then(()=>{if(disposed)throw invalid('setup_closed');return action();})
      .then(()=>{if(name==='start'&&!disposed)connected=true;})
      .catch(error=>{if(!disposed)lastError=failure(error);})
      .finally(()=>{busy=false;operation=null;});
    return reply(202,{accepted:true,operation:name});
  }
  async function handle(request){
    const url=new URL(request.url),action=url.pathname.slice(SETUP_PREFIX.length);
    if(!url.pathname.startsWith(SETUP_PREFIX)||!ACTIONS.has(action))return reply(404,'not_found',false);
    if(disposed)return reply(410,'setup_closed',false);
    if(action==='status')return request.method==='GET'?reply(200,await status()):reply(405,'method_not_allowed',false);
    if(request.method!=='POST')return reply(405,'method_not_allowed',false);
    if(diagnosticOnly)return reply(409,'host_build_unverified',false);
    // Connection's physical carrier has already authenticated an owner Peer.
    // This additional browser-intent token cannot be replaced by a chat message.
    const supplied=request.headers.get('x-memory-brain-setup')||'';
    if(!/^[A-Za-z0-9_-]{43}$/.test(supplied)||!crypto.timingSafeEqual(Buffer.from(supplied),Buffer.from(csrfToken)))
      return reply(403,'setup_confirmation_required',false);
    const origin=request.headers.get('origin');
    if(origin!==null){try{if(new URL(origin).origin!==url.origin)return reply(403,'untrusted_origin',false);}catch{return reply(403,'untrusted_origin',false);}}
    let body;try{body=await readBody(request);}catch(error){return reply(error.code==='body_too_large'?413:400,failure(error),false);}
    const fields=Object.keys(body);
    if(action==='initialize'){
      if(fields.some(key=>key!=='allowDownloads')||body.allowDownloads!==true)return reply(400,'explicit_download_consent_required',false);
      return enqueue(action,()=>manager.initialize({allowDownloads:true}));
    }
    if(action==='models'){
      if(fields.some(key=>!MODEL_FIELDS.has(key))||fields.some(key=>typeof body[key]!=='string')||!fields.length)
        return reply(400,'invalid_model_fields',false);
      return enqueue(action,()=>manager.saveModels(body));
    }
    if(fields.length)return reply(400,'unexpected_fields',false);
    if(action==='start')return enqueue(action,async()=>{await onStart();});
    if(busy)return reply(409,'setup_busy',false);
    try{
      const address=await manager.panelUrl(),target=new URL(address);
      if(target.protocol!=='http:'||!['127.0.0.1','localhost','[::1]'].includes(target.hostname)||
        target.username||target.password||target.search||!['/brain','/panel'].includes(target.pathname)||
        !/^#ticket=[A-Za-z0-9_-]+$/.test(target.hash))throw invalid('invalid_panel_ticket');
      return reply(200,{url:target.href});
    }catch(error){return reply(409,failure(error),false);}
  }
  async function dispose(){if(disposed)return;disposed=true;connected=false;await manager.dispose?.();}
  const setError=error=>{lastError=failure(error);};
  return Object.freeze({status,handle,dispose,setError});
}

export function mountOnboarding(ctx,controller){
  const connection=ctx.get?.('connection')||ctx.connection;
  if(typeof connection?.fetch?.register!=='function')throw invalid('dsh_setup_connection_routes_required');
  const off=[];
  try{for(const action of ACTIONS)off.push(connection.fetch.register({
    path:SETUP_PREFIX+action,methods:[action==='status'?'GET':'POST'],requestBody:'buffered',fetch:request=>controller.handle(request)}));}
  catch(error){for(const dispose of off)void dispose();throw error;}
  return async()=>{await Promise.all(off.map(dispose=>dispose()));};
}
