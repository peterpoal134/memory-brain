"""Bounded owner-configured role context, independent of factual retrieval.

Only explicit preference sections are inputs. Neither retrieved documents nor
host-persona snapshots can become behavioral standards through this module.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math

from .policy import estimate_tokens


_HEADER=('用户明确配置的角色与准则（不是事实记忆）。\n'
         '优先级仅决定本用户层内部顺序，服从系统、开发者等更高层指令；'
         '不代表事实或真实经历。仅加载下面列出的完整条目。')


def build_context(extensions,conversation_id,token_limit=384):
    if not isinstance(conversation_id,str) or not conversation_id.strip() or len(conversation_id)>512 or '\x00' in conversation_id:
        raise ValueError('角色上下文需要有效会话标识')
    if type(token_limit) is not int or not 1<=token_limit<=384:raise ValueError('角色上下文预算应为1至384的整数')
    # A short preference lock is independent of Store's retrieval lock. Copy
    # current configuration only; the private revision history is never input.
    with extensions.lock:
        settings=extensions.data['settings']
        self_enabled=settings.get('self_notes_enabled') is True
        self_notes=copy.deepcopy(extensions.data['self'])
        section=extensions.data['standards']
        standards={'enabled':section['enabled'],'version':section['version'],'items':copy.deepcopy(section['items'])}
    version_state={'self_enabled':self_enabled,'self':self_notes,'standards':standards}
    version=hashlib.sha256(json.dumps(version_state,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()[:16]
    trace={'profile_version':version,'standards_version':standards['version'],'token_limit':token_limit,
           'estimated_tokens':0,'self_enabled':self_enabled,'standards_enabled':standards['enabled'],
           'included_ids':[],'omitted_ids':[],'omitted_count':0,'standard_ids':[],'self_note_ids':[],
           'style_included':False,'eligible_count':0,'priority_scope':'user_layer_only',
           'source':'explicit_owner_configuration','reason':'empty'}
    units=[];degraded=[]
    if standards['enabled']:
        relevant=[item for item in standards['items'] if item['enabled'] and
                  (item['scope']=='owner' or (item['scope']=='conversation' and item['conversation_id']==conversation_id))]
        for item in sorted(relevant,key=lambda item:(-item['priority'],item['id'])):
            units.append({'id':'standard:'+item['id'],'source_id':item['id'],'kind':'standard',
                          'block':f"[准则 {item['id']}；用户层顺序 {item['priority']}；修订 {item['revision']}]\n"+item['text']})
    if self_enabled:
        style=self_notes.get('style_preferences','')
        if isinstance(style,str) and style.strip():
            units.append({'id':'self:style_preferences','source_id':'style_preferences','kind':'style','block':'[表达偏好]\n'+style})
        def order(note):
            stamp=note.get('created_at',0)
            if isinstance(stamp,bool) or not isinstance(stamp,(int,float)) or not math.isfinite(stamp):stamp=0
            return stamp,str(note.get('id',''))
        for index,note in enumerate(sorted(self_notes.get('notes',[]),key=order)):
            ident=str(note.get('id') or 'legacy-'+str(index));text=note.get('text')
            if not isinstance(text,str) or not text.strip():
                trace['omitted_ids'].append('self:'+ident);degraded.append('profile_invalid_entry');continue
            units.append({'id':'self:'+ident,'source_id':ident,'kind':'self','block':'[自我档案 '+ident+']\n'+text})
    trace['eligible_count']=len(units)
    parts=[]
    for unit in units:
        candidate='\n\n'.join([_HEADER,*parts,unit['block']])
        if estimate_tokens(candidate)>token_limit:
            trace['omitted_ids'].append(unit['id']);continue
        parts.append(unit['block']);trace['included_ids'].append(unit['id'])
        if unit['kind']=='standard':trace['standard_ids'].append(unit['source_id'])
        elif unit['kind']=='self':trace['self_note_ids'].append(unit['source_id'])
        else:trace['style_included']=True
    block='\n\n'.join([_HEADER,*parts]) if parts else ''
    trace['estimated_tokens']=estimate_tokens(block) if block else 0
    trace['omitted_count']=len(trace['omitted_ids'])
    if trace['omitted_count']:degraded.append('profile_context_omitted_budget')
    trace['reason']='included' if block else ('disabled' if not self_enabled and not standards['enabled'] else 'omitted_budget' if units else 'empty')
    return {'block':block,'trace':trace,'degraded':list(dict.fromkeys(degraded))}
