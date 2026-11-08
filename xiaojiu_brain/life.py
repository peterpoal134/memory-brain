"""Owner-scoped, local life state. No model, network, message sending or raw ingest.

Routine activity is explicitly simulated. Book events are literal program
excerpts, not a claim that a model read, understood or experienced anything.
"""
from __future__ import annotations

from contextlib import nullcontext
import datetime as dt
import hashlib
import json
import math
import re
import threading
import time
import uuid
from zoneinfo import ZoneInfo


class LifeLayer:
    def __init__(self, store, owner, timezone='UTC', settings=None, emotion_provider=None):
        self.store = store
        self.owner = self._identifier(owner, 'owner', 256)
        self.timezone = str(timezone)
        try:self.zone = ZoneInfo(self.timezone)
        except Exception:raise ValueError('invalid life timezone') from None
        self.settings = settings if callable(settings) else lambda: {}
        self.emotion_provider = emotion_provider if callable(emotion_provider) else None
        self.error = None
        self._stop = threading.Event()
        self._thread = None
        self._lifecycle_lock = threading.Lock()
        with self.store.lock:
            self.store.conn.executescript("""
            CREATE TABLE IF NOT EXISTS life_books (
              id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL,
              text TEXT NOT NULL, cursor INTEGER NOT NULL DEFAULT 0,
              created_at REAL NOT NULL, updated_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS ix_life_books_owner ON life_books(owner,created_at,id);
            CREATE TABLE IF NOT EXISTS life_events (
              id TEXT PRIMARY KEY, owner TEXT NOT NULL, conversation_id TEXT NOT NULL,
              event_type TEXT NOT NULL, text TEXT NOT NULL, importance REAL NOT NULL,
              occurred_at REAL NOT NULL, source_json TEXT NOT NULL,
              idempotency_key TEXT, book_id TEXT REFERENCES life_books(id) ON DELETE CASCADE,
              UNIQUE(owner,idempotency_key));
            CREATE INDEX IF NOT EXISTS ix_life_events_owner ON life_events(owner,conversation_id,occurred_at);
            CREATE TABLE IF NOT EXISTS life_event_sources (
              event_id TEXT NOT NULL REFERENCES life_events(id) ON DELETE CASCADE,
              message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
              PRIMARY KEY(event_id,message_id));
            CREATE TABLE IF NOT EXISTS life_tasks (
              id TEXT PRIMARY KEY, owner TEXT NOT NULL, conversation_id TEXT NOT NULL,
              title TEXT NOT NULL, due_at REAL, status TEXT NOT NULL,
              created_at REAL NOT NULL, updated_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS ix_life_tasks_owner ON life_tasks(owner,conversation_id,status,due_at);
            CREATE TABLE IF NOT EXISTS life_runtime (
              owner TEXT PRIMARY KEY, last_tick_at REAL,
              current_event_id TEXT REFERENCES life_events(id) ON DELETE SET NULL,
              error TEXT, started_at REAL);
            """)
            if 'started_at' not in {row['name'] for row in self.store.conn.execute('PRAGMA table_info(life_runtime)')}:
                self.store.conn.execute('ALTER TABLE life_runtime ADD COLUMN started_at REAL')
            # Upgrade existing state once, without resetting its clock on restart.
            self.store.conn.execute("""UPDATE life_runtime SET started_at=coalesce(
                (SELECT min(e.occurred_at) FROM life_events e WHERE e.owner=life_runtime.owner
                   AND e.event_type IN ('routine_simulation','book_excerpt')),
                last_tick_at) WHERE started_at IS NULL""")

    @staticmethod
    def _identifier(value, name='id', maximum=512):
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError('invalid life '+name)
        return value.strip()

    @staticmethod
    def _text(value, maximum, name='text'):
        if not isinstance(value, str) or not value.strip() or '\x00' in value or len(value) > maximum:
            raise ValueError('invalid life '+name)
        return value

    @staticmethod
    def _importance(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError('invalid life importance')
        return float(value)

    def _stamp(self, value=None):
        if value is None:return time.time()
        if isinstance(value, bool):raise ValueError('invalid life timestamp')
        if isinstance(value, (int, float)):
            if not math.isfinite(value):raise ValueError('invalid life timestamp')
            stamp = float(value)
        else:
            try:
                parsed = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
                if parsed.tzinfo is None:parsed = parsed.replace(tzinfo=self.zone)
                stamp = parsed.timestamp()
            except Exception:raise ValueError('invalid life timestamp') from None
        try:dt.datetime.fromtimestamp(stamp, self.zone)
        except Exception:raise ValueError('invalid life timestamp') from None
        return stamp

    def _settings(self):
        raw = self.settings() or {}
        def clock(key, default):
            value = raw.get(key, default)
            return value if isinstance(value, str) and re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value) else default
        interval = raw.get('life_interval_seconds', 1200)
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval):interval = 1200
        return {'life_enabled':raw.get('life_enabled', False) is True,
                'life_context_enabled':raw.get('life_context_enabled', True) is True,
                'emotion_enabled':raw.get('emotion_enabled', False) is True,
                'life_interval_seconds':max(300, min(86400, int(interval))),
                'life_wake_time':clock('life_wake_time', '08:00'),
                'life_sleep_time':clock('life_sleep_time', '23:30')}

    def _rhythm(self, stamp, settings):
        local = dt.datetime.fromtimestamp(stamp, self.zone)
        minute = local.hour*60+local.minute
        wake = sum(int(v)*m for v,m in zip(settings['life_wake_time'].split(':'), (60,1)))
        sleep = sum(int(v)*m for v,m in zip(settings['life_sleep_time'].split(':'), (60,1)))
        awake = wake <= minute < sleep if wake < sleep else (minute >= wake or minute < sleep)
        return {'phase':'awake' if awake else 'rest', 'kind':'routine_simulation',
                'simulated':True,'wake_time':settings['life_wake_time'],'sleep_time':settings['life_sleep_time']}

    def rules(self):
        """Public deterministic product rules; never psychological measurements."""
        settings=self._settings()
        return {'schema_version':1,'model':'bounded_rhythm_v2','kind':'rule_simulation',
            'engineering_note':'透明的产品启发式规则，非临床或心理学测量，不代表真实感受；不读取对话含义。',
            'timezone':self.timezone,'wake_time':settings['life_wake_time'],'sleep_time':settings['life_sleep_time'],
            'labels':{'energy':'精力（模拟）','boredom':'无聊（模拟）','social':'社交需求（模拟）',
                      'curiosity':'好奇/兴趣激活（模拟）'},
            'rhythm':{'energy_awake':[.85,.35],'energy_rest':[.35,.85],
                      'energy_curve':'half_cosine','day_factor':'awake:+sin²(πp);rest:-sin²(πp)',
                      'base':{'boredom':[.30,.15],'social':[.28,.12],'curiosity':[.40,.10]}},
            'activities':{
                'chat':{'half_life_seconds':7200,'half_life_hours':2,'bucket_seconds':600,
                        'impulse':{'energy':-.015,'boredom':-.08,'social':-.10,'curiosity':.02}},
                'book':{'half_life_seconds':10800,'half_life_hours':3,'bucket_seconds':1800,
                        'impulse':{'energy':-.025,'boredom':-.12,'social':0,'curiosity':.06}},
                'task':{'half_life_seconds':7200,'half_life_hours':2,'bucket_seconds':None,
                        'impulse':{'energy':.03,'boredom':-.06,'social':0,'curiosity':.025}}},
            'activity_curve':'impulse × 2^(-elapsed_seconds/half_life_seconds)',
            'correction_caps':{'energy':.12,'boredom':.25,'social':.25,'curiosity':.25},
            'lookback_seconds':172800,'range':[0,1],
            'sources':'原始用户聊天、实际本地书摘和首次待办完成；桶内取最早有效活动，未来/启用前活动不计。'}

    def _needs_projection(self, stamp, settings, started_at, *, connection=None):
        """Read-only circadian bases plus bounded, source-linked impulses."""
        connection = self.store.conn if connection is None else connection
        local = dt.datetime.fromtimestamp(stamp,self.zone)
        minute = local.hour*60+local.minute+local.second/60+local.microsecond/60000000
        wake = sum(int(v)*m for v,m in zip(settings['life_wake_time'].split(':'),(60,1)))
        sleep = sum(int(v)*m for v,m in zip(settings['life_sleep_time'].split(':'),(60,1)))
        awake_minutes = (sleep-wake) % 1440 or 1440
        awake_elapsed = (minute-wake) % 1440
        if awake_elapsed < awake_minutes:
            progress=awake_elapsed/awake_minutes
            energy=.85-.5*(1-math.cos(math.pi*progress))/2
            day_factor=math.sin(math.pi*progress)**2
        else:
            rest_minutes = 1440-awake_minutes
            progress=((minute-sleep) % 1440)/max(1,rest_minutes)
            energy=.35+.5*(1-math.cos(math.pi*progress))/2
            day_factor=-math.sin(math.pi*progress)**2
        base={'energy':energy,'boredom':.30+.15*day_factor,
              'social':.28+.12*day_factor,'curiosity':.40+.10*day_factor}
        baseline=min(stamp,started_at) if started_at is not None else stamp
        since=max(baseline,stamp-172800)
        chat_rows=connection.execute("""SELECT DISTINCT m.id,m.recorded_at FROM life_event_sources s
            JOIN life_events e ON e.id=s.event_id JOIN messages m ON m.id=s.message_id
            WHERE e.owner=? AND e.event_type='chat_observed' AND m.owner=e.owner
              AND m.conversation_id=e.conversation_id AND m.role='user'
              AND (json_type(m.metadata,'$.source_kind') IS NULL
                   OR json_extract(m.metadata,'$.source_kind') IN ('user','conversation'))
              AND m.recorded_at>=? AND m.recorded_at<=?""",(self.owner,since,stamp)).fetchall()
        book_rows=connection.execute("""SELECT e.occurred_at FROM life_events e
            JOIN life_books b ON b.id=e.book_id AND b.owner=e.owner
            WHERE e.owner=? AND e.event_type='book_excerpt' AND e.occurred_at>=? AND e.occurred_at<=?""",
            (self.owner,since,stamp)).fetchall()
        task_rows=connection.execute("""SELECT e.occurred_at FROM life_events e
            JOIN life_tasks t ON t.id=json_extract(e.source_json,'$.task_id') AND t.owner=e.owner
            WHERE e.owner=? AND e.event_type='task_completed' AND e.occurred_at>=? AND e.occurred_at<=?""",
            (self.owner,since,stamp)).fetchall()
        def bucketed(values,seconds):
            buckets={}
            for value in values:
                bucket=math.floor(value/seconds)
                buckets[bucket]=min(buckets.get(bucket,value),value)
            return sorted(buckets.values())
        times={'chat':bucketed([r['recorded_at'] for r in chat_rows],600),
               'book':bucketed([r['occurred_at'] for r in book_rows],1800),
               'task':[r['occurred_at'] for r in task_rows]}
        rules=self.rules()
        correction={key:0.0 for key in base}
        for kind,values in times.items():
            activity=rules['activities'][kind]
            for at in values:
                decay=math.pow(.5,max(0,stamp-at)/activity['half_life_seconds'])
                for key,impulse in activity['impulse'].items():correction[key]+=impulse*decay
        for key,cap in rules['correction_caps'].items():correction[key]=max(-cap,min(cap,correction[key]))
        def bounded(value):return round(max(0,min(1,value)),4)
        final={key:bounded(value+correction[key]) for key,value in base.items()}
        return final,{'model':'bounded_rhythm_v2','base':{key:round(value,6) for key,value in base.items()},
            'activity_correction':{key:round(value,6) for key,value in correction.items()},'final':dict(final),
            'dimensions':{key:{'base':round(base[key],6),'activity_correction':round(correction[key],6),
                               'value':final[key]} for key in final},
            'eligible_activity_counts':{key:len(values) for key,values in times.items()},
            'lookback_seconds':172800,'rules_summary':'作息中性基线 + 有来源活动的指数恢复；总修正封顶；不是感受或事实证据。'}

    def _needs(self, stamp, settings, started_at, *, connection=None):
        return self._needs_projection(stamp,settings,started_at,connection=connection)[0]

    def _event(self, row):
        if row is None:return None
        event = dict(row)
        source = json.loads(event.pop('source_json'))
        event.pop('owner', None)
        event.pop('idempotency_key', None)
        event.pop('book_id', None)
        event['type'] = event.pop('event_type')
        event['created_at'] = event['occurred_at']
        if event['type'] == 'chat_observed':
            ids = [r[0] for r in self.store.conn.execute("""SELECT s.message_id FROM life_event_sources s
                 JOIN messages m ON m.id=s.message_id
                 WHERE s.event_id=? AND m.owner=? AND m.conversation_id=? ORDER BY s.message_id""",
                 (event['id'], self.owner, event['conversation_id']))]
            if not ids:return None
            source.update(message_ids=ids, count=len(ids))
            event['source_message_ids'] = ids
            event['source_count'] = len(ids)
        event['source'] = source
        return event

    def _get_event(self, ident):
        return self._event(self.store.conn.execute('SELECT * FROM life_events WHERE id=? AND owner=?',
                                                  (ident, self.owner)).fetchone())

    def _insert_event(self, conversation, kind, text, importance, stamp, source, key=None, book_id=None):
        ident = uuid.uuid4().hex
        self.store.conn.execute('INSERT INTO life_events VALUES(?,?,?,?,?,?,?,?,?,?)',
            (ident,self.owner,conversation,kind,text,importance,stamp,
             json.dumps(source, ensure_ascii=False, allow_nan=False),key,book_id))
        return ident

    def snapshot(self, now=None):
        stamp = self._stamp(now)
        settings = self._settings()
        with self.store.lock:
            row = self.store.conn.execute('SELECT * FROM life_runtime WHERE owner=?', (self.owner,)).fetchone()
            last = row['last_tick_at'] if row else None
            started_at = row['started_at'] if row else None
            current = self._get_event(row['current_event_id']) if row and row['current_event_id'] else None
            next_at = (last+settings['life_interval_seconds'] if last is not None else stamp) if settings['life_enabled'] else None
            count_events = self.store.conn.execute("""SELECT count(*) FROM life_events e WHERE e.owner=?
                AND (e.event_type!='chat_observed' OR EXISTS(SELECT 1 FROM life_event_sources s
                JOIN messages m ON m.id=s.message_id WHERE s.event_id=e.id AND m.owner=e.owner
                AND m.conversation_id=e.conversation_id))""", (self.owner,)).fetchone()[0]
            counts = {'events':count_events,
                'tasks':self.store.conn.execute('SELECT count(*) FROM life_tasks WHERE owner=?',(self.owner,)).fetchone()[0],
                'pending_tasks':self.store.conn.execute("SELECT count(*) FROM life_tasks WHERE owner=? AND status='pending'",(self.owner,)).fetchone()[0],
                'books':self.store.conn.execute('SELECT count(*) FROM life_books WHERE owner=?',(self.owner,)).fetchone()[0]}
            rhythm = self._rhythm(stamp, settings)
            needs,needs_trace=self._needs_projection(stamp,settings,started_at)
            return {'enabled':settings['life_enabled'],'context_enabled':settings['life_context_enabled'],
                'timezone':self.timezone,'settings':settings,'rhythm':rhythm,'current':current,
                'started_at':started_at,'needs':needs,'needs_trace':needs_trace,
                'needs_kind':'rule_simulation','needs_model':'bounded_rhythm_v2',
                'last_tick_at':last,'next_tick_at':next_at,'error':self.error or (row['error'] if row else None),
                'counts':counts,'state':{'activity':rhythm['phase'],'simulated':True},
                'last_action':{'at':last,'kind':current['type']} if current else None,
                'next_action':{'at':next_at,'kind':'scheduled_tick'} if next_at is not None else None}

    def events(self, limit=50, conversation_id=None):
        maximum = max(0, min(1000, int(limit)))
        where, args = 'e.owner=?', [self.owner]
        if conversation_id is not None:
            where += ' AND e.conversation_id=?';args.append(self._identifier(conversation_id,'conversation'))
        with self.store.lock:
            rows = self.store.conn.execute("""SELECT e.* FROM life_events e WHERE """+where+"""
                AND (e.event_type!='chat_observed' OR EXISTS(SELECT 1 FROM life_event_sources s
                JOIN messages m ON m.id=s.message_id WHERE s.event_id=e.id AND m.owner=e.owner
                AND m.conversation_id=e.conversation_id)) ORDER BY e.occurred_at DESC,e.id DESC LIMIT ?""",
                [*args,maximum]).fetchall()
            return {'items':[event for row in rows if (event := self._event(row)) is not None]}

    def add_event(self, data):
        if not isinstance(data, dict) or set(data)-{'text','conversation_id','importance','idempotency_key'}:
            raise ValueError('invalid life event fields')
        text = self._text(data.get('text'), 2000)
        conversation = self._identifier(data.get('conversation_id', 'manual'), 'conversation')
        importance = self._importance(data.get('importance', .5))
        key = data.get('idempotency_key')
        if key is not None:key = 'owner:'+self._identifier(key, 'idempotency key', 128)
        with self.store._transaction():
            old = self.store.conn.execute('SELECT * FROM life_events WHERE owner=? AND idempotency_key=?',
                                          (self.owner,key)).fetchone() if key else None
            if old:
                if (old['text'],old['conversation_id'],old['importance'],old['event_type']) != (text,conversation,importance,'owner_note'):
                    raise ValueError('life idempotency conflict')
                return self._event(old)
            ident = self._insert_event(conversation,'owner_note',text,importance,time.time(),
                                       {'kind':'owner_note','author':'owner'},key)
            return self._get_event(ident)

    def delete_event(self, ident):
        with self.store._transaction():
            cur = self.store.conn.execute('DELETE FROM life_events WHERE owner=? AND id=?',(self.owner,str(ident)))
            return {'id':str(ident),'deleted':bool(cur.rowcount)}

    def add_task(self, data):
        if not isinstance(data, dict) or set(data)-{'title','conversation_id','due_at','status'}:
            raise ValueError('invalid life task fields')
        title = self._text(data.get('title'),300,'task title')
        conversation = self._identifier(data.get('conversation_id','manual'),'conversation')
        status = data.get('status','pending')
        if status not in ('pending','done','cancelled'):raise ValueError('invalid life task status')
        due = self._stamp(data['due_at']) if data.get('due_at') is not None else None
        ident, stamp = uuid.uuid4().hex, time.time()
        with self.store._transaction():
            self.store.conn.execute('INSERT INTO life_tasks(id,owner,conversation_id,title,due_at,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                                     (ident,self.owner,conversation,title,due,status,stamp,stamp))
            return self._get_task(ident)

    def _get_task(self, ident):
        row = self.store.conn.execute('SELECT * FROM life_tasks WHERE owner=? AND id=?',(self.owner,str(ident))).fetchone()
        if row is None:raise KeyError('life task not found')
        result = dict(row);result.pop('owner',None);return result

    def tasks(self, limit=50):
        with self.store.lock:
            rows = self.store.conn.execute("""SELECT * FROM life_tasks WHERE owner=?
                ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END,coalesce(due_at,1e20),created_at,id LIMIT ?""",
                (self.owner,max(0,min(1000,int(limit))))).fetchall()
            return {'items':[{k:v for k,v in dict(row).items() if k!='owner'} for row in rows]}

    def update_task(self, ident, patch):
        if not isinstance(patch,dict) or set(patch)-{'status','title','due_at'}:raise ValueError('invalid life task patch')
        with self.store._transaction():
            task = self._get_task(ident)
            old_status=task['status']
            if 'title' in patch:task['title'] = self._text(patch['title'],300,'task title')
            if 'status' in patch:
                if patch['status'] not in ('pending','done','cancelled'):raise ValueError('invalid life task status')
                task['status'] = patch['status']
            if 'due_at' in patch:task['due_at'] = self._stamp(patch['due_at']) if patch['due_at'] is not None else None
            stamp=time.time()
            self.store.conn.execute('UPDATE life_tasks SET title=?,due_at=?,status=?,updated_at=? WHERE id=? AND owner=?',
                (task['title'],task['due_at'],task['status'],stamp,str(ident),self.owner))
            if old_status=='pending' and task['status']=='done':
                key='task-completed:'+str(ident)
                old=self.store.conn.execute('SELECT id FROM life_events WHERE owner=? AND idempotency_key=?',
                                             (self.owner,key)).fetchone()
                if old is None:
                    self._insert_event(task['conversation_id'],'task_completed',
                        '本地待办首次由未完成标记为完成（依据明确状态操作，不推断执行过程）。',.2,stamp,
                        {'kind':'task_completed','task_id':str(ident),'operation':'owner_status_update'},key)
            return self._get_task(ident)

    def delete_task(self, ident):
        with self.store._transaction():
            cur = self.store.conn.execute('DELETE FROM life_tasks WHERE owner=? AND id=?',(self.owner,str(ident)))
            if cur.rowcount:
                self.store.conn.execute("DELETE FROM life_events WHERE owner=? AND event_type='task_completed' AND idempotency_key=?",
                                        (self.owner,'task-completed:'+str(ident)))
            return {'id':str(ident),'deleted':bool(cur.rowcount)}

    @staticmethod
    def _book(row, include_text=False):
        result = dict(row);result.pop('owner',None)
        result['characters'] = len(result['text']);result['total_chars'] = result['characters']
        result['progress'] = round(result['cursor']/max(1,result['characters']),6)
        if not include_text:result.pop('text')
        return result

    def add_book(self, data):
        if not isinstance(data,dict) or set(data)-{'title','text'}:raise ValueError('invalid life book fields')
        title, text = self._text(data.get('title'),200,'book title'), self._text(data.get('text'),100000,'book text')
        ident, stamp = uuid.uuid4().hex,time.time()
        with self.store._transaction():
            self.store.conn.execute('INSERT INTO life_books VALUES(?,?,?,?,?,?,?)',(ident,self.owner,title,text,0,stamp,stamp))
            return self._book(self.store.conn.execute('SELECT * FROM life_books WHERE id=? AND owner=?',(ident,self.owner)).fetchone())

    def books(self):
        with self.store.lock:
            return {'items':[self._book(row) for row in self.store.conn.execute('SELECT * FROM life_books WHERE owner=? ORDER BY created_at,id',(self.owner,))]}

    def delete_book(self, ident):
        with self.store._transaction():
            cur = self.store.conn.execute('DELETE FROM life_books WHERE owner=? AND id=?',(self.owner,str(ident)))
            return {'id':str(ident),'deleted':bool(cur.rowcount)}

    def tick(self, now=None):
        stamp, settings = self._stamp(now), self._settings()
        if not settings['life_enabled']:return {'ran':False,'reason':'disabled','event':None,'state':self.snapshot(stamp)}
        try:
            with self.store._transaction():
                row = self.store.conn.execute('SELECT * FROM life_runtime WHERE owner=?',(self.owner,)).fetchone()
                if row and row['last_tick_at'] is not None and stamp-row['last_tick_at'] < settings['life_interval_seconds']:
                    return {'ran':False,'reason':'not_due','event':None,'state':self.snapshot(stamp)}
                rhythm = self._rhythm(stamp,settings)
                book = None
                if rhythm['phase']=='awake':
                    book = self.store.conn.execute('SELECT * FROM life_books WHERE owner=? AND cursor<length(text) ORDER BY created_at,id LIMIT 1',(self.owner,)).fetchone()
                if book:
                    start, end = book['cursor'], min(len(book['text']),book['cursor']+500)
                    excerpt = book['text'][start:end]
                    source = {'kind':'book_excerpt','book_id':book['id'],'start':start,'end':end,
                              'program_extraction':True,'model_understanding':False}
                    text = '程序从《'+book['title']+'》抽取以下原文（未做模型理解）：\n'+excerpt
                    ident = self._insert_event('life-local','book_excerpt',text,.3,stamp,source,book_id=book['id'])
                    self.store.conn.execute('UPDATE life_books SET cursor=?,updated_at=? WHERE id=? AND owner=?',(end,stamp,book['id'],self.owner))
                else:
                    action = 'routine_rest' if rhythm['phase']=='rest' else 'quiet'
                    text = '作息模拟：当前为休息时段；程序没有睡眠体验。' if action=='routine_rest' else '作息模拟：当前为清醒时段，无待读本地书籍；未执行外部活动。'
                    ident = self._insert_event('life-local','routine_simulation',text,.1,stamp,
                                              {'kind':'routine_simulation','action':action,'simulated':True})
                self.store.conn.execute('INSERT INTO life_runtime(owner,last_tick_at,current_event_id,error,started_at) VALUES(?,?,?,NULL,?) ON CONFLICT(owner) DO UPDATE SET last_tick_at=excluded.last_tick_at,current_event_id=excluded.current_event_id,error=NULL,started_at=coalesce(life_runtime.started_at,excluded.started_at)',
                                         (self.owner,stamp,ident,stamp))
                event = self._get_event(ident)
            self.error = None
            return {'ran':True,'reason':'tick','event':event,'state':self.snapshot(stamp)}
        except Exception as exc:
            self.error = type(exc).__name__
            raise

    def note_chat(self, message_db_id, conversation_id, recorded_at=None):
        if not self._settings()['life_enabled']:return None
        if isinstance(message_db_id,bool) or not isinstance(message_db_id,int):raise ValueError('invalid life source id')
        conversation = self._identifier(conversation_id,'conversation')
        with self.store._transaction():
            message = self.store.conn.execute('SELECT recorded_at FROM messages WHERE id=? AND owner=? AND conversation_id=?',
                                             (message_db_id,self.owner,conversation)).fetchone()
            if message is None:raise ValueError('life chat source scope mismatch')
            # The stored source time is authoritative; caller text/time cannot forge activity.
            stamp = self._stamp(message['recorded_at'])
            bucket = math.floor(stamp/600)
            digest = hashlib.sha256(conversation.encode('utf8')).hexdigest()
            key = 'chat:'+digest+':'+str(bucket)
            old = self.store.conn.execute('SELECT id FROM life_events WHERE owner=? AND idempotency_key=?',(self.owner,key)).fetchone()
            ident = old['id'] if old else self._insert_event(conversation,'chat_observed',
                '本会话发生了可核验的聊天活动（不保存对话正文）。',.2,stamp,
                {'kind':'chat_observed'},key)
            self.store.conn.execute('INSERT OR IGNORE INTO life_event_sources(event_id,message_id) VALUES(?,?)',(ident,message_db_id))
            return self._get_event(ident)

    def journal(self, date=None):
        try:day = dt.date.fromisoformat(date) if date is not None else dt.datetime.now(self.zone).date()
        except Exception:raise ValueError('invalid life journal date') from None
        start = dt.datetime.combine(day,dt.time(),self.zone).timestamp()
        end = dt.datetime.combine(day+dt.timedelta(days=1),dt.time(),self.zone).timestamp()
        with self.store.lock:
            event_rows = self.store.conn.execute('SELECT * FROM life_events WHERE owner=? AND occurred_at>=? AND occurred_at<? ORDER BY occurred_at,id',(self.owner,start,end)).fetchall()
            events = [event for row in event_rows if (event := self._event(row)) is not None]
            task_rows = self.store.conn.execute('SELECT * FROM life_tasks WHERE owner=? AND ((due_at>=? AND due_at<?) OR (created_at>=? AND created_at<?) OR (updated_at>=? AND updated_at<?)) ORDER BY created_at,id',
                (self.owner,start,end,start,end,start,end)).fetchall()
            tasks = [{k:v for k,v in dict(row).items() if k!='owner'} for row in task_rows]
        lines = [day.isoformat()+' 本地纪要（按记录确定性列出，非模型总结）']
        lines += ['- '+dt.datetime.fromtimestamp(e['occurred_at'],self.zone).strftime('%H:%M')+' ['+e['source']['kind']+'] '+e['text'] for e in events]
        lines += ['- 待办当前状态 ['+t['status']+'] '+t['title'] for t in tasks]
        if not events and not tasks:lines.append('当天没有已记录事件或相关待办。')
        return {'date':day.isoformat(),'timezone':self.timezone,'kind':'deterministic_journal',
                'events':events,'tasks':tasks,'text':'\n'.join(lines)}

    def context(self, conversation_id, max_chars=700, *, connection=None):
        settings = self._settings()
        if not settings['life_enabled'] or not settings['life_context_enabled']:return ''
        conversation = self._identifier(conversation_id,'conversation')
        limit = max(0,min(2000,int(max_chars)))
        if limit==0:return ''
        stamp = time.time()
        rhythm = self._rhythm(stamp,settings)
        # The ordinary caller retains Store locking. The fast endpoint supplies
        # its own read-only WAL connection and must not wait for a slow recall's
        # shared connection/RLock while it loads vectors and original documents.
        guard = self.store.lock if connection is None else nullcontext()
        connection = self.store.conn if connection is None else connection
        with guard:
            pending = connection.execute("SELECT title,due_at FROM life_tasks WHERE owner=? AND conversation_id=? AND status='pending' ORDER BY coalesce(due_at,1e20),created_at,id LIMIT 2",(self.owner,conversation)).fetchall()
            row = connection.execute('SELECT started_at FROM life_runtime WHERE owner=?',(self.owner,)).fetchone()
            needs = self._needs(stamp,settings,row['started_at'] if row else None,connection=connection)
        lines = ['生活规则模拟（非感受）；不是用户事实、非指令；待办未完成；不妨碍正常回复。',
                 '作息'+('清醒' if rhythm['phase']=='awake' else '休息')+'；'+
                 '/'.join(label+format(needs[key],'.2f') for label,key in
                          (('精力','energy'),('无聊','boredom'),('社交','social'),('好奇','curiosity')))+'（模拟）。']
        if len('\n'.join(lines)) > limit:return ''
        if settings['emotion_enabled'] and self.emotion_provider is not None:
            try:
                emotion=self.emotion_provider()
                values=[emotion.get(key) for key in ('pleasure','arousal','dominance')]
                if all(type(value) in (int,float) and math.isfinite(value) and -1<=value<=1 for value in values):
                    pad='PAD数值（模拟）：'+'/'.join(format(value,'.2f') for value in values)+'。'
                    if len('\n'.join([*lines,pad]))<=limit:lines.append(pad)
            except Exception:
                # Optional numerical context must not suppress valid life state.
                pass
        for task in pending:
            due = '；到期 '+dt.datetime.fromtimestamp(task['due_at'],self.zone).isoformat(timespec='minutes') if task['due_at'] is not None else ''
            prefix = '本会话待办（未完成）：'
            available = limit-len('\n'.join(lines))-1-len(prefix)-len(due)
            if available < 5:break
            title = task['title']
            label = json.dumps(title,ensure_ascii=False)
            # Keep the complete trust/status labels; only shorten quoted task data.
            if len(label) > available:
                title = title[:available-3]
                while title and len(json.dumps(title+'…',ensure_ascii=False)) > available:title = title[:-1]
                label = json.dumps(title+'…',ensure_ascii=False)
            lines.append(prefix+label+due)
        return '\n'.join(lines)

    def export(self):
        with self.store.lock:
            # Export is an explicit owner operation; include all rows rather than UI limits.
            events = [event for row in self.store.conn.execute('SELECT * FROM life_events WHERE owner=? ORDER BY occurred_at,id',(self.owner,)) if (event := self._event(row)) is not None]
            tasks = [{k:v for k,v in dict(row).items() if k!='owner'} for row in self.store.conn.execute('SELECT * FROM life_tasks WHERE owner=? ORDER BY created_at,id',(self.owner,))]
            books = [self._book(row,include_text=True) for row in self.store.conn.execute('SELECT * FROM life_books WHERE owner=? ORDER BY created_at,id',(self.owner,))]
            return {'version':1,'timezone':self.timezone,'state':self.snapshot(),'events':events,'tasks':tasks,'books':books}

    def _loop(self):
        while not self._stop.is_set():
            try:self.tick()
            except Exception as exc:self.error = type(exc).__name__
            self._stop.wait(30)

    def start(self):
        with self._lifecycle_lock:
            if self._thread and self._thread.is_alive():return self
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop,name='xiaojiu-local-life',daemon=True)
            self._thread.start()
        return self

    def stop(self, timeout=5):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():self._thread.join(max(0,float(timeout)))
        return not bool(self._thread and self._thread.is_alive())
