from __future__ import annotations
import copy, datetime as dt, json, math, os, threading, time, uuid
from pathlib import Path

DEFAULT_SETTINGS={'capture_enabled':True,'recall_enabled':True,'semantic_enabled':True,'derive_enabled':True,
 'cross_conversation':False,'token_budget':1200,'emotion_enabled':False,
 'self_notes_enabled':False,'emotion_half_life_hours':12,'derive_interval':5,
 'emotion_pleasure_half_life_hours':12,'emotion_arousal_half_life_hours':2,
 'emotion_dominance_half_life_hours':6,
 'derive_batch_messages':12,'derive_max_wait_seconds':900,
 'life_enabled':False,'life_context_enabled':True,'life_interval_seconds':1200,
 'life_wake_time':'08:00','life_sleep_time':'23:30',
 'decay_enabled':False,'decay_rate_per_day':.01,'archive_after_days':90,'archive_below':.2}

EMOTION_AXES=('pleasure','arousal','dominance')
EMOTION_COOLDOWN_SECONDS=300
EMOTION_EVENT_LIMIT=100
EMOTION_PRESETS={
 'joy':{'label':'开心','delta':{'pleasure':.35,'arousal':.12,'dominance':.08}},
 'sadness':{'label':'难过','delta':{'pleasure':-.30,'arousal':-.08,'dominance':-.10}},
 'excitement':{'label':'激动','delta':{'pleasure':.25,'arousal':.40,'dominance':.10}},
 'stress':{'label':'紧张','delta':{'pleasure':-.20,'arousal':.35,'dominance':-.20}},
 'comfort':{'label':'安慰','delta':{'pleasure':.20,'arousal':-.12,'dominance':.12}},
 'rest':{'label':'休息','delta':{'pleasure':.08,'arousal':-.25,'dominance':.05}}}
_EMOTION_HALF_KEYS={'emotion_half_life_hours',*(f'emotion_{axis}_half_life_hours' for axis in EMOTION_AXES)}
_STANDARD_ITEM_FIELDS={'id','text','priority','enabled','scope','conversation_id','created_at','updated_at','revision'}


def _standard_id(value):
    if not isinstance(value,str) or not value.strip() or len(value)>128 or any(ord(char)<32 for char in value):
        raise ValueError('准则标识格式无效')
    return value


def _standard_values(value):
    if not isinstance(value,dict):raise ValueError('准则必须为对象')
    result=copy.deepcopy(value)
    text=result.get('text')
    if not isinstance(text,str) or not text.strip() or len(text)>1000 or '\x00' in text:
        raise ValueError('准则正文应为1至1000个字符')
    result['text']=text.strip()
    if type(result.get('priority')) is not int or not 0<=result['priority']<=100:
        raise ValueError('准则优先级应为0至100的整数')
    if type(result.get('enabled')) is not bool:raise ValueError('准则开关必须为true/false')
    if result.get('scope') not in ('owner','conversation'):raise ValueError('准则范围无效')
    conversation=result.get('conversation_id')
    if result['scope']=='owner':
        if conversation is not None:raise ValueError('所有者准则不能指定会话')
        result['conversation_id']=None
    elif not isinstance(conversation,str) or not conversation.strip() or len(conversation)>512 or '\x00' in conversation:
        raise ValueError('会话准则需要有效会话标识')
    return result


def _standard_section(value):
    """Validate stored explicit configuration, never promote factual memory."""
    if not isinstance(value,dict) or set(value)-{'enabled','version','items','history'}:raise ValueError('准则配置格式无效')
    section={'enabled':False,'version':0,'items':[],'history':[],**copy.deepcopy(value)}
    if type(section['enabled']) is not bool or type(section['version']) is not int or section['version']<0:
        raise ValueError('准则版本或开关无效')
    if not isinstance(section['items'],list) or len(section['items'])>50:raise ValueError('最多配置50条准则')
    items=[];ids=set()
    for item in section['items']:
        if not isinstance(item,dict) or set(item)!=_STANDARD_ITEM_FIELDS:raise ValueError('准则条目格式无效')
        normalized=_standard_values(item);ident=_standard_id(item['id'])
        if ident in ids:raise ValueError('准则标识重复')
        ids.add(ident)
        if type(item['revision']) is not int or item['revision']<1:raise ValueError('准则修订号无效')
        for key in ('created_at','updated_at'):
            stamp=item[key]
            if isinstance(stamp,bool) or not isinstance(stamp,(int,float)) or not math.isfinite(stamp) or stamp<0:
                raise ValueError('准则时间无效')
        if item['updated_at']<item['created_at']:raise ValueError('准则时间顺序无效')
        items.append(normalized)
    if not isinstance(section['history'],list) or any(not isinstance(item,dict) for item in section['history']):
        raise ValueError('准则修改历史格式无效')
    section['items']=items;section['history']=section['history'][-50:]
    return section


def _validate_emotion_half_lives(values):
    for key in _EMOTION_HALF_KEYS & set(values):
        value=values[key]
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not .1<=value<=720:
            raise ValueError('情绪半衰期应为0.1至720小时的有限数值')


def _emotion_projection(data,stamp):
    raw=copy.deepcopy(data['emotion']);anchor_at=raw['updated_at']
    if isinstance(anchor_at,bool) or not isinstance(anchor_at,(int,float)) or not math.isfinite(anchor_at):
        raise ValueError('情绪更新时间无效')
    elapsed=max(0.,stamp-anchor_at)/3600;axes={}
    for key in EMOTION_AXES:
        anchor=raw[key]
        if isinstance(anchor,bool) or not isinstance(anchor,(int,float)) or not math.isfinite(anchor) or not -1<=anchor<=1:
            raise ValueError('情绪信号应为-1至1之间的有限数值')
        half=data['settings'][f'emotion_{key}_half_life_hours']
        factor=math.pow(.5,elapsed/half)
        raw[key]=round(anchor*factor,4)
        axes[key]={'baseline':0.,'anchor':anchor,'anchor_at':anchor_at,
                   'half_life_hours':half,'elapsed_hours':elapsed,'decay_factor':factor,'value':raw[key]}
    events=data.get('emotion_events',[])
    raw.update(signal_only=True,rule_simulation=True,kind='rule_simulation',as_of=stamp,
               trace={'rule_version':'pad_rules_v2','axes':axes,'classification':'explicit_owner_events_only'},
               last_event=copy.deepcopy(events[-1]) if events else None,event_count=len(events))
    return raw

def _validate_derivation_settings(values):
    if 'derive_interval' in values:
        value=values['derive_interval']
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not 1<=value<=600 or not math.isfinite(value):
            raise ValueError('整理检查间隔应为1至600秒')
    if 'derive_batch_messages' in values:
        value=values['derive_batch_messages']
        if type(value) is not int or not 1<=value<=12:
            raise ValueError('每批整理应为1至12条消息（不是回合）')
    if 'derive_max_wait_seconds' in values:
        value=values['derive_max_wait_seconds']
        if type(value) is not int or not 30<=value<=86400:
            raise ValueError('最长积累时间应为30至86400秒的整数')

def _validate_life_settings(values):
    if any(type(values[key]) is not bool for key in ('life_enabled','life_context_enabled')):
        raise ValueError('生活开关必须为true/false')
    interval=values['life_interval_seconds']
    if type(interval) is not int or not 300<=interval<=86400:
        raise ValueError('生活检查间隔应为300至86400秒的整数')
    for key in ('life_wake_time','life_sleep_time'):
        value=values[key]
        if (not isinstance(value,str) or len(value)!=5 or value[2]!=':'
            or any(c not in '0123456789' for c in value[:2]+value[3:])
            or not 0<=int(value[:2])<=23 or not 0<=int(value[3:])<=59):
            raise ValueError('作息时间应为严格的24小时制HH:MM')
    if values['life_wake_time']==values['life_sleep_time']:
        raise ValueError('起床与休息时间不能相同')

class Extensions:
    """Explicit owner-managed preferences, separate from factual memory."""
    def __init__(self,path,defaults=None):
        self.path=Path(path);self.lock=threading.RLock()
        self.data={'settings':{**DEFAULT_SETTINGS,**(defaults or {})},'self':{'notes':[],'style_preferences':''},
          'emotion':{'pleasure':0.,'arousal':0.,'dominance':0.,'tags':[],'notes':'','updated_at':time.time()},
          'emotion_events':[],'standards':{'enabled':False,'version':0,'items':[],'history':[]}}
        if self.path.exists():
            old=json.loads(self.path.read_text(encoding='utf8'))
            for section in ('settings','self','emotion'):self.data[section].update(old.get(section,{}))
            # Keep existing signals on their original curve until their owner
            # explicitly changes a half-life; do not reinterpret old anchors.
            old_settings=old.get('settings',{})
            for axis in EMOTION_AXES:
                key=f'emotion_{axis}_half_life_hours'
                if key not in old_settings:self.data['settings'][key]=old_settings.get('emotion_half_life_hours',12)
            events=old.get('emotion_events',[])
            if not isinstance(events,list) or any(not isinstance(event,dict) for event in events):
                raise ValueError('情绪事件记录格式无效')
            self.data['emotion_events']=copy.deepcopy(events[-EMOTION_EVENT_LIMIT:])
            self.data['standards']=_standard_section(old.get('standards',{}))
        _validate_emotion_half_lives(self.data['settings'])
        _validate_derivation_settings(self.data['settings'])
        _validate_life_settings(self.data['settings'])
        self._save()

    def _save(self,candidate=None):
        self.path.parent.mkdir(parents=True,exist_ok=True);tmp=self.path.with_suffix('.tmp')
        with tmp.open('w',encoding='utf8') as f:json.dump(self.data if candidate is None else candidate,f,ensure_ascii=False,indent=2,allow_nan=False);f.flush();os.fsync(f.fileno())
        os.replace(tmp,self.path)
        if candidate is not None:self.data=candidate

    def settings(self):
        with self.lock:return copy.deepcopy(self.data['settings'])

    def update_settings(self,patch):
        if set(patch)-set(DEFAULT_SETTINGS):raise ValueError('不支持的设置项')
        booleans={k for k,v in DEFAULT_SETTINGS.items() if isinstance(v,bool)}
        for k,v in patch.items():
            if k in booleans and not isinstance(v,bool):raise ValueError('开关必须为true/false')
            if k=='token_budget' and (not isinstance(v,int) or not 128<=v<=8192):raise ValueError('证据预算应为128至8192')
            if k in ('decay_rate_per_day','archive_below') and (isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or not 0<=v<=1):raise ValueError('衰减参数应为0至1')
            if k=='archive_after_days' and (isinstance(v,bool) or not isinstance(v,int) or not 1<=v<=36500):raise ValueError('归档天数超出范围')
        _validate_derivation_settings(patch)
        _validate_emotion_half_lives(patch)
        with self.lock:
            candidate=copy.deepcopy(self.data);candidate['settings'].update(patch)
            if _EMOTION_HALF_KEYS & set(patch):
                stamp=time.time();projected=_emotion_projection(self.data,stamp)
                for axis in EMOTION_AXES:candidate['emotion'][axis]=projected[axis]
                candidate['emotion']['updated_at']=stamp
                if 'emotion_half_life_hours' in patch:
                    for axis in EMOTION_AXES:
                        key=f'emotion_{axis}_half_life_hours'
                        if key not in patch:candidate['settings'][key]=patch['emotion_half_life_hours']
            _validate_life_settings(candidate['settings'])
            self._save(candidate);return self.settings()

    def emotion(self):
        with self.lock:return _emotion_projection(self.data,time.time())

    def update_emotion(self,patch):
        allowed={'pleasure','arousal','dominance','tags','notes'}
        if set(patch)-allowed:raise ValueError('不支持的情绪字段')
        for key in allowed&set(patch):
            v=patch[key]
            if key in EMOTION_AXES and (isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or not -1<=v<=1):raise ValueError('情绪信号应在-1至1之间')
            if key=='tags' and (not isinstance(v,list) or len(v)>12 or any(not isinstance(t,str) or len(t)>40 for t in v)):raise ValueError('情绪标签格式错误')
            if key=='notes' and (not isinstance(v,str) or len(v)>2000):raise ValueError('情绪备注过长')
        with self.lock:
            stamp=time.time();projected=_emotion_projection(self.data,stamp)
            candidate=copy.deepcopy(self.data);target=candidate['emotion']
            for key in EMOTION_AXES:target[key]=projected[key]
            target.update(patch);target['updated_at']=stamp
            self._save(candidate);return self.emotion()

    def emotion_rules(self):
        with self.lock:
            return {'kind':'rule_simulation','rule_version':'pad_rules_v2',
              'baseline':{axis:0. for axis in EMOTION_AXES},
              'half_life_hours':{axis:self.data['settings'][f'emotion_{axis}_half_life_hours'] for axis in EMOTION_AXES},
              'range':{'minimum':-1.,'maximum':1.},'intensity':{'minimum':0.,'maximum':1.,'default':.5},
              'cooldown_seconds':EMOTION_COOLDOWN_SECONDS,'max_events':EMOTION_EVENT_LIMIT,
              'presets':[{'kind':kind,**copy.deepcopy(spec)} for kind,spec in EMOTION_PRESETS.items()],
              'automatic_classification':False,'model_calls':False,
              'decay_formula':'baseline + (anchor - baseline) * 2 ** (-elapsed_hours / half_life_hours)',
              'impulse_formula':'clamp(value + impulse * (1 - sign(impulse) * value), -1, 1)',
              'notice':'产品启发式规则，不是临床参数；仅模拟角色状态，不代表真实感受。'}

    def emotion_events(self,limit=50):
        if type(limit) is not int or not 1<=limit<=EMOTION_EVENT_LIMIT:raise ValueError('事件数量应为1至100的整数')
        with self.lock:
            events=self.data['emotion_events']
            return {'items':copy.deepcopy(list(reversed(events[-limit:]))),'total':len(events),'limit':limit}

    def apply_emotion_event(self,data):
        if not isinstance(data,dict) or set(data)-{'kind','intensity','idempotency_key'}:
            raise ValueError('情绪事件字段无效')
        kind=data.get('kind');intensity=data.get('intensity',.5)
        if not isinstance(kind,str) or kind not in EMOTION_PRESETS:raise ValueError('不支持的情绪事件类型')
        if isinstance(intensity,bool) or not isinstance(intensity,(int,float)) or not math.isfinite(intensity) or not 0<=intensity<=1:
            raise ValueError('事件强度应为0至1的有限数值')
        key=data.get('idempotency_key')
        if key is not None and (not isinstance(key,str) or not key.strip() or len(key)>128 or '\x00' in key):
            raise ValueError('事件幂等标识无效')
        with self.lock:
            stamp=time.time();events=self.data['emotion_events']
            for event in events:
                if key is not None and event.get('idempotency_key')==key:
                    if event.get('kind')!=kind or event.get('intensity')!=float(intensity):raise ValueError('事件幂等标识已用于不同内容')
                    return {'applied':False,'replayed':True,'reason':'idempotent_replay','event':copy.deepcopy(event),
                            'emotion':_emotion_projection(self.data,stamp),'cooldown_until':event['at']+EMOTION_COOLDOWN_SECONDS}
            latest=next((event for event in reversed(events) if event.get('kind')==kind),None)
            if latest is not None and stamp<latest['at']+EMOTION_COOLDOWN_SECONDS:
                return {'applied':False,'replayed':False,'reason':'cooldown','event':None,
                        'emotion':_emotion_projection(self.data,stamp),'cooldown_until':latest['at']+EMOTION_COOLDOWN_SECONDS}
            current=_emotion_projection(self.data,stamp)
            before={axis:current[axis] for axis in EMOTION_AXES};impulse={};after={}
            for axis in EMOTION_AXES:
                delta=EMOTION_PRESETS[kind]['delta'][axis]*float(intensity);impulse[axis]=delta
                direction=1 if delta>0 else -1 if delta<0 else 0
                after[axis]=round(max(-1.,min(1.,before[axis]+delta*(1-direction*before[axis]))),8)
            event={'id':uuid.uuid4().hex,'kind':kind,'intensity':float(intensity),'at':stamp,
                   'origin':'owner_manual_event','rule_version':'pad_rules_v2','before':before,'impulse':impulse,
                   'delta':{axis:round(after[axis]-before[axis],8) for axis in EMOTION_AXES},'after':after,
                   'idempotency_key':key or uuid.uuid4().hex,'cooldown_until':stamp+EMOTION_COOLDOWN_SECONDS}
            candidate=copy.deepcopy(self.data)
            candidate['emotion'].update(after);candidate['emotion']['updated_at']=stamp
            candidate['emotion_events']=[*events,event][-EMOTION_EVENT_LIMIT:]
            self._save(candidate)
            return {'applied':True,'replayed':False,'reason':'applied','event':copy.deepcopy(event),
                    'emotion':_emotion_projection(self.data,stamp),'cooldown_until':event['cooldown_until']}

    def self_notes(self):
        with self.lock:return copy.deepcopy(self.data['self'])

    def update_self(self,patch):
        if set(patch)-{'text','style_preferences','notes'}:raise ValueError('不支持的档案字段')
        with self.lock:
            candidate=copy.deepcopy(self.data);target=candidate['self']
            if 'text' in patch:
                text=str(patch['text']).strip()
                if not text or len(text)>6000:raise ValueError('档案内容不能为空或超过6000字')
                target['notes'].append({'id':uuid.uuid4().hex,'text':text,'created_at':time.time()})
            if 'style_preferences' in patch:
                text=str(patch['style_preferences'])
                if len(text)>3000:raise ValueError('表达偏好过长')
                target['style_preferences']=text
            if 'notes' in patch:
                notes=patch['notes']
                if not isinstance(notes,list) or len(notes)>100:raise ValueError('档案列表格式错误')
                normalized=[]
                for note in notes:
                    if not isinstance(note,dict) or not str(note.get('text','')).strip() or len(str(note['text']))>6000:raise ValueError('档案内容格式错误')
                    created=float(note.get('created_at') or time.time())
                    if not math.isfinite(created):raise ValueError('档案时间格式错误')
                    normalized.append({'id':str(note.get('id') or uuid.uuid4().hex),'text':str(note['text']),'created_at':created})
                target['notes']=normalized
            self._save(candidate);return self.self_notes()

    def delete_self(self,key):
        with self.lock:
            candidate=copy.deepcopy(self.data);candidate['self']['notes']=[n for n in candidate['self']['notes'] if n['id']!=key];self._save(candidate);return self.self_notes()

    def standards(self):
        with self.lock:return copy.deepcopy(self.data['standards'])

    def update_standards(self,patch):
        if not isinstance(patch,dict) or set(patch)-{'enabled','add','update'}:raise ValueError('准则修改字段无效')
        if 'enabled' in patch and type(patch['enabled']) is not bool:raise ValueError('准则开关必须为true/false')
        for command in ('add','update'):
            if command in patch and not isinstance(patch[command],dict):raise ValueError('准则修改必须为对象')
        with self.lock:
            candidate=copy.deepcopy(self.data);section=candidate['standards'];changes=[]
            latest=max([0.,*[item['updated_at'] for item in section['items']],
                        *[entry.get('at',0.) for entry in section['history']]])
            stamp=max(time.time(),latest)
            if 'enabled' in patch and section['enabled']!=patch['enabled']:
                changes.append({'action':'enabled','before':section['enabled'],'after':patch['enabled']})
                section['enabled']=patch['enabled']
            if 'add' in patch:
                raw=patch['add']
                if set(raw)-{'id','text','priority','enabled','scope','conversation_id'}:raise ValueError('不支持的准则新增字段')
                ident=_standard_id(raw.get('id',uuid.uuid4().hex))
                values=_standard_values({'text':raw.get('text'),'priority':raw.get('priority',50),
                    'enabled':raw.get('enabled',True),'scope':raw.get('scope','owner'),
                    'conversation_id':raw.get('conversation_id')})
                existing=next((item for item in section['items'] if item['id']==ident),None)
                if existing is not None:
                    if any(existing[key]!=value for key,value in values.items()):raise ValueError('准则标识已用于不同内容')
                else:
                    if len(section['items'])>=50:raise ValueError('最多配置50条准则')
                    item={'id':ident,**values,'created_at':stamp,'updated_at':stamp,'revision':1}
                    section['items'].append(item);changes.append({'action':'add','item_id':ident,'before':None,'after':copy.deepcopy(item)})
            if 'update' in patch:
                raw=patch['update']
                if set(raw)-{'id','text','priority','enabled','scope','conversation_id'}:raise ValueError('不支持的准则更新字段')
                ident=_standard_id(raw.get('id'));existing=next((item for item in section['items'] if item['id']==ident),None)
                if existing is None:raise KeyError('准则不存在')
                proposed={**existing,**{key:value for key,value in raw.items() if key!='id'}}
                if raw.get('scope')=='owner' and 'conversation_id' not in raw:proposed['conversation_id']=None
                proposed=_standard_values(proposed)
                if any(proposed[key]!=existing[key] for key in ('text','priority','enabled','scope','conversation_id')):
                    before=copy.deepcopy(existing);proposed['updated_at']=stamp;proposed['revision']=existing['revision']+1
                    section['items'][section['items'].index(existing)]=proposed
                    changes.append({'action':'update','item_id':ident,'before':before,'after':copy.deepcopy(proposed)})
            if not changes:return self.standards()
            section['version']+=1
            section['history']=[*section['history'],{'version':section['version'],'at':stamp,'operation':'patch','changes':changes}][-50:]
            candidate['standards']=_standard_section(section)
            self._save(candidate);return self.standards()

    def delete_standard(self,key):
        key=_standard_id(key)
        with self.lock:
            candidate=copy.deepcopy(self.data);section=candidate['standards']
            existing=next((item for item in section['items'] if item['id']==key),None)
            if existing is None:return self.standards()
            section['items']=[item for item in section['items'] if item['id']!=key];section['version']+=1
            stamp=max(time.time(),existing['updated_at'],*[entry.get('at',0.) for entry in section['history']])
            section['history']=[*section['history'],{'version':section['version'],'at':stamp,'operation':'delete',
                'changes':[{'action':'delete','item_id':key,'before':existing,'after':None}]}][-50:]
            self._save(candidate);return self.standards()

    def catalog(self):
        s=self.settings()
        items=[
          {'id':'memory','title':'原话与记忆','enabled':s['capture_enabled'],'implemented':True,'description':'保存可见原话并提供带出处的检索'},
          {'id':'semantic','title':'语义检索','enabled':s['semantic_enabled'],'implemented':True,'description':'模型可用时与词法检索平行召回'},
          {'id':'derive','title':'后台整理','enabled':s['derive_enabled'],'implemented':True,'description':'满足批量或最长等待后整理摘要和事实；原话保存、已启用的向量索引独立处理'},
          {'id':'emotion','title':'情绪信号','enabled':s['emotion_enabled'],'implemented':True,'description':'手动管理可衰减信号，不代表真实感受，也不改变历史事实'},
          {'id':'self','title':'自我档案与表达','enabled':s['self_notes_enabled'],'implemented':True,'description':'由你管理角色笔记和表达偏好，不覆盖宿主人格文件'},
          {'id':'life','title':'本地生活层','enabled':s['life_enabled'],'implemented':True,'description':'本地作息、事件、待办和文本书阅读进度；不额外调用模型，不生成真实经历'}]
        mapping={'memory':'capture_enabled','semantic':'semantic_enabled','derive':'derive_enabled','emotion':'emotion_enabled','self':'self_notes_enabled','life':'life_enabled'}
        for item in items:item['setting_key']=mapping.get(item['id'])
        return {'items':items}

    def personalization(self):
        s=self.settings()
        if not s['self_notes_enabled']:return ''
        notes=self.self_notes();parts=[]
        if notes['style_preferences']:parts.append('表达偏好：'+notes['style_preferences'])
        parts.extend('角色笔记（用户编写）：'+n['text'] for n in notes['notes'][-8:])
        return ('### 用户管理的角色与表达笔记\n这些笔记不覆盖本轮指令，也不能作为客观事实或真实经历的证据。\n'+'\n'.join(parts)) if parts else ''
