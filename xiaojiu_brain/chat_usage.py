"""Metadata-only request ledger, source-key idempotence and weighted turn reports."""
from collections import defaultdict
from datetime import datetime,timedelta
from decimal import Decimal
import hashlib,json,math,time
from .chat_pricing import BJT,VERSION,amount,estimate,price_info

TOKENS=('input_tokens','output_tokens','cache_hit_tokens','cache_miss_tokens','total_tokens')
FIELDS=('turn_id','model','provider','started_at','ended_at','outcome',*TOKENS,
        'source_event_seq','normalization_basis','capture_mode')
ALLOWED={'conversation_id','request_id','source','captured_at',*FIELDS}


class UsageConflict(ValueError):
    pass


def _text(value,name,maximum=512,default=None):
    if value is None and default is not None:return default
    if not isinstance(value,str) or not value.strip() or len(value)>maximum or any(ord(c)<32 for c in value) or '://' in value:
        raise ValueError('invalid chat usage '+name)
    return value


def _stamp(value):
    if value is None:return None
    if isinstance(value,str):
        try:
            at=datetime.fromisoformat(value.replace('Z','+00:00'))
            if at.tzinfo is None:raise ValueError()
            value=at.timestamp()
        except (ValueError,OverflowError):raise ValueError('chat usage time requires a valid timezone') from None
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not 0<=value<=32503680000:
        raise ValueError('chat usage time must be finite epoch seconds')
    return round(float(value),6)


def _token(value,name):
    if value is None:return None
    if type(value) is not int or not 0<=value<=1_000_000_000_000:
        raise ValueError('invalid chat usage '+name)
    return value


def _reconcile(row):
    i,h,m,o,t=(row.get(k) for k in ('input_tokens','cache_hit_tokens','cache_miss_tokens','output_tokens','total_tokens'))
    if i is None and t is not None and o is not None:
        i=t-o
    if i is None and h is not None and m is not None:i=h+m
    if i is not None:
        if i<0:raise ValueError('chat usage totals disagree')
        if h is not None and m is None:m=i-h
        if m is not None and h is None:h=i-m
    if h is not None and h<0 or m is not None and m<0:raise ValueError('chat cache usage exceeds total input')
    if all(v is not None for v in (i,h,m)) and h+m!=i:raise ValueError('chat cache usage totals disagree')
    if t is None and i is not None and o is not None:t=i+o
    if all(v is not None for v in (i,o,t)) and i+o!=t:raise ValueError('chat total usage disagrees')
    row.update(input_tokens=i,cache_hit_tokens=h,cache_miss_tokens=m,total_tokens=t)
    if row.get('started_at') is not None and row.get('ended_at') is not None and row['started_at']>row['ended_at']:
        raise ValueError('chat request ends before it starts')
    return row


def canonical(body):
    if not isinstance(body,dict) or set(body)-ALLOWED:raise ValueError('unsupported chat usage fields')
    conv=_text(body.get('conversation_id'),'conversation_id')
    ident=_text(body.get('request_id'),'request_id')
    if body.get('source','dsh_session_event')!='dsh_session_event':raise ValueError('unsupported chat usage source')
    row={'conversation_id':conv,'request_id':ident,
        'turn_id':_text(body['turn_id'],'turn_id',256) if body.get('turn_id') is not None else None,
        'model':_text(body.get('model'),'model',256,default='unknown'),
        'provider':_text(body.get('provider'),'provider',256,default='unknown'),
        'started_at':_stamp(body.get('started_at')),'ended_at':_stamp(body.get('ended_at')),
        'outcome':body.get('outcome','unknown'),'source_event_seq':body.get('source_event_seq'),
        'normalization_basis':body.get('normalization_basis','partial_unknown'),
        'capture_mode':body.get('capture_mode','live')}
    if row['outcome'] not in ('success','error','cancelled','unknown'):raise ValueError('invalid chat usage outcome')
    if row['normalization_basis'] not in ('reported_total_minus_output','disjoint_sum','partial_unknown'):
        raise ValueError('invalid chat usage normalization basis')
    if row['capture_mode'] not in ('live','history'):raise ValueError('invalid chat usage capture mode')
    if row['source_event_seq'] is not None and (type(row['source_event_seq']) is not int or not 0<row['source_event_seq']<=2**53-1):
        raise ValueError('invalid chat source event sequence')
    if body.get('captured_at') is not None:_stamp(body['captured_at'])
    row.update({key:_token(body.get(key),key) for key in TOKENS})
    return _reconcile(row)


def _sum_known(rows,key):
    values=[r[key] for r in rows if r.get(key) is not None]
    return sum(values) if values else None


def aggregate(rows):
    result={key:_sum_known(rows,key) for key in TOKENS}
    count=len(rows);cached=[r for r in rows if r.get('cache_hit_tokens') is not None and r.get('cache_miss_tokens') is not None]
    denominator=sum(r['cache_hit_tokens']+r['cache_miss_tokens'] for r in cached)
    ratio=sum(r['cache_hit_tokens'] for r in cached)/denominator if denominator else None
    priced=[r for r in rows if r.get('cost_low_cny') is not None]
    exact=[r for r in rows if r.get('estimated_cost_cny') is not None]
    known=sum((Decimal(r['estimated_cost_cny']) for r in exact),Decimal(0)) if exact else None
    low=sum((Decimal(r['cost_low_cny']) for r in priced),Decimal(0)) if priced else None
    high=sum((Decimal(r['cost_high_cny']) for r in priced),Decimal(0)) if priced and len(priced)==count else None
    periods={r.get('price_period') for r in rows}
    period=next(iter(periods)) if len(periods)==1 else 'uncertain' if 'uncertain' in periods or not periods else 'mixed'
    result.update(request_count=count,turn_count=len({(r.get('conversation_id'),r.get('turn_id')) for r in rows}),
        cache_known_requests=len(cached),cache_hit_rate=ratio if count and len(cached)==count else None,
        reported_cache_hit_rate=ratio,known_field_requests={key:sum(r.get(key) is not None for r in rows) for key in TOKENS},
        unknown_usage_requests=sum(all(r.get(key) is None for key in TOKENS) for r in rows),
        partial_usage_requests=sum(any(r.get(key) is None for key in TOKENS) for r in rows),
        estimated_cost_cny=amount(known) if count and len(exact)==count else None,
        known_cost_cny=amount(known) if known is not None else None,
        cost_low_cny=amount(low) if low is not None else None,
        cost_high_cny=amount(high) if high is not None else None,
        unpriced_requests=count-len(priced),uncertain_price_requests=len(priced)-len(exact),price_period=period)
    return result


class ChatUsageLedger:
    def __init__(self,store):
        self.store=store
        with store.lock:
            store.conn.executescript("""
            CREATE TABLE IF NOT EXISTS chat_usage_calls_v1 (
              owner TEXT NOT NULL,conversation_id TEXT NOT NULL,request_id TEXT NOT NULL,
              turn_id TEXT NOT NULL,model TEXT NOT NULL,provider TEXT NOT NULL,
              started_at REAL,ended_at REAL,outcome TEXT NOT NULL,
              input_tokens INTEGER,output_tokens INTEGER,cache_hit_tokens INTEGER,
              cache_miss_tokens INTEGER,total_tokens INTEGER,source_event_seq INTEGER,
              normalization_basis TEXT NOT NULL,capture_mode TEXT NOT NULL,pricing_version TEXT NOT NULL,
              created_at REAL NOT NULL,updated_at REAL NOT NULL,
              PRIMARY KEY(owner,conversation_id,request_id));
            CREATE INDEX IF NOT EXISTS ix_chat_usage_scope_time_v1
              ON chat_usage_calls_v1(owner,ended_at,conversation_id,turn_id);
            """)

    def ingest(self,owner,body):
        owner=_text(owner,'owner',256);incoming=canonical(body);now=time.time()
        keys=('owner','conversation_id','request_id',*FIELDS,'pricing_version','created_at','updated_at')
        # conversation/request precede FIELDS, and FIELDS deliberately excludes them.
        with self.store._transaction():
            previous=self.store.conn.execute("""SELECT * FROM chat_usage_calls_v1
                WHERE owner=? AND conversation_id=? AND request_id=?""",
                (owner,incoming['conversation_id'],incoming['request_id'])).fetchone()
            enriched=False
            if previous:
                merged=dict(previous)
                for key in FIELDS:
                    old,new=merged[key],incoming[key]
                    if key in ('capture_mode','normalization_basis'):continue
                    unknown=old is None or key in ('model','provider','outcome') and old=='unknown' or key=='turn_id' and old.startswith('unassigned:')
                    if unknown and new is not None and new!='unknown':
                        if old!=new:merged[key]=new;enriched=True
                    elif new is not None and new!='unknown' and old!=new:
                        raise UsageConflict('stable chat request already has different accounting metadata')
                try:_reconcile(merged)
                except ValueError as exc:raise UsageConflict('additional chat usage disagrees with the saved request') from exc
                if enriched:
                    merged['updated_at']=now
                    self.store.conn.execute('UPDATE chat_usage_calls_v1 SET '+','.join(k+'=?' for k in FIELDS)+
                        ',updated_at=? WHERE owner=? AND conversation_id=? AND request_id=?',
                        (*[merged[k] for k in FIELDS],now,owner,incoming['conversation_id'],incoming['request_id']))
                saved=merged
            else:
                saved={**incoming,'owner':owner,'pricing_version':VERSION,'created_at':now,'updated_at':now}
                if saved['turn_id'] is None:
                    saved['turn_id']='unassigned:'+hashlib.sha256(saved['request_id'].encode()).hexdigest()[:24]
                self.store.conn.execute('INSERT INTO chat_usage_calls_v1('+','.join(keys)+') VALUES('+','.join('?' for _ in keys)+')',
                    [saved[key] for key in keys])
        usage={key:saved[key] for key in TOKENS}
        return {'accepted':not previous or enriched,'duplicate':bool(previous) and not enriched,'enriched':enriched,
            'conversation_id':saved['conversation_id'],'request_id':saved['request_id'],
            'source_event_seq':saved['source_event_seq'],'usage':usage}

    def report(self,owner,*,period='today',conversation_id=None,offset=0,limit=30,now=None):
        owner=_text(owner,'owner',256)
        if period not in ('today','month','all'):raise ValueError('invalid chat usage period')
        if type(offset) is not int or not 0<=offset<=1_000_000 or type(limit) is not int or not 1<=limit<=100:
            raise ValueError('invalid chat usage pagination')
        if conversation_id is not None:conversation_id=_text(conversation_id,'conversation_id')
        at=datetime.fromtimestamp(time.time() if now is None else now,BJT)
        begin=at.replace(hour=0,minute=0,second=0,microsecond=0)
        if period=='month':begin=begin.replace(day=1)
        clauses=['owner=?'];params=[owner]
        if period!='all':
            clauses+=['COALESCE(ended_at,started_at,created_at)>=?','COALESCE(ended_at,started_at,created_at)<=?']
            params.extend((begin.timestamp(),at.timestamp()))
        if conversation_id is not None:clauses.append('conversation_id=?');params.append(conversation_id)
        with self.store.lock:
            records=[dict(r) for r in self.store.conn.execute('SELECT * FROM chat_usage_calls_v1 WHERE '+
                ' AND '.join(clauses)+' ORDER BY COALESCE(ended_at,started_at,created_at) DESC,request_id',params)]
            cap=self.store.conn.execute("""SELECT min(created_at),max(created_at),
                min(COALESCE(ended_at,started_at)),max(COALESCE(ended_at,started_at)),count(*),
                sum(capture_mode='live'),sum(capture_mode='history') FROM chat_usage_calls_v1 WHERE owner=?""",(owner,)).fetchone()
        rows=[]
        for record in records:
            row={key:value for key,value in record.items() if key not in ('owner','updated_at')}
            row.update(estimate(row))
            denominator=(row['cache_hit_tokens'] or 0)+(row['cache_miss_tokens'] or 0)
            row['cache_hit_rate']=row['cache_hit_tokens']/denominator if denominator and row['cache_hit_tokens'] is not None and row['cache_miss_tokens'] is not None else None
            rows.append(row)
        grouped=defaultdict(list)
        for row in rows:grouped[(row['conversation_id'],row['turn_id'])].append(row)
        groups=list(grouped.items());turns=[]
        for (conversation,turn),items in groups[offset:offset+limit]:
            chronological=sorted(items,key=lambda r:(r.get('ended_at') or r.get('started_at') or r['created_at'],r['request_id']))
            start=[r['started_at'] for r in items if r['started_at'] is not None]
            end=[r['ended_at'] for r in items if r['ended_at'] is not None]
            turns.append({**aggregate(items),'conversation_id':conversation,'turn_id':turn,
                'started_at':min(start) if start else None,'ended_at':max(end) if end else None,
                'models':sorted({r['model'] for r in items}),'requests_total':len(items),
                'requests_shown':min(200,len(items)),'requests':chronological[:200]})
        return {'summary':aggregate(rows),'turns':turns,'price_info':price_info(),
            'filters':{'period':period,'conversation_id':conversation_id,'timezone':'Asia/Shanghai'},
            'pagination':{'offset':offset,'limit':limit,'total_turns':len(groups),'has_more':offset+limit<len(groups)},
            'capture_info':{'first_recorded_at':cap[0],'last_recorded_at':cap[1],'earliest_request_at':cap[2],
                'latest_request_at':cap[3],'record_count':cap[4],'live_records':cap[5] or 0,'history_records':cap[6] or 0,
                'hidden_retries_unknown':True,
                'coverage_note':'只含已采集的DSH结算事件；历史在会话恢复或显式导入后补采；底层SDK未暴露的重试和缺失usage不代表免费。'}}
