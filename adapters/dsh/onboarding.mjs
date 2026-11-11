/** Authenticated owner-only setup routes, registered through DSH Connection.
 * Merely loading the plugin, fetching status, or rendering the card never installs.
 */
import crypto from 'node:crypto';
import { CONTEXT_DEFAULTS, validateContextPatch } from './context-settings.mjs';

export const SETUP_PREFIX='/api/memory-brain/setup/';
const ACTIONS=new Set(['status','initialize','models','context','start','panel']);
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

function publicContextManagement(value){
  if(!value||typeof value!=='object')return null;
  const textFields=new Set(['state','scope','last_reason','mode','source','estimation_basis',
    'engine_state','summary_state','pressure_state','pruner_state']);
  const numberFields=new Set(['trigger_turns','keep_turns','successes','deferred','failures','last_before_tokens','last_after_tokens',
    'last_compressed_at','timeout_ms','archive_timeout_ms','max_input_tokens','safety_tokens','estimated_tokens','estimated_input_tokens',
    'estimated_total_tokens','limit','limit_tokens','input_limit','input_limit_tokens','reserved','reserved_tokens','remaining','remaining_tokens',
    'effective_limit','effective_input_limit','request_tokens','history_tokens','memory_tokens','output_reserve_tokens',
    'estimated_before_tokens','estimated_after_tokens','estimated_compacted_tokens','context_window_tokens','reserved_output_tokens',
    'checks','passed','blocked','unguarded','partial','warnings',
    'agents','summary_max_chars','summary_max_tokens','cooldown_ms','threshold_chars','head_chars','tail_chars',
    'threshold_ratio','retain_ratio','headroom_tokens','pressure_cooldown_ms',
    'summaries','pressure_attempts','pressure_deferred','pruned_results','pruned_chars','last_summary_chars']);
  const booleanFields=new Set(['enabled','available','supported','blocked','estimated','active','stopped']);
  const clean=source=>{
    const result={};if(!source||typeof source!=='object')return result;
    for(const [key,item]of Object.entries(source)){
      if(textFields.has(key)&&typeof item==='string'&&/^[a-zA-Z0-9_:.-]{0,100}$/.test(item))result[key]=item;
      else if(numberFields.has(key)&&(item===null||typeof item==='number'&&Number.isFinite(item)&&item>=0))result[key]=item;
      else if(booleanFields.has(key)&&typeof item==='boolean')result[key]=item;
    }return result;
  };
  const support=value.support;
  return {...clean(value),compression:clean(value.compression),budget:clean(value.budget),
    support:{...clean(support),summary:clean(support?.summary),pressure:clean(support?.pressure),
      tool_pruning:clean(support?.tool_pruning),token_meter:clean(support?.token_meter)}};
}
function publicSessionHistory(value){
  if(!value||typeof value!=='object')return null;
  const result={};
  for(const key of ['enabled','available','tool_available','auto_enabled'])if(typeof value[key]==='boolean')result[key]=value[key];
  for(const key of ['state','last_reason'])if(typeof value[key]==='string'&&/^[a-zA-Z0-9_:.-]{0,100}$/.test(value[key]))result[key]=value[key];
  for(const key of ['searches','automatic_checks','matches','failures'])if(Number.isFinite(value[key])&&value[key]>=0)result[key]=value[key];
  return result;
}
export function createOnboarding({manager,onStart,home='',compatibility='',legacy=false,getAdapterStatus,getContextSettings,saveContextSettings}={}){
  const csrfToken=crypto.randomBytes(32).toString('base64url');
  const diagnosticOnly=compatibility?.supported===false;
  let disposed=false,busy=false,operation=null,lastError=null,connected=false,task=null;
  async function status(){
    let runtime;try{runtime=publicRuntime(await manager.status());}catch(error){runtime=publicRuntime({state:'error',error:failure(error)});}
    const adapter=typeof getAdapterStatus==='function'?getAdapterStatus():null;
    let adapterSnapshot;try{adapterSnapshot=adapter?.status?.();}catch{/* diagnostics are optional */}
    const enabled=!diagnosticOnly&&(typeof getAdapterStatus==='function'?adapter?.enabled===true:connected);
    let capture=null;
    if(enabled)try{const observed=adapterSnapshot?.capture;
      if(Number.isSafeInteger(observed?.attached_sessions)&&observed.attached_sessions>=0)
        capture={attached_sessions:observed.attached_sessions,live_discovery:observed.live_discovery===true};
    }catch{/* optional diagnostics */}
    let context=null,contextManagement=null;
    if(!diagnosticOnly){
      if(typeof getContextSettings==='function')try{
        const value=await getContextSettings();
        const settings={...CONTEXT_DEFAULTS,...validateContextPatch(value?.settings)};
        if(settings.contextKeepTurns>=settings.contextTriggerTurns)throw invalid('context_keep_must_be_below_trigger');
        context={settings,restart_required:value?.restart_required===true};
      }catch(error){lastError=failure(error);}
      try{contextManagement=publicContextManagement(adapterSnapshot?.context_management);}catch{/* optional observed capability */}
    }
    return {phase:disposed?'closed':diagnosticOnly?'compatibility_required':busy?'working':enabled?'connected':runtime.installed_version?'ready':'not_initialized',
      operation,busy,error:lastError||runtime.error,csrfToken,runtime,home,compatibility,legacy,
      context,adapter:{enabled,capture,context_management:contextManagement,session_history:diagnosticOnly?null:publicSessionHistory(adapterSnapshot?.session_history)}};
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
    if(action==='context'){
      if(typeof saveContextSettings!=='function'||!fields.length)return reply(400,'context_settings_unavailable',false);
      try{validateContextPatch(body);}catch(error){return reply(400,failure(error),false);}
      return enqueue(action,()=>saveContextSettings(body));
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
  const setError=error=>{lastError=error==null?null:failure(error);};
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
