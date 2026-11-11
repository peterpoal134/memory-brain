"""Optional, bounded date leads from permitted user originals.

This is not a task list, a reminder, a verified event or a consent detector.
The caller must first apply owner, conversation, source, cutoff and disclosure
checks. No model-generated text or broad inference is used here.
"""
from __future__ import annotations
import datetime as dt
import re
from .temporal import source_times

_PLAN=re.compile(r'计划|打算|准备|安排|预约|约好|约了|答应|约定|要去|要交|要带|要给|要做|要开|要见|要到|要还|得去|记得去|提醒我|(?:让我|叫我|通知我)去')
_UNSAFE=re.compile(r'不要|别提|不准|不想|不用|取消|改期|推迟|已经|已完成|做完|如果|假如|要是|万一|可能|梦到|故事|虚构|想象|以前|过去')
_QUERY_VETO=re.compile(r'不要提|别提|不想提|不要说|别说|别提醒|不用提醒|不必提醒|无需提醒|不需要提醒|不想聊|保密|隐私')
_DISTRESS=re.compile(r'难过|伤心|崩溃|焦虑|悲伤|心情不好|发蔫|不想说|没力气|累坏了|好累')
_DIRECT_PLAN=re.compile(r'提醒我|今天.{0,8}(?:安排|计划|预约|待办|行程)|(?:安排|计划|预约|待办|行程).{0,8}今天')
_SENSITIVE=re.compile(r'医院|医生|病历|复诊|手术|治疗|心理|工资|债务|财务|密码|住址|私事|秘密|隐私')

def due_candidates(documents, references, messages, query, cutoff, timezone, *, max_scan=512, limit=2):
    """Return at most two document IDs with a matching *date reference*.

    A date match does not prove that a plan still applies. Model output must
    receive the original with that uncertainty and no automatic notification.
    """
    trace={'enabled':True,'scanned':0,'candidate_count':0,'scan_limit':max_scan,
           'meaning':'source_date_reference_not_verified_due_event'}
    if _QUERY_VETO.search(query):
        trace['suppressed']='explicit_query_boundary';return [],trace
    # Suppress speculative, unsolicited date leads when the current message
    # signals distress. An explicit request for plans may still be answered;
    # the owner's negative boundary above always takes precedence.
    if _DISTRESS.search(query) and not _DIRECT_PLAN.search(query):
        trace['suppressed']='emotional_context';return [],trace
    # Only normal, active raw records. Restricted originals are never supplied
    # by this unsolicited route even if another exact request would permit one.
    raw=[(k,d) for k,d in documents.items() if d.get('kind')=='raw'
         and d.get('status','active')=='active' and d.get('disclosure','normal')=='normal'
         and 0 <= cutoff-float(d.get('recorded_at',0)) <= 90*86400]
    raw.sort(key=lambda item:(-float(item[1].get('recorded_at',0)),item[0]))
    selected=[];seen=set()
    for key,doc in raw[:max_scan]:
        trace['scanned']+=1
        for ref in references.get(key,[]):
            mid=ref['message_id'];source=messages.get(mid)
            if mid in seen or not source or ref.get('role')!='user' or not ref.get('is_original_utterance'):continue
            if (source.get('metadata') or {}).get('disclosure','normal')!='normal':continue
            text=ref['text']
            # Normal disclosure is not proof a message is non-sensitive.
            # This conservative local filter reduces unsolicited exposure;
            # it is intentionally not represented as a complete privacy test.
            if len(text)>500 or _SENSITIVE.search(text):continue
            for match in source_times(source,ref['start'],ref['end'],timezone=timezone,limit=6):
                if match.get('status')!='resolved' or match.get('precision')!='day':continue
                # Inspect the sentence containing the cited date rather than
                # treating an unrelated negation elsewhere in a message as a
                # cancellation. Still keep the complete original as evidence.
                absolute=match['expression_start']; original=source['text']
                left=max([ref['start']]+[original.rfind(c,ref['start'],absolute)+1 for c in '。！？；\n'])
                ends=[pos for c in '。！？；\n' if (pos:=original.find(c,absolute,ref['end']))>=0]
                right=min(ends) if ends else ref['end']
                sentence=original[left:right]
                if not _PLAN.search(sentence) or _UNSAFE.search(sentence) or '?' in sentence:continue
                if right<ref['end'] and original[right] in '？?':continue
                try:
                    start=dt.datetime.fromisoformat(match['start']).timestamp();end=dt.datetime.fromisoformat(match['end']).timestamp()
                except (ValueError,TypeError,OverflowError):continue
                if start<=cutoff<end and float(source['recorded_at'])<start:
                    selected.append(key);seen.add(mid);break
            if len(selected)>=limit:break
        if len(selected)>=limit:break
    trace['candidate_count']=len(selected)
    return selected,trace
