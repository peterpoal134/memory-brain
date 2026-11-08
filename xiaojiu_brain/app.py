from __future__ import annotations
from collections import OrderedDict
import datetime as dt, json, os, secrets, sqlite3, threading, time, uuid, zipfile
from pathlib import Path
from . import __version__
from .config import Config,build_id
from .storage import Store
from .retrieval import Retriever
from .derivation import DerivationWorker
from .providers import EmbeddingProvider,ChatProvider
from .extensions import Extensions
from .life import LifeLayer
from .policy import estimate_tokens
from .profile_context import build_context as build_profile_context

class BrainApp:
    def __init__(self,config:Config,start_worker=True):
        config.validate();self.config=config;self.owner=config.owner;self.build_id=build_id()
        self.data_dir=Path(config.data_dir).resolve();self.data_dir.mkdir(parents=True,exist_ok=True)
        self.store=Store(self.data_dir/'brain.db')
        self.extensions=Extensions(self.data_dir/'preferences.json',{'derive_enabled':config.derive_enabled,
            'token_budget':config.token_budget,'derive_interval':config.derive_interval,
            'derive_batch_messages':config.derive_batch_messages,'derive_max_wait_seconds':config.derive_max_wait_seconds})
        self.life=LifeLayer(self.store,self.owner,config.timezone,self.extensions.settings,emotion_provider=self.extensions.emotion)
        self.embedder=EmbeddingProvider(config.embed_model,config.embed_url,os.getenv(config.embed_key_env),timeout=3) if config.embed_url else None
        self.llm=ChatProvider(config.llm_model,config.llm_url,os.getenv(config.llm_key_env),timeout=config.model_timeout) if config.llm_url and config.llm_model else None
        if self.llm:self.llm.attempt_recorder=lambda event:self.store.record_model_attempt(self.owner,event)
        self.retriever=Retriever(self.store,embedder=self.embedder,timezone=config.timezone)
        self.worker=DerivationWorker(self.store,embedder=self.embedder,llm=self.llm,interval=config.derive_interval,timezone=config.timezone)
        self._trace_lock=threading.Lock();self._traces=OrderedDict();self._apply_settings()
        self._tickets={};self._ticket_lock=threading.Lock();self._stop=threading.Event();self._maintenance_thread=None
        self.maintenance_status={'last_run':None,'last_error':None,'result':None}
        if start_worker:
            self.worker.start()
            self.life.start()
            self._maintenance_thread=threading.Thread(target=self._maintenance_loop,daemon=True,name='brain-retention-policy');self._maintenance_thread.start()

    def issue_ticket(self):
        with self._ticket_lock:
            now=time.monotonic();self._tickets={k:v for k,v in self._tickets.items() if v>now}
            if len(self._tickets)>32:self._tickets.clear()
            ticket=secrets.token_urlsafe(32);self._tickets[ticket]=now+60;return ticket

    def redeem_ticket(self,ticket):
        with self._ticket_lock:
            until=self._tickets.pop(str(ticket),0)
            if until<=time.monotonic():raise ValueError('登录链接已过期或已使用')
            return {'token':self.config.api_token}

    def maintain(self):
        s=self.extensions.settings()
        try:
            result=self.store.maintain(self.owner,rate=s['decay_rate_per_day'],archive_below=s['archive_below'],archive_days=s['archive_after_days'])
            self.maintenance_status={'last_run':time.time(),'last_error':None,'result':result};return result
        except Exception as e:
            self.maintenance_status={'last_run':time.time(),'last_error':type(e).__name__,'result':None};raise

    def _maintenance_loop(self):
        while not self._stop.wait(60):
            if self.extensions.settings()['decay_enabled']:
                try:self.maintain()
                except Exception:continue  # maintain() records the failure category in status.

    def _apply_settings(self):
        s=self.extensions.settings()
        self.retriever.embedder=self.embedder if s['semantic_enabled'] else None
        self.retriever.allow_cross_conversation=s['cross_conversation']
        self.worker.embedder=self.embedder if s['semantic_enabled'] else None
        self.worker.llm=self.llm if s['derive_enabled'] else None
        self.worker.interval=s['derive_interval']
        self.worker.min_batch_messages=s['derive_batch_messages']
        self.worker.max_wait_seconds=s['derive_max_wait_seconds']

    def status(self):
        self._apply_settings()
        return {'version':__version__,'build_id':self.build_id,'stats':self.store.stats(self.owner),
          'jobs':self.store.job_stats(),'worker':self.worker.last_result,'maintenance':self.maintenance_status,
          'model_usage':self.store.model_usage(self.owner),'settings':self.extensions.settings(),'providers':{
           'embedding':{'configured':self.embedder is not None,'model':self.config.embed_model,'last_error':getattr(self.embedder,'last_error',None)},
           'derivation':{'configured':self.llm is not None,'model':self.config.llm_model,'last_error':getattr(self.llm,'last_error',None),'calls':getattr(self.llm,'calls',0)}},
          'extensions':self.extensions.catalog()['items'],'life':self.life.snapshot(),'config':self.config.public()}

    def ingest(self,data):
        if not self.extensions.settings()['capture_enabled']:return {'accepted':False,'duplicate':False,'reason':'capture_paused'}
        conversation=str(data.get('conversation_id') or 'manual')
        result=self.store.ingest(self.owner,conversation,
           str(data.get('message_id') or ''),str(data.get('role') or ''),data.get('text'),
           recorded_at=data.get('recorded_at'),metadata=data.get('metadata'))
        # The already-committed original is never held hostage by optional life state.
        if self.extensions.settings()['life_enabled'] and result.get('id'):
            try:self.life.note_chat(result['id'],conversation)
            except Exception as exc:self.life.error=type(exc).__name__
        return result

    def remember(self,data):
        text=data.get('text');conv=str(data.get('conversation_id') or 'manual')
        if not isinstance(text,str) or not text.strip():raise ValueError('记忆内容不能为空')
        importance=data.get('importance',.5)
        if isinstance(importance,bool) or not isinstance(importance,(int,float)) or not 0<=importance<=1:raise ValueError('重要性应为0至1')
        if data.get('disclosure','normal') not in ('normal','only_if_asked','never_say'):raise ValueError('披露策略无效')
        metadata={**(data.get('metadata') or {}),'source_kind':'owner_note'}
        if 'disclosure' in data:metadata['disclosure']=data['disclosure']
        if 'disclosure_topics' in data:metadata['disclosure_topics']=data['disclosure_topics']
        result=self.store.ingest(self.owner,conv,'note-'+uuid.uuid4().hex,'user',text,metadata=metadata)
        source=self.store.get_message(self.owner,result['id'])
        derived=self.store.add_derived(self.owner,conv,'fact',text,
          [{'message_id':result['id'],'start':0,'end':len(source['text'])}],
          metadata={**metadata,'owner_authored':True,'claims_verified':False},
          importance=float(data.get('importance',.5)),disclosure=str(metadata.get('disclosure','normal')))
        result['document_id']=derived['id']
        if 'importance' in data or 'pinned' in data:
            patch={k:data[k] for k in ['importance','pinned'] if k in data}
            for doc in self.store.documents(self.owner,conv):
                if any(int(r['message_id'])==result['id'] for r in doc['source_refs']):self.store.update_memory(self.owner,doc['id'],patch)
        return result

    def remember_important(self,data):
        if not isinstance(data,dict) or set(data)-{'conversation_id','request_message_id','source_refs','disclosure','importance'}:
            raise ValueError('important_invalid_request_fields')
        if 'importance' in data and (type(data['importance']) not in (int,float) or data['importance']!=.95):
            raise ValueError('important_importance_is_fixed')
        return self.store.remember_important(self.owner,data.get('conversation_id'),
            data.get('request_message_id'),data.get('source_refs'),data.get('disclosure'))

    def life_context(self,data):
        """Fast optional state delivery, independent of memory/model retrieval."""
        if not isinstance(data,dict) or set(data)-{'conversation_id'}:
            raise ValueError('生活状态请求字段无效')
        conversation=data.get('conversation_id')
        if not isinstance(conversation,str) or not conversation.strip() or len(conversation)>512:
            raise ValueError('生活状态需要有效会话标识')
        conversation=conversation.strip()
        result={'block':'','trace':{'life_context_included':False,
            'life_context_token_limit':160,'life_context_estimated_tokens':0,'estimated_tokens':0},'degraded':[]}
        settings=self.extensions.settings()
        if not settings['life_enabled'] or not settings['life_context_enabled']:return result
        connection=None
        try:
            if self.store.path==':memory:':raise RuntimeError('independent_life_database_unavailable')
            connection=sqlite3.connect(Path(self.store.path).resolve().as_uri()+'?mode=ro',
                                      uri=True,timeout=.05,isolation_level=None)
            connection.row_factory=sqlite3.Row
            connection.execute('PRAGMA query_only=ON')
            deadline=time.monotonic()+.3
            connection.set_progress_handler(lambda:1 if time.monotonic()>deadline else 0,1000)
            connection.execute('BEGIN')  # One short, consistent read-only WAL snapshot.
            candidate=''
            for char_limit in range(180,49,-5):
                candidate=self.life.context(conversation,max_chars=char_limit,connection=connection)
                if not candidate:break
                tokens=estimate_tokens(candidate)
                if tokens<=160:
                    result['block']=candidate
                    result['trace'].update(life_context_included=True,
                        life_context_estimated_tokens=tokens,estimated_tokens=tokens)
                    break
            if candidate and not result['block']:result['degraded'].append('life_context_omitted_budget')
        except Exception as exc:
            self.life.error=type(exc).__name__
            result['degraded'].append('life_context_unavailable')
        finally:
            if connection is not None:connection.close()
        return result

    def profile_context(self,data):
        if not isinstance(data,dict) or set(data)-{'conversation_id','token_limit'}:
            raise ValueError('角色状态请求字段无效')
        conversation=data.get('conversation_id')
        if not isinstance(conversation,str) or not conversation.strip() or len(conversation)>512:
            raise ValueError('角色状态需要有效会话标识')
        limit=data.get('token_limit',384)
        if type(limit) is not int or not 64<=limit<=384:raise ValueError('角色状态额度应为64至384')
        return build_profile_context(self.extensions,conversation.strip(),token_limit=limit)

    def recall(self,data):
        self._apply_settings();settings=self.extensions.settings();query=str(data.get('query') or '').strip()
        if not query or len(query)>12000:raise ValueError('问题不能为空或过长')
        include_life_context=data.get('include_life_context',True)
        if not isinstance(include_life_context,bool):raise ValueError('生活状态附加开关必须为布尔值')
        include_profile_context=data.get('include_profile_context',True)
        if not isinstance(include_profile_context,bool):raise ValueError('角色状态附加开关必须为布尔值')
        conversation=str(data.get('conversation_id') or 'manual')
        exclude=[];external=data.get('current_message_id');as_of=data.get('as_of');cutoff_message_id=None
        if external:
            row=self.store.find_message(self.owner,conversation,str(external),role='user')
            if row:
                exclude.append(row['id'])
                cutoff_message_id=row['id']
                from .retrieval import _epoch
                # The archived question is the trusted observation boundary.
                # A caller's later wall clock cannot admit facts after it.
                as_of=min(_epoch(as_of),row['recorded_at'])
            else:
                # Refuse to guess a source cutoff from substring matching.
                return {'evidence':[],'assertions':[],'conflicts':[],'coverage_gaps':['current_message_not_archived'],
                  'degraded':['current_message_not_archived'],'block':'','policy_version':'evidence-first-1','build_id':self.build_id}
        if not settings['recall_enabled']:
            return {'evidence':[],'assertions':[],'conflicts':[],'coverage_gaps':['recall_disabled'],'degraded':[],'block':'','policy_version':'evidence-first-1','build_id':self.build_id}
        budget=data.get('token_budget',settings['token_budget'])
        if isinstance(budget,bool) or not isinstance(budget,int) or not 128<=budget<=8192:raise ValueError('证据预算应为128至8192')
        topics=data.get('requested_topics') or []
        if not isinstance(topics,list) or len(topics)>12 or any(not isinstance(t,str) or len(t)>80 for t in topics):raise ValueError('披露主题格式错误')
        result=self.retriever.recall(self.owner,None if settings['cross_conversation'] else conversation,query,
          token_budget=budget,as_of=as_of,exclude_message_ids=exclude,requested_topics=topics,cutoff_message_id=cutoff_message_id)
        result['build_id']=self.build_id;personal=self.extensions.personalization() if include_profile_context else ''
        result.setdefault('trace',{})['profile_context_mode']='legacy_budgeted' if include_profile_context else 'independent_request_channel'
        if personal:
            combined=(result.get('block','')+'\n\n'+personal).strip()
            if estimate_tokens(combined)<=budget:
                result['block']=combined
                result.setdefault('trace',{})['personalization_included']=True
            else:
                result.setdefault('coverage_gaps',[]).append({'code':'personalization_omitted_budget'})
                result.setdefault('trace',{})['personalization_included']=False
            result['trace']['estimated_tokens']=estimate_tokens(result.get('block',''))
        # Life is a labelled side channel, not source evidence. Preserve the full
        # retrieval allowance and give state a separate, small, explicit ceiling.
        # Retrieval commonly fills its own budget; sharing it silently hid life.
        evidence_block=result.get('block','')
        evidence_tokens=estimate_tokens(evidence_block)
        life_context='';combined=evidence_block
        if include_life_context:
            try:
                for char_limit in range(180,49,-5):
                    life_context=self.life.context(conversation,max_chars=char_limit)
                    combined=(evidence_block+'\n\n'+life_context).strip() if life_context else evidence_block
                    if not life_context or estimate_tokens(combined)-evidence_tokens<=160:break
            except Exception as exc:
                self.life.error=type(exc).__name__;life_context=''
                result.setdefault('degraded',[]).append('life_context_unavailable')
        result.setdefault('trace',{})['life_context_included']=False
        result['trace']['evidence_estimated_tokens']=evidence_tokens
        result['trace']['life_context_token_limit']=160
        result['trace']['life_context_estimated_tokens']=0
        if life_context:
            if estimate_tokens(combined)-evidence_tokens<=160:
                result['block']=combined;result['trace']['life_context_included']=True
                result['trace']['life_context_estimated_tokens']=estimate_tokens(combined)-evidence_tokens
            else:result.setdefault('coverage_gaps',[]).append({'code':'life_context_omitted_budget'})
        result['trace']['estimated_tokens']=estimate_tokens(result.get('block',''))
        trace_id=uuid.uuid4().hex
        with self._trace_lock:
            self._traces[trace_id]={'owner':self.owner,'conversation_id':conversation,'at':time.time(),'query':query,
              'trace':result.get('trace',{}),'degraded':result.get('degraded',[]),'coverage_gaps':result.get('coverage_gaps',[])}
            while len(self._traces)>100:self._traces.popitem(last=False)
        result['trace_id']=trace_id;return result

    def trace(self,key):
        with self._trace_lock:
            item=self._traces.get(key)
            if not item or item['owner']!=self.owner:raise KeyError('检索记录不存在或已过期')
            return item

    def backup(self):
        directory=self.data_dir/'backups';directory.mkdir(exist_ok=True)
        key=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:8]
        db=directory/(key+'.db');result=self.store.backup(db)
        output=directory/(key+'.zip')
        with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED,compresslevel=1) as z:
            z.write(db,'brain.db');z.writestr('preferences.json',json.dumps(self.extensions.data,ensure_ascii=False,indent=2))
            z.writestr('manifest.json',json.dumps({'version':__version__,'build_id':self.build_id,'backup':result,'created_at':time.time()},ensure_ascii=False,indent=2))
        db.unlink()
        return {'id':key,'file_name':output.name,'url':'/v1/backups/'+key,'bytes':output.stat().st_size,'integrity':result}

    def close(self):
        self._stop.set()
        if self._maintenance_thread:self._maintenance_thread.join(2)
        life_stopped=self.life.stop()
        if self.worker.stop() and life_stopped is not False:self.store.close()
