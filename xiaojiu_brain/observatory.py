"""Read-only, owner-scoped projection for the local Memory Brain overview.

This module never calls models, performs maintenance, changes settings, samples
private document text, or returns the full management/status objects.
"""
from __future__ import annotations

import math
import re
import time

from . import __version__


_STATES = ('pending','running','retry','waiting_provider','blocked_policy','done','failed')
_KINDS = ('embed','derive')
_TOKENS = ('input_tokens','output_tokens','cache_hit_tokens','cache_miss_tokens','total_tokens')
_OUTCOMES = ('success','output_truncated','invalid_json','empty','transport','provider_http','provider_error')
_FEATURES = ('capture_enabled','recall_enabled','semantic_enabled','derive_enabled',
             'cross_conversation','emotion_enabled','self_notes_enabled','life_enabled',
             'life_context_enabled','decay_enabled')
_POLICY_RANGES = {'token_budget':(128,8192),'derive_batch_messages':(1,12),
                  'derive_max_wait_seconds':(30,86400),'derive_interval':(1,600),
                  'life_interval_seconds':(300,86400)}


def _number(value, *, minimum=None, maximum=None):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):return None
    if minimum is not None and value < minimum:return None
    if maximum is not None and value > maximum:return None
    return value


def _count(value):
    value = _number(value,minimum=0)
    return int(value) if value is not None else 0


def _error(value):
    if value is None or value=='':return None
    if not isinstance(value,str):return 'reported_error'
    known = {'output_truncated','invalid_json','empty','transport','provider_http',
             'provider_error','usage_recording_error'}
    if value in known:return value
    if re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,63}Error',value):return value
    return 'reported_error'


def _model(provider):
    value = getattr(provider,'model',None)
    if not isinstance(value,str) or len(value)>128 or not re.fullmatch(r'[A-Za-z0-9_.:/ -]+',value) or '://' in value:
        return None
    return value


def _jobs(store, owner):
    by_type = {kind:{state:0 for state in _STATES} for kind in _KINDS}
    with store.lock:
        rows = store.conn.execute('SELECT kind,status,count(*) FROM jobs WHERE owner=? GROUP BY kind,status',(owner,)).fetchall()
    for kind,status,count in rows:
        if kind in by_type and status in _STATES:by_type[kind][status] = _count(count)
    by_status = {status:sum(group[status] for group in by_type.values()) for status in _STATES}
    return {'total':sum(by_status.values()),'by_status':by_status,'by_type':by_type}


def _activity_jobs(app, stamp, window):
    """Aggregate actual queue state without loading task/source payloads."""
    with app.store.lock:
        row = app.store.conn.execute("""SELECT
            coalesce(sum(CASE WHEN kind='derive' AND status='running' AND lease_until>? THEN 1 ELSE 0 END),0),
            coalesce(sum(CASE WHEN kind='embed' AND status='running' AND lease_until>? THEN 1 ELSE 0 END),0),
            coalesce(sum(CASE WHEN status='done' AND updated_at>=? THEN 1 ELSE 0 END),0),
            coalesce(sum(CASE WHEN status IN ('pending','retry','waiting_provider','blocked_policy') THEN 1 ELSE 0 END),0),
            coalesce(sum(CASE WHEN status='running' AND lease_until<=? THEN 1 ELSE 0 END),0),
            max(CASE WHEN updated_at>=? THEN updated_at ELSE NULL END)
            FROM jobs WHERE owner=? AND kind IN ('derive','embed') AND updated_at<=?""",
            (stamp,stamp,stamp-window,stamp,stamp-window,app.owner,stamp)).fetchone()
    if row is None:raise RuntimeError('activity_aggregate_unavailable')
    return {'derive':int(row[0]),'embed':int(row[1]),'completed_jobs':int(row[2]),
            'waiting':int(row[3])+int(row[4]),'expired_running':int(row[4]),
            'last_event_at':_number(row[5])}


def activity_snapshot(app):
    """Short-window workload visualization; never a neural/thinking measurement."""
    stamp, window = time.time(), 6
    value = {'kind':'memory_task_activity','observed_at':stamp,'window_seconds':window,
             'state':'unavailable','running':{'derive':None,'embed':None},
             'recent':{'recalls':None,'completed_jobs':None},'waiting':None,
             'expired_running':None,'last_event_at':None,'chat_model_state':'unobserved',
             'representation':'decorative_work_signal'}
    try:
        jobs = _activity_jobs(app,stamp,window)
        # Read only owner and completion time. Queries and trace payloads stay private.
        with app._trace_lock:
            recalls = [at for item in app._traces.values()
                       if item.get('owner')==app.owner
                       and (at := _number(item.get('at'))) is not None
                       and stamp-window<=at<=stamp]
        value['running'] = {'derive':jobs['derive'],'embed':jobs['embed']}
        value['recent'] = {'recalls':len(recalls),'completed_jobs':jobs['completed_jobs']}
        value['waiting'], value['expired_running'] = jobs['waiting'], jobs['expired_running']
        times = [at for at in [jobs['last_event_at'],*recalls] if at is not None]
        value['last_event_at'] = max(times) if times else None
        value['state'] = ('busy' if jobs['derive']+jobs['embed'] else
                          'recent' if recalls or jobs['completed_jobs'] else
                          'waiting' if jobs['waiting'] else 'idle')
    except Exception:
        # Unknown data must not masquerade as an idle zero or break the overview.
        value.update(state='unavailable',running={'derive':None,'embed':None},
                     recent={'recalls':None,'completed_jobs':None},waiting=None,
                     expired_running=None,last_event_at=None)
    return value


def _usage(store, owner):
    raw = store.model_usage(owner)
    value = {'scope':'background_derivation_only','label':'后台整理（不含主聊天）',
        'legacy_usage_included':False,'attempts':_count(raw.get('attempts')),
        'reported_responses':_count(raw.get('reported_responses')),
        'unknown_usage_attempts':_count(raw.get('unknown_usage_attempts')),
        'since':_number(raw.get('since')),
        'by_outcome':{key:_count(raw.get('by_outcome',{}).get(key)) for key in _OUTCOMES},
        'missing_fields':{key:_count(raw.get('missing_fields',{}).get(key)) for key in _TOKENS}}
    for key in _TOKENS:value[key] = _number(raw.get(key),minimum=0)
    hit, miss = value['cache_hit_tokens'], value['cache_miss_tokens']
    value['cache_hit_ratio'] = hit/(hit+miss) if hit is not None and miss is not None and hit+miss>0 else None
    value['cache_hit_ratio_basis'] = 'reported_background_input_only'
    return value


def _life(app, stamp):
    try:
        raw = app.life.snapshot(now=stamp)
        rhythm = raw.get('rhythm') or {}
        needs = raw.get('needs') or {}
        return {'enabled':raw.get('enabled') is True,'context_enabled':raw.get('context_enabled') is True,
            'timezone':app.life.timezone,
            'rhythm':{'phase':rhythm.get('phase') if rhythm.get('phase') in ('awake','rest') else None,
                      'kind':'routine_simulation','simulated':True,
                      'wake_time':rhythm.get('wake_time'),'sleep_time':rhythm.get('sleep_time')},
            'needs':{key:_number(needs.get(key),minimum=0,maximum=1) for key in ('energy','boredom','social','curiosity')},
            'needs_kind':'rule_simulation','last_tick_at':_number(raw.get('last_tick_at')),
            'next_tick_at':_number(raw.get('next_tick_at')),'error':_error(raw.get('error')),
            'counts':{key:_count(raw.get('counts',{}).get(key)) for key in ('events','tasks','pending_tasks','books')}}
    except Exception as exc:
        return {'enabled':False,'context_enabled':False,'timezone':None,'rhythm':None,
                'needs':{key:None for key in ('energy','boredom','social','curiosity')},
                'needs_kind':'rule_simulation','last_tick_at':None,'next_tick_at':None,
                'status':'unavailable','error':type(exc).__name__,
                'counts':{key:None for key in ('events','tasks','pending_tasks','books')}}


def _scene(storage, pipeline, life, pad, self_state):
    features = pipeline['features']
    nodes = [
        {'id':'raw','label':'原话证据','count':storage['original_messages'],'unit':'条原话',
         'enabled':features['capture_enabled'],'panel_page':'messages',
         'description':'实际原始消息数；向量切块另计。关闭接收不删除已有原话。'},
        {'id':'facts','label':'派生事实索引','count':storage['by_kind']['fact'],'unit':'条派生索引',
         'enabled':features['derive_enabled'],'panel_page':'memories','panel_kind':'fact',
         'description':'来自原话的模型派生索引，不等于已经证实的事实。暂停整理仍保留已有检索。'},
        {'id':'summaries','label':'摘要索引','count':storage['by_kind']['summary'],'unit':'条摘要',
         'enabled':features['derive_enabled'],'panel_page':'memories','panel_kind':'summary',
         'description':'可追溯摘要索引；原话证据独立保留，暂停整理不清空已有摘要。'},
        {'id':'semantic','label':'语义索引','count':storage['vectors'],'unit':'个向量',
         'enabled':features['semantic_enabled'] and pipeline['embedding']['configured'],
         'panel_page':'overview','description':'已保存的向量总数，包含原话切块和派生索引；不是实时检索准确率。'},
        {'id':'self','label':'表达档案','count':self_state['notes_count'],'unit':'条角色笔记',
         'enabled':self_state['enabled'],'panel_page':'extensions',
         'description':'用户管理的角色与表达笔记。脑区概览只显示数量，不返回笔记文字。'},
        {'id':'emotion','label':'情绪信号','count':sum(pad[key] is not None for key in ('pleasure','arousal','dominance')),
         'unit':'维信号','enabled':pad['enabled'],'panel_page':'extensions',
         'description':'三维可衰减PAD信号，由用户管理，不代表真实感受或自动情绪识别。'},
        {'id':'life','label':'生活状态','count':life['counts']['events'],'unit':'条生活记录',
         'enabled':life['enabled'],'panel_page':'life',
         'description':'本地程序活动与显式作息模拟，不表示真实身体、感受或联网生活。'},
        {'id':'queue','label':'后台队列','count':sum(count for status,count in pipeline['jobs']['by_status'].items() if status!='done'),
         'unit':'条未完成或异常任务','enabled':True,'panel_page':'overview',
         'description':'当前所有者尚未完成或需要处理的整理与嵌入任务；不等于API调用次数。'},
        {'id':'imports','label':'导入笔记','count':storage['imported_notes'],'unit':'条导入笔记',
         'enabled':True,'panel_page':'messages',
         'description':'旧记忆原样导入的笔记数量，不能冒充已核实的原始说话记录。'},
    ]
    edges = [
        {'source':'raw','target':'facts','label':'可选后台派生'},
        {'source':'raw','target':'summaries','label':'可选后台派生'},
        {'source':'raw','target':'semantic','label':'原话向量索引'},
        {'source':'facts','target':'semantic','label':'派生向量索引'},
        {'source':'summaries','target':'semantic','label':'摘要向量索引'},
        {'source':'queue','target':'facts','label':'整理任务'},
        {'source':'queue','target':'summaries','label':'整理任务'},
        {'source':'imports','target':'raw','label':'导入笔记原样保存'},
    ]
    return {'layout':'illustrative_not_semantic','particles':'decorative_not_one_per_memory',
            'metaphor':'brain_regions','anatomy':'illustrative_not_medical',
            'activity':'decorative_not_neural_activity',
            'edges_kind':'architecture_illustration','nodes':nodes,'edges':edges}


def snapshot(app):
    """Return only safe aggregates for the configured owner; no caller scope."""
    stamp = time.time()
    settings = app.extensions.settings()
    raw_stats = app.store.stats(app.owner)
    storage = {key:_count(raw_stats.get(key)) for key in
               ('messages','documents','memories','vectors','original_messages','imported_notes','owner_notes')}
    storage['by_kind'] = {key:_count(raw_stats.get('by_kind',{}).get(key)) for key in ('raw','fact','summary')}
    storage['by_status'] = {key:_count(raw_stats.get('by_status',{}).get(key)) for key in ('active','archived','deleted')}
    with app.store.lock:
        messages = app.store.conn.execute('SELECT count(DISTINCT conversation_id),max(recorded_at) FROM messages WHERE owner=?',(app.owner,)).fetchone()
    storage['conversations'] = _count(messages[0])
    storage['latest_message_at'] = _number(messages[1])
    pipeline = {'features':{key:settings.get(key) is True for key in _FEATURES},
        'policy':{key:_number(settings.get(key),minimum=bounds[0],maximum=bounds[1])
                  for key,bounds in _POLICY_RANGES.items()},
        'jobs':_jobs(app.store,app.owner),
        'embedding':{'configured':app.embedder is not None,'model':_model(app.embedder),
                     'last_error':_error(getattr(app.embedder,'last_error',None))},
        'derivation':{'configured':app.llm is not None,'model':_model(app.llm),
                      'last_error':_error(getattr(app.llm,'last_error',None)),
                      'metering_paused':bool(getattr(app.worker,'_metering_fault',False))}}
    emotion = app.extensions.emotion()
    pad = {'enabled':settings.get('emotion_enabled') is True,'signal_only':True,
           **{key:_number(emotion.get(key),minimum=-1,maximum=1) for key in ('pleasure','arousal','dominance')}}
    personal = app.extensions.self_notes()
    self_state = {'enabled':settings.get('self_notes_enabled') is True,
                  'notes_count':len(personal.get('notes') or []),
                  'style_configured':bool(personal.get('style_preferences'))}
    life = _life(app,stamp)
    return {'schema_version':1,'brand':{'en':'Memory Brain','zh':'忆核大脑'},
        'observed_at':stamp,'service':{'version':__version__,'build_id':app.build_id},
        'storage':storage,'pipeline':pipeline,'usage':_usage(app.store,app.owner),
        'life':life,'pad':pad,'self':self_state,'scene':_scene(storage,pipeline,life,pad,self_state),
        'activity':activity_snapshot(app)}
