"""Deterministic source-bound date references, never assertions of occurrence.

Only a finite, public date grammar is supported. No model, system-clock lookup,
database writes, question-specific rules, or semantic guesses are used here.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
from zoneinfo import ZoneInfo

UTC=dt.timezone.utc
_DAY_WORDS={'今天':0,'今日':0,'昨天':-1,'昨日':-1,'前天':-2,'明天':1,'明日':1,'后天':2,
            'today':0,'yesterday':-1,'the day before yesterday':-2,'tomorrow':1,'the day after tomorrow':2}
_WEEKDAYS={name:index for index,names in enumerate((('monday','mon'),('tuesday','tue','tues'),
    ('wednesday','wed'),('thursday','thu','thur','thurs'),('friday','fri'),('saturday','sat'),('sunday','sun')))
    for name in names}
_CN_DAYS={'一':0,'二':1,'三':2,'四':3,'五':4,'六':5,'日':6,'天':6,**{str(i):i-1 for i in range(1,8)}}
_MONTHS={name:index for index,names in enumerate((('january','jan'),('february','feb'),('march','mar'),
    ('april','apr'),('may',),('june','jun'),('july','jul'),('august','aug'),('september','sep','sept'),
    ('october','oct'),('november','nov'),('december','dec')),1) for name in names}
_WD='(?:'+ '|'.join(sorted(_WEEKDAYS,key=len,reverse=True))+')'
_MON='(?:'+'|'.join(sorted(_MONTHS,key=len,reverse=True))+')'
_NUM=r'[0-9零〇一二两三四五六七八九十百千]+'
_CN_WEEK=r'(?:上|本|这|下)(?:周|星期|礼拜)(?:(?:周|星期|礼拜)?[一二三四五六日天1-7])?'
_EN_WEEK=rf'(?:(?:last|this|next)\s+week(?:\s+{_WD})?|{_WD}\s+(?:of\s+)?(?:last|this|next)\s+week|(?:last|this|next)\s+{_WD})'
_EN_REL=r'(?:in\s+[0-9]+\s+(?:days?|weeks?)|[0-9]+\s+(?:days?|weeks?)\s+(?:ago|later|from\s+now|before|after))'
_CN_EVENT_REL=rf'[\u3400-\u9fff]{{1,16}}[前后]{_NUM}(?:个)?(?:天|日|周|星期)'
_EN_DATE=rf'(?:{_MON}\s+[0-9]{{1,2}}(?:,?\s+[0-9]{{4}})?|[0-9]{{1,2}}\s+{_MON}(?:\s+[0-9]{{4}})?)'
_SCAN=re.compile('|'.join((
    r'(?<![0-9A-Za-z_-])[0-9]{4}-[0-9]{2}-[0-9]{2}(?![0-9A-Za-z_-])',
    r'(?<![0-9])[0-9]{4}年[0-9]{1,2}月[0-9]{1,2}[日号](?![0-9])',
    rf'(?<![A-Za-z0-9]){_EN_DATE}(?![A-Za-z0-9])',
    rf'{_CN_WEEK}(?![一二三四五六日天0-9])',
    rf'(?<![A-Za-z0-9]){_EN_WEEK}(?![A-Za-z0-9])',
    _CN_EVENT_REL,
    rf'(?<![0-9零〇一二两三四五六七八九十百千]){_NUM}(?:个)?(?:天|日|周|星期)(?:以)?[前后]',
    rf'(?<![A-Za-z0-9]){_EN_REL}(?![A-Za-z0-9])',
    r'今天|今日|昨天|昨日|前天|明天|明日|后天',
    r'(?<![A-Za-z])(?:the\s+day\s+before\s+yesterday|the\s+day\s+after\s+tomorrow|today|yesterday|tomorrow)(?![A-Za-z])',
    r'(?<![0-9年])[0-9]{1,2}月[0-9]{1,2}[日号]?(?![0-9])',
    r'(?:本月|上月|下月|[0-9]{1,2}月)?[上中下]旬|改天',
    r'(?<![0-9A-Za-z/])[0-9]{1,2}/[0-9]{1,2}(?![0-9A-Za-z/])',
)),re.IGNORECASE)


def _result(expression,timezone,anchor_id=None):
    return {'schema_version':1,'expression':expression if isinstance(expression,str) else None,
        'anchor_message_id':anchor_id,'anchor_recorded_at_iso':None,'timezone':timezone,
        'start':None,'end':None,'precision':None,'status':'unsupported','reason':'unsupported_expression',
        'source_ref':None,'semantics':'date_reference_only_not_occurrence'}


def _unresolved(result,reason,status='unsupported'):
    return dict(result,start=None,end=None,precision=None,status=status,reason=reason)


def _source(message,start=None,end=None):
    if not isinstance(message,dict) or type(message.get('id')) is not int or message['id']<=0 or not isinstance(message.get('text'),str):
        return None
    start=0 if start is None else start;end=len(message['text']) if end is None else end
    if type(start) is not int or type(end) is not int or not 0<=start<end<=len(message['text']):return None
    return message,start,end


def _message_metadata(message):
    metadata=message.get('metadata') or {}
    if isinstance(metadata,str):
        try:metadata=json.loads(metadata)
        except (ValueError,TypeError):metadata={}
    return metadata if isinstance(metadata,dict) else {}


def _anchor_time(message,zone):
    value=message.get('recorded_at')
    if type(value) not in (int,float) or not math.isfinite(value):return None
    try:instant=dt.datetime.fromtimestamp(value,UTC)
    except (ValueError,OverflowError,OSError):return None
    original=_message_metadata(message).get('recorded_at_iso')
    if isinstance(original,str):
        try:
            parsed=dt.datetime.fromisoformat(original.replace('Z','+00:00'))
            if parsed.tzinfo is not None and abs(parsed.timestamp()-value)<.001:return instant.astimezone(zone),parsed.isoformat()
        except (ValueError,OverflowError,OSError):pass
    local=instant.astimezone(zone)
    return local,local.isoformat()


def _integer(text):
    if re.fullmatch(r'[0-9]+',text):return int(text) if len(text)<=6 else None
    digits={'零':0,'〇':0,'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
    if len(text)==1:return digits.get(text,10 if text=='十' else None)
    total,current,last=0,0,10000
    if not any(unit in text for unit in '十百千'):return None
    for char in text:
        if char in digits:
            if current and digits[char]:return None
            current=digits[char]
        elif char in '十百千':
            unit={'十':10,'百':100,'千':1000}[char]
            if unit>=last:return None
            total+=(current or 1)*unit;current=0;last=unit
        else:return None
    return total+current


def _date_reference(expression,anchor_date):
    """Return (date, calendar-day width, precision, reason), or an unresolved reason."""
    text=expression.strip();english=' '.join(text.lower().split())
    if text in _DAY_WORDS:return anchor_date+dt.timedelta(days=_DAY_WORDS[text]),1,'day','relative_day'
    if english in _DAY_WORDS:return anchor_date+dt.timedelta(days=_DAY_WORDS[english]),1,'day','relative_day'
    match=re.fullmatch(r'([0-9]{4})-([0-9]{2})-([0-9]{2})',text)
    if not match:match=re.fullmatch(r'([0-9]{4})年([0-9]{1,2})月([0-9]{1,2})[日号]',text)
    if match:return dt.date(*(int(value) for value in match.groups())),1,'day','absolute_date'
    match=re.fullmatch(rf'({_MON})\s+([0-9]{{1,2}}),?\s+([0-9]{{4}})',english)
    if match:return dt.date(int(match[3]),_MONTHS[match[1]],int(match[2])),1,'day','absolute_date'
    match=re.fullmatch(rf'([0-9]{{1,2}})\s+({_MON})\s+([0-9]{{4}})',english)
    if match:return dt.date(int(match[3]),_MONTHS[match[2]],int(match[1])),1,'day','absolute_date'
    match=re.fullmatch(r'(上|本|这|下)(?:周|星期|礼拜)(?:(?:周|星期|礼拜)?([一二三四五六日天1-7]))?',text)
    direction=weekday=None
    if match:direction={'上':-1,'本':0,'这':0,'下':1}[match[1]];weekday=_CN_DAYS.get(match[2])
    else:
        match=re.fullmatch(rf'(last|this|next)\s+week(?:\s+({_WD}))?',english)
        if match:direction={'last':-1,'this':0,'next':1}[match[1]];weekday=_WEEKDAYS.get(match[2])
        else:
            match=re.fullmatch(rf'({_WD})\s+(?:of\s+)?(last|this|next)\s+week',english)
            if match:direction={'last':-1,'this':0,'next':1}[match[2]];weekday=_WEEKDAYS[match[1]]
            else:
                match=re.fullmatch(rf'(last|this|next)\s+({_WD})',english)
                if match:
                    if match[1]!='this':return None,'ambiguous','ambiguous_weekday_convention'
                    direction=0;weekday=_WEEKDAYS[match[2]]
    if direction is not None:
        monday=anchor_date-dt.timedelta(days=anchor_date.weekday())+dt.timedelta(weeks=direction)
        return (monday+dt.timedelta(days=weekday),1,'day','calendar_weekday') if weekday is not None else (monday,7,'week','calendar_week')
    match=re.fullmatch(rf'({_NUM})(?:个)?(天|日|周|星期)(?:以)?(前|后)',text)
    if match:
        count=_integer(match[1])
        if count is None:return None,'unsupported','invalid_relative_number'
        days=count*(7 if match[2] in ('周','星期') else 1)*(-1 if match[3]=='前' else 1)
        return anchor_date+dt.timedelta(days=days),1,'day','relative_calendar_offset'
    match=re.fullmatch(r'in\s+([0-9]+)\s+(days?|weeks?)',english)
    if match:count=int(match[1]);unit=match[2];direction=1
    else:
        match=re.fullmatch(r'([0-9]+)\s+(days?|weeks?)\s+(ago|later|from\s+now|before|after)',english)
        if match:
            if match[3] in ('before','after','later'):return None,'ambiguous','external_event_anchor_required'
            count=int(match[1]);unit=match[2];direction=-1 if match[3]=='ago' else 1
    if match:return anchor_date+dt.timedelta(days=count*(7 if unit.startswith('week') else 1)*direction),1,'day','relative_calendar_offset'
    if re.fullmatch(_CN_EVENT_REL,text):return None,'ambiguous','external_event_anchor_required'
    if re.search(r'[上中下]旬|改天',text):return None,'ambiguous','vague_date_expression'
    if re.fullmatch(r'[0-9]{1,2}月[0-9]{1,2}[日号]?',text) or re.fullmatch(r'[0-9]{1,2}/[0-9]{1,2}',text):return None,'ambiguous','missing_year_or_date_order'
    if re.fullmatch(rf'{_MON}\s+[0-9]{{1,2}}|[0-9]{{1,2}}\s+{_MON}',english):return None,'ambiguous','missing_year'
    if re.fullmatch(r'(?:周|星期|礼拜)[一二三四五六日天1-7]',text) or english in _WEEKDAYS:return None,'ambiguous','missing_week_reference'
    return None,'unsupported','unsupported_expression'


def normalize_time(expression,anchor_message,timezone='UTC',*,source_start=None,source_end=None):
    result=_result(expression,timezone,anchor_message.get('id') if isinstance(anchor_message,dict) else None)
    if not isinstance(expression,str) or not expression.strip() or len(expression)>200:return _unresolved(result,'invalid_expression')
    checked=_source(anchor_message,source_start,source_end)
    if checked is None:return _unresolved(result,'invalid_source_span')
    message,start,end=checked
    if expression not in message['text'][start:end]:return _unresolved(result,'expression_not_in_source_span')
    result['source_ref']={'message_id':message['id'],'start':start,'end':end}
    try:zone=ZoneInfo(timezone)
    except (ValueError,TypeError,KeyError):return _unresolved(result,'invalid_timezone')
    anchor=_anchor_time(message,zone)
    if _message_metadata(message).get('source_kind')=='imported_memory':
        if anchor is not None:result['anchor_recorded_at_iso']=anchor[1]
        return _unresolved(result,'unverified_source_time')
    if anchor is None:return _unresolved(result,'invalid_anchor_time')
    local,original_iso=anchor;result['anchor_recorded_at_iso']=original_iso
    try:
        reference=_date_reference(expression,local.date())
        if reference[0] is None:return _unresolved(result,reference[2],reference[1])
        day,width,precision,reason=reference
        begin=dt.datetime.combine(day,dt.time(),zone)
        finish=dt.datetime.combine(day+dt.timedelta(days=width),dt.time(),zone)
        if any(boundary.astimezone(UTC).astimezone(zone).replace(tzinfo=None)!=boundary.replace(tzinfo=None) for boundary in (begin,finish)):
            return _unresolved(result,'nonexistent_local_date_boundary','ambiguous')
        return dict(result,start=begin.isoformat(),end=finish.isoformat(),precision=precision,status='resolved',reason=reason)
    except (ValueError,OverflowError):return _unresolved(result,'invalid_or_out_of_range_date')


def _proposal(value):
    if isinstance(value,str):return value,None
    if isinstance(value,dict):return value.get('expression'),value.get('anchor_message_id')
    return None,None


def time_metadata(metadata,checked_sources,timezone='UTC'):
    """Normalize only a proposed, literally quoted expression in checked sources."""
    if not isinstance(metadata,dict):return None
    nested=metadata.get('claim') if isinstance(metadata.get('claim'),dict) else {}
    if 'event_time' not in metadata and 'event_time' not in nested:return None
    flat=metadata.get('event_time') if 'event_time' in metadata else nested.get('event_time')
    expression,anchor_id=_proposal(flat)
    result=_result(expression,timezone,anchor_id)
    if 'event_time' in metadata and 'event_time' in nested:
        other_expression,other_anchor=_proposal(nested['event_time'])
        if other_expression!=expression or (anchor_id is not None and other_anchor is not None and anchor_id!=other_anchor):
            return _unresolved(result,'conflicting_time_proposals','ambiguous')
        if anchor_id is None and other_anchor is not None:anchor_id=other_anchor;result['anchor_message_id']=anchor_id
    if not isinstance(expression,str) or not expression.strip():return _unresolved(result,'invalid_time_proposal')
    if anchor_id is not None and (type(anchor_id) is not int or anchor_id<=0):return _unresolved(result,'invalid_anchor_message_id')
    candidates=[];invalid=False
    for raw in checked_sources if isinstance(checked_sources,(list,tuple)) else []:
        if not isinstance(raw,(tuple,list)) or len(raw)!=3:invalid=True;continue
        message,start,end=raw;source=_source(message,start,end)
        if source is None:invalid=True;continue
        if anchor_id is not None and message['id']!=anchor_id:continue
        if expression in message['text'][start:end]:candidates.append(source)
    if not candidates:return _unresolved(result,'invalid_checked_sources' if invalid else 'expression_or_anchor_not_in_cited_sources')
    anchors={item[0]['id'] for item in candidates}
    if len(anchors)>1:return _unresolved(result,'multiple_possible_anchors','ambiguous')
    # Same-message repeated/overlapping citations have the same temporal anchor;
    # retain one actual cited span rather than merging across uncited content.
    message,start,end=min(candidates,key=lambda item:(item[2]-item[1],item[1]))
    return normalize_time(expression,message,timezone,source_start=start,source_end=end)


def source_times(message,start,end,timezone='UTC',limit=3):
    """Scan only the cited span using the same bounded general date grammar."""
    checked=_source(message,start,end)
    if checked is None or type(limit) is not int or not 0<=limit<=20:return []
    text=message['text'][start:end];answer=[]
    for match in _SCAN.finditer(text):
        if len(answer)>=limit:break
        result=normalize_time(match.group(),message,timezone,source_start=start,source_end=end)
        result['expression_start']=start+match.start();result['expression_end']=start+match.end()
        answer.append(result)
    return answer
