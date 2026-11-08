"""Host-independent, owner-scoped evidence storage and durable work queue.

Offsets are Python Unicode character offsets, not UTF-16 or UTF-8 byte offsets.
Derived text is a model proposal with checked provenance, never a replacement for
the original message. Content equality alone never merges separate occurrences.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
import unicodedata
import uuid


DISCLOSURES = {"normal": 0, "only_if_asked": 1, "never_say": 2}
CHUNK_CHARS = 800
CHUNK_OVERLAP = 120


class JobLeaseLost(RuntimeError):
    """A stale worker has no authority to commit or settle the current job."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)


def _timestamp(value=None):
    if value is None:
        return time.time()
    if isinstance(value, bool):
        raise ValueError("timestamp must be an ISO timestamp or finite epoch seconds")
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            result = parsed.timestamp()
        except (ValueError, OverflowError):
            raise ValueError("invalid ISO timestamp") from None
    else:
        try:
            result = float(value)
        except (ValueError, TypeError):
            raise ValueError("invalid epoch timestamp") from None
    if not math.isfinite(result):
        raise ValueError("timestamp must be finite")
    return result


def _identifier(value, name, maximum=512):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"invalid {name}")
    return value


def _importance(value):
    if isinstance(value, bool):
        raise ValueError("importance must be a finite number between zero and one")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError("invalid importance") from None
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise ValueError("importance must be between zero and one")
    return result


def _topics(value):
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise ValueError("disclosure_topics must be a list of strings")
    return sorted({x.strip() for x in value if x.strip()})


def _metadata(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("metadata must be an object")
    return json.loads(_json(value))


def _lexical_tokens(text):
    """Language-neutral word tokens plus CJK ngrams; no question-type rules."""
    text = unicodedata.normalize("NFKC", text).casefold()
    words, word, cjk = [], [], []

    def flush_word():
        if word:
            words.append("".join(word))
            word.clear()

    def flush_cjk():
        if cjk:
            words.extend(cjk)
            words.extend("".join(cjk[i:i + 2]) for i in range(len(cjk) - 1))
            cjk.clear()

    for char in text:
        n = ord(char)
        if 0x3400 <= n <= 0x9FFF or 0x20000 <= n <= 0x3134F:
            flush_word()
            cjk.append(char)
        elif char.isalnum() or char == "_":
            flush_cjk()
            word.append(char)
        else:
            flush_word()
            flush_cjk()
    flush_word()
    flush_cjk()
    return words


def _chunks(text):
    start = 0
    while start < len(text):
        end = min(len(text), start + CHUNK_CHARS)
        if end < len(text):
            boundary = text.rfind("\n", start + CHUNK_CHARS // 2, end)
            if boundary >= 0:
                end = boundary + 1
        yield start, end
        if end == len(text):
            break
        start = max(start + 1, end - CHUNK_OVERLAP)


class Store:
    def __init__(self, path):
        self.path = str(path)
        if self.path != ":memory:":
            target = Path(path).expanduser().resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            self.path = str(target)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL,
          conversation_id TEXT NOT NULL, message_id TEXT NOT NULL,
          role TEXT NOT NULL CHECK(role IN ('user','assistant')), text TEXT NOT NULL,
          recorded_at REAL NOT NULL, metadata TEXT NOT NULL,
          UNIQUE(owner,conversation_id,message_id,role));
        CREATE INDEX IF NOT EXISTS ix_messages_scope ON messages(owner,conversation_id,recorded_at,id);
        CREATE TABLE IF NOT EXISTS ingest_keys (
          owner TEXT NOT NULL, conversation_id TEXT NOT NULL, external_id TEXT NOT NULL,
          role TEXT NOT NULL, body_hash TEXT NOT NULL, message_db_id INTEGER NOT NULL,
          deleted INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(owner,conversation_id,external_id,role));
        CREATE TABLE IF NOT EXISTS documents (
          id TEXT PRIMARY KEY, owner TEXT NOT NULL, conversation_id TEXT NOT NULL,
          kind TEXT NOT NULL CHECK(kind IN ('raw','summary','fact')), role TEXT NOT NULL,
          text TEXT NOT NULL, recorded_at REAL NOT NULL, created_at REAL NOT NULL,
          importance REAL NOT NULL, status TEXT NOT NULL DEFAULT 'active',
          pinned INTEGER NOT NULL DEFAULT 0, disclosure TEXT NOT NULL DEFAULT 'normal',
          metadata TEXT NOT NULL, content_hash TEXT NOT NULL, source_key TEXT NOT NULL,
          UNIQUE(owner,conversation_id,kind,source_key,content_hash));
        CREATE INDEX IF NOT EXISTS ix_documents_scope ON documents(owner,conversation_id,status,recorded_at);
        CREATE TABLE IF NOT EXISTS document_sources (
          document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
          message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
          start INTEGER NOT NULL, end INTEGER NOT NULL,
          PRIMARY KEY(document_id,message_id,start,end));
        CREATE INDEX IF NOT EXISTS ix_sources_message ON document_sources(message_id,document_id);
        CREATE TABLE IF NOT EXISTS document_dependencies (
          document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
          message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
          PRIMARY KEY(document_id,message_id));
        CREATE INDEX IF NOT EXISTS ix_dependencies_message ON document_dependencies(message_id,document_id);
        CREATE TABLE IF NOT EXISTS message_dependencies (
          source_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
          dependent_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
          PRIMARY KEY(source_id,dependent_id));
        CREATE VIRTUAL TABLE IF NOT EXISTS document_fts USING fts5(
          document_id UNINDEXED, terms, tokenize='unicode61 remove_diacritics 2');
        CREATE TABLE IF NOT EXISTS vectors (
          document_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
          owner TEXT NOT NULL, model TEXT NOT NULL, vector TEXT NOT NULL, dimensions INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, owner TEXT NOT NULL,
          conversation_id TEXT NOT NULL, dedupe_key TEXT NOT NULL,
          document_id TEXT REFERENCES documents(id) ON DELETE CASCADE,
          message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
          status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
          available_at REAL NOT NULL, lease_until REAL NOT NULL DEFAULT 0,
          worker_id TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
          created_at REAL NOT NULL, updated_at REAL NOT NULL,
          UNIQUE(kind,dedupe_key));
        CREATE INDEX IF NOT EXISTS ix_jobs_available ON jobs(kind,status,available_at,id);
        CREATE TABLE IF NOT EXISTS model_attempts (
          attempt_id TEXT PRIMARY KEY, owner TEXT NOT NULL,
          started_at REAL NOT NULL, outcome TEXT NOT NULL, event_json TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS ix_model_attempts_owner ON model_attempts(owner,started_at);
        PRAGMA user_version=1;
        """)
        # Additive only: old code can still read the database after a code rollback.
        if 'batch_limit' not in {r['name'] for r in self.conn.execute('PRAGMA table_info(jobs)')}:
            self.conn.execute('ALTER TABLE jobs ADD COLUMN batch_limit INTEGER NOT NULL DEFAULT 12')

    @contextmanager
    def _transaction(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.conn.rollback()
                raise
            else:
                self.conn.commit()

    def close(self):
        with self.lock:
            self.conn.close()

    def _enqueue(self, kind, owner, conversation_id, key, document_id=None, message_id=None):
        now = time.time()
        self.conn.execute("""INSERT OR IGNORE INTO jobs
          (kind,owner,conversation_id,dedupe_key,document_id,message_id,available_at,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?)""",
          (kind,owner,conversation_id,key,document_id,message_id,now,now,now))

    def ingest(self, owner, conversation_id, message_id, role, text, recorded_at=None, metadata=None, source_dependencies=None):
        owner = _identifier(owner, "owner", 256)
        conversation_id = _identifier(conversation_id, "conversation_id")
        message_id = _identifier(message_id, "message_id")
        if role not in ("user", "assistant"):
            raise ValueError("only visible user and assistant messages can be ingested")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("message text is required")
        stamp = _timestamp(recorded_at)
        meta = _metadata(metadata)
        if isinstance(recorded_at,str):
            parsed = datetime.fromisoformat(recorded_at.replace("Z","+00:00"))
            if parsed.tzinfo is None:parsed=parsed.replace(tzinfo=timezone.utc)
            meta["recorded_at_iso"] = parsed.isoformat()
        else:
            meta["recorded_at_iso"] = datetime.fromtimestamp(stamp,timezone.utc).isoformat()
        disclosure = meta.get("disclosure", "normal")
        if disclosure not in DISCLOSURES:
            raise ValueError("invalid disclosure")
        meta["disclosure_topics"] = _topics(meta.get("disclosure_topics"))
        meta["disclosure"] = disclosure
        if meta.get("skip_derivation") is True:
            meta["derivation_status"]="skipped_import" if meta.get("source_kind","").startswith("imported_") else "skipped_by_owner"
        importance = _importance(meta.get("importance", .5))
        body_hash = hashlib.sha256(text.encode("utf8")).hexdigest()
        with self._transaction():
            old = self.conn.execute("""SELECT body_hash,message_db_id,deleted FROM ingest_keys
                WHERE owner=? AND conversation_id=? AND external_id=? AND role=?""",
                (owner,conversation_id,message_id,role)).fetchone()
            if old:
                if old["body_hash"] != body_hash:
                    raise ValueError("the stable message key already has different text")
                if source_dependencies and not old["deleted"]:
                    self._link_import_message(owner,conversation_id,old["message_db_id"],source_dependencies)
                return {"accepted":False,"duplicate":True,"id":old["message_db_id"],"deleted":bool(old["deleted"])}
            cur = self.conn.execute("""INSERT INTO messages
              (owner,conversation_id,message_id,role,text,recorded_at,metadata) VALUES(?,?,?,?,?,?,?)""",
              (owner,conversation_id,message_id,role,text,stamp,_json(meta)))
            mid = cur.lastrowid
            self.conn.execute("INSERT INTO ingest_keys VALUES(?,?,?,?,?,?,0)",
                (owner,conversation_id,message_id,role,body_hash,mid))
            if source_dependencies:
                self._link_import_message(owner,conversation_id,mid,source_dependencies)
            docs = []
            for start,end in _chunks(text):
                doc = self._insert_document(owner,conversation_id,"raw",text[start:end],
                  [{"message_id":mid,"start":start,"end":end}],meta,importance,disclosure,
                  role,stamp,document_id=f"raw:{mid}:{start}")
                docs.append(doc["id"])
            if meta.get("skip_derivation") is not True:
                self._enqueue("derive",owner,conversation_id,f"message:{mid}",message_id=mid)
        return {"accepted":True,"duplicate":False,"id":mid,"document_ids":docs}

    def _link_import_message(self, owner, conversation_id, dependent_id, source_ids):
        note=self.conn.execute("SELECT metadata FROM messages WHERE id=? AND owner=? AND conversation_id=?",
            (dependent_id,owner,conversation_id)).fetchone()
        if not note or json.loads(note[0]).get("source_kind")!="imported_memory":
            raise ValueError("only labelled legacy notes accept import dependencies")
        for mid in source_ids:
            if isinstance(mid,bool) or not isinstance(mid,int):raise ValueError("invalid import dependency")
            raw=self.conn.execute("SELECT metadata FROM messages WHERE id=? AND owner=? AND conversation_id=?",
                (mid,owner,conversation_id)).fetchone()
            if not raw or json.loads(raw[0]).get("source_kind")!="imported_raw":
                raise ValueError("legacy note dependency is not an imported original in the same scope")
            self.conn.execute("INSERT OR IGNORE INTO message_dependencies(source_id,dependent_id) VALUES(?,?)",(mid,dependent_id))

    def _validate_sources(self, owner, conversation_id, refs):
        if not isinstance(refs, list) or not refs:
            raise ValueError("source_refs must contain original source spans")
        checked = []
        seen = set()
        for ref in refs:
            if not isinstance(ref, dict):
                raise ValueError("invalid source reference")
            mid,start,end = (ref.get(k) for k in ("message_id","start","end"))
            if any(isinstance(v,bool) or not isinstance(v,int) for v in (mid,start,end)):
                raise ValueError("source IDs and offsets must be integers")
            msg = self.conn.execute("SELECT * FROM messages WHERE id=? AND owner=? AND conversation_id=?",
                 (mid,owner,conversation_id)).fetchone()
            if msg is None:
                raise ValueError("source message does not belong to the supplied owner and conversation")
            if not 0 <= start < end <= len(msg["text"]):
                raise ValueError("source span is outside the original message")
            key = (mid,start,end)
            if key not in seen:
                seen.add(key)
                checked.append((dict(msg),start,end))
        return checked

    @staticmethod
    def _inherited_disclosure(checked, requested, metadata):
        if requested not in DISCLOSURES:
            raise ValueError("invalid disclosure")
        level = DISCLOSURES[requested]
        restricted_topics = []
        source_level = 0
        for msg,_,_ in checked:
            meta = json.loads(msg["metadata"])
            source_level = max(source_level, DISCLOSURES.get(meta.get("disclosure", "normal"),2))
            if meta.get("disclosure") == "only_if_asked":
                restricted_topics.append(set(_topics(meta.get("disclosure_topics"))))
        level = max(level,source_level)
        effective = next(k for k,v in DISCLOSURES.items() if v==level)
        if restricted_topics:
            topics = sorted(set.intersection(*restricted_topics))
        else:
            topics = _topics(metadata.get("disclosure_topics"))
        return effective,topics,source_level

    def _insert_document(self, owner, conversation_id, kind, text, refs, metadata,
                         importance, disclosure, role, recorded_at, document_id=None, dependency_ids=None):
        checked = self._validate_sources(owner,conversation_id,refs)
        canonical = sorted((m["id"],a,b) for m,a,b in checked)
        source_key = hashlib.sha256(_json(canonical).encode()).hexdigest()
        content_hash = hashlib.sha256(text.encode("utf8")).hexdigest()
        prior = self.conn.execute("""SELECT * FROM documents WHERE owner=? AND conversation_id=?
             AND kind=? AND source_key=? AND content_hash=?""",
             (owner,conversation_id,kind,source_key,content_hash)).fetchone()
        if prior:
            self.conn.executemany("INSERT OR IGNORE INTO document_dependencies VALUES(?,?)",
                [(prior["id"],mid) for mid in (dependency_ids or {m["id"] for m,_,_ in checked})])
            # Idempotent content/source does not grant authority to lower an
            # existing privacy restriction. Re-observed stricter policy sticks.
            old_meta=json.loads(prior["metadata"])
            effective=prior["disclosure"]
            if DISCLOSURES[disclosure]>DISCLOSURES[effective]:
                effective=disclosure
                old_meta["disclosure_topics"]=_topics(metadata.get("disclosure_topics"))
            elif effective=="only_if_asked" and disclosure=="only_if_asked":
                old_meta["disclosure_topics"]=sorted(set(_topics(old_meta.get("disclosure_topics"))) &
                    set(_topics(metadata.get("disclosure_topics"))))
            self.conn.execute("UPDATE documents SET disclosure=?,metadata=? WHERE id=?",
                 (effective,_json(old_meta),prior["id"]))
            return self._document(self.conn.execute("SELECT * FROM documents WHERE id=?",(prior["id"],)).fetchone())
        docid = document_id or str(uuid.uuid4())
        self.conn.execute("""INSERT INTO documents
          (id,owner,conversation_id,kind,role,text,recorded_at,created_at,importance,status,pinned,
           disclosure,metadata,content_hash,source_key) VALUES(?,?,?,?,?,?,?,?,?,'active',0,?,?,?,?)""",
          (docid,owner,conversation_id,kind,role,text,recorded_at,time.time(),importance,
           disclosure,_json(metadata),content_hash,source_key))
        self.conn.executemany("INSERT INTO document_sources VALUES(?,?,?,?)",
             [(docid,m["id"],start,end) for m,start,end in checked])
        self.conn.executemany("INSERT INTO document_dependencies VALUES(?,?)",
             [(docid,mid) for mid in (dependency_ids or {m["id"] for m,_,_ in checked})])
        self.conn.execute("INSERT INTO document_fts(document_id,terms) VALUES(?,?)",
             (docid," ".join(_lexical_tokens(text))))
        self._enqueue("embed",owner,conversation_id,"document:"+docid,document_id=docid)
        return self._document(self.conn.execute("SELECT * FROM documents WHERE id=?",(docid,)).fetchone())

    def _add_derived(self, owner, conversation_id, kind, text, source_refs,
                     metadata=None, importance=.5, disclosure="normal", dependency_message_ids=None):
        if kind not in ("summary","fact"):
            raise ValueError("derived kind must be summary or fact")
        if not isinstance(text,str) or not text.strip():
            raise ValueError("derived text is required")
        checked = self._validate_sources(owner,conversation_id,source_refs)
        dependencies={m["id"] for m,_,_ in checked}
        dependencies.update(dependency_message_ids or [])
        context=[]
        for mid in dependencies:
            msg=self.conn.execute("SELECT * FROM messages WHERE id=? AND owner=? AND conversation_id=?",(mid,owner,conversation_id)).fetchone()
            if not msg:raise ValueError("derivation context is not available in this scope")
            context.append((dict(msg),0,len(msg["text"])))
        meta = _metadata(metadata)
        if any(json.loads(m["metadata"]).get("import_model_policy")=="local_only" for m,_,_ in context):
            meta["import_model_policy"]="local_only"
        effective,topics,source_level = self._inherited_disclosure(context,disclosure,meta)
        meta["disclosure_topics"] = topics
        meta["source_disclosure_level"] = source_level
        meta["verification"] = "source_spans_validated_not_semantic_proof"
        stamp = max(m["recorded_at"] for m,_,_ in checked)
        meta["last_decay_at"] = stamp
        roles = {m["role"] for m,_,_ in checked}
        return self._insert_document(owner,conversation_id,kind,text,source_refs,meta,
             _importance(importance),effective,next(iter(roles)) if len(roles)==1 else "mixed",stamp,dependency_ids=dependencies)

    def add_derived(self, owner, conversation_id, kind, text, source_refs, metadata=None,
                    importance=.5, disclosure="normal", dependency_message_ids=None):
        with self._transaction():
            return self._add_derived(owner,conversation_id,kind,text,source_refs,metadata,importance,disclosure,dependency_message_ids)

    def remember_important(self, owner, conversation_id, request_message_id, source_refs=None, disclosure=None):
        """Pin literal visible-user evidence; no model rewrite or synthetic raw row.

        The existing canonical source/content UNIQUE key and this transaction
        make concurrent/retried actions reuse documents. A repeated old request
        does not undo later owner edits such as unpinning or archiving.
        """
        owner=_identifier(owner,'owner',256)
        conversation_id=_identifier(conversation_id,'conversation_id')
        request_message_id=_identifier(request_message_id,'request_message_id')
        if disclosure is not None and (not isinstance(disclosure,str) or disclosure not in DISCLOSURES):raise ValueError('important_invalid_disclosure')
        if source_refs is None:source_refs=[{'message_id':request_message_id}]
        if not isinstance(source_refs,list) or not 1<=len(source_refs)<=12:raise ValueError('important_invalid_sources')
        now=time.time()

        def original(external_id, purpose):
            external_id=_identifier(external_id,'source_message_id')
            row=self.conn.execute("SELECT * FROM messages WHERE owner=? AND conversation_id=? AND message_id=? AND role='user'",
                                  (owner,conversation_id,external_id)).fetchone()
            if row is None:raise ValueError('important_'+purpose+'_not_archived')
            item=dict(row);meta=json.loads(item['metadata'])
            if not isinstance(meta,dict) or ('source_kind' in meta and meta['source_kind'] not in ('user','conversation')):
                raise ValueError('important_original_user_required')
            if not isinstance(item['recorded_at'],(int,float)) or not math.isfinite(item['recorded_at']) or item['recorded_at']>now:
                raise ValueError('important_future_source')
            return item

        def protected_level(message, start, end):
            level=0
            for r in self.conn.execute("""SELECT d.disclosure FROM document_sources s
                 JOIN documents d ON d.id=s.document_id
                 WHERE s.message_id=? AND d.owner=? AND d.conversation_id=? AND d.status!='deleted'
                   AND s.start<? AND ?<s.end""",(message['id'],owner,conversation_id,end,start)):
                level=max(level,DISCLOSURES.get(r['disclosure'],2))
            return level

        with self._transaction():
            request=original(request_message_id,'request')
            selected={}
            for ref in source_refs:
                if not isinstance(ref,dict) or set(ref)-{'message_id','start','end'}:raise ValueError('important_invalid_source_fields')
                message=original(ref.get('message_id'),'source')
                if message['id']>request['id'] or message['recorded_at']>request['recorded_at']:
                    raise ValueError('important_source_after_request')
                start,end=ref.get('start',0),ref.get('end',len(message['text']))
                if any(type(v) is not int for v in (start,end)) or not 0<=start<end<=len(message['text']):
                    raise ValueError('important_invalid_span')
                text=message['text'][start:end]
                if not text.strip():raise ValueError('important_empty_span')
                selected[(message['id'],start,end)]=(message,text)
            prepared=[]
            for (mid,start,end),(message,text) in sorted(selected.items()):
                source_key=hashlib.sha256(_json([(mid,start,end)]).encode()).hexdigest()
                content_hash=hashlib.sha256(text.encode('utf8')).hexdigest()
                prior=self.conn.execute("""SELECT * FROM documents WHERE owner=? AND conversation_id=?
                    AND kind='fact' AND source_key=? AND content_hash=?""",
                    (owner,conversation_id,source_key,content_hash)).fetchone()
                inherited,_,_=self._inherited_disclosure([(message,start,end),(request,0,len(request['text']))],'normal',{})
                level=max(DISCLOSURES[inherited],protected_level(message,start,end),
                          protected_level(request,0,len(request['text'])),
                          DISCLOSURES.get(prior['disclosure'],2) if prior else 0)
                if disclosure is not None and DISCLOSURES[disclosure]<level:
                    raise ValueError('important_cannot_lower_disclosure')
                level=max(level,DISCLOSURES[disclosure] if disclosure is not None else 0)
                effective=next(key for key,value in DISCLOSURES.items() if value==level)
                prepared.append((message,start,end,text,prior,effective))
            items=[]
            for message,start,end,text,prior,effective in prepared:
                old_meta=json.loads(prior['metadata']) if prior else {}
                old_retention=old_meta.get('retention') if isinstance(old_meta.get('retention'),dict) else {}
                origins=old_retention.get('request_source_ids',[])
                origins=[v for v in origins if type(v) is int] if isinstance(origins,list) else []
                duplicate=bool(prior and request['id'] in origins and old_retention.get('explicit_user_request') is True
                               and old_retention.get('operation_verified') is True)
                if duplicate:
                    # A later tighter request is safe, but a replay never silently
                    # re-pins content the owner subsequently unpinned/archived.
                    if DISCLOSURES[effective]>DISCLOSURES.get(prior['disclosure'],2):
                        old_meta['disclosure_topics']=[]
                        self.conn.execute('UPDATE documents SET disclosure=?,metadata=? WHERE id=? AND owner=?',
                                          (effective,_json(old_meta),prior['id'],owner))
                    ident=prior['id']
                else:
                    retention={**old_retention,'kind':'explicit_user_request','explicit_user_request':True,
                        'operation_verified':True,
                        'request_message_id':old_retention.get('request_message_id',request['id']),
                        'request_external_id':old_retention.get('request_external_id',request_message_id),
                        'request_source_ids':sorted(set([*origins,request['id']])),
                        'declared_at':old_retention.get('declared_at',now),'declared_importance':.95}
                    document=self._add_derived(owner,conversation_id,'fact',text,
                        [{'message_id':message['id'],'start':start,'end':end}],
                        metadata={'retention':retention,'verbatim_user_quote':True,'claims_verified':False},
                        importance=.95,disclosure=effective,
                        dependency_message_ids=[message['id'],request['id']])
                    ident=document['id']
                    meta=document['metadata']
                    meta.update(retention=retention,verbatim_user_quote=True,claims_verified=False)
                    self.conn.execute("UPDATE documents SET pinned=1,importance=.95,status='active',metadata=? WHERE id=? AND owner=?",
                                      (_json(meta),ident,owner))
                current=self.conn.execute('SELECT pinned,importance,disclosure,status FROM documents WHERE id=? AND owner=?',(ident,owner)).fetchone()
                items.append({'id':ident,'pinned':bool(current['pinned']),'importance':current['importance'],
                              'disclosure':current['disclosure'],'status':current['status'],'duplicate':duplicate})
            all_duplicate=all(item['duplicate'] for item in items)
            return {'accepted':not all_duplicate,'duplicate':all_duplicate,
                'document_ids':[item['id'] for item in items],'items':items,
                'request_message_id':request['id'],
                'source_message_ids':sorted({mid for mid,_,_ in selected})}

    def _document(self, row):
        result = dict(row)
        result["metadata"] = json.loads(result["metadata"])
        result["pinned"] = bool(result["pinned"])
        result["source_refs"] = []
        for r in self.conn.execute("""SELECT s.message_id,s.start,s.end,m.text
              FROM document_sources s JOIN messages m ON m.id=s.message_id
              WHERE s.document_id=? ORDER BY s.message_id,s.start,s.end""",(result["id"],)):
            result["source_refs"].append({"message_id":r["message_id"],"start":r["start"],
                 "end":r["end"],"quote":r["text"][r["start"]:r["end"]]})
        vector = self.conn.execute("SELECT vector,model FROM vectors WHERE document_id=? AND owner=?",
             (result["id"],result["owner"])).fetchone()
        result["vector"] = json.loads(vector["vector"]) if vector else None
        result["vector_model"] = vector["model"] if vector else None
        return result

    def find_message(self, owner, conversation_id, message_id, role="user"):
        with self.lock:
            row=self.conn.execute("SELECT * FROM messages WHERE owner=? AND conversation_id=? AND message_id=? AND role=?",
                (owner,conversation_id,message_id,role)).fetchone()
            if not row:return None
            result=dict(row);result["metadata"]=json.loads(result["metadata"])
            return result

    @staticmethod
    def _kind_filter(where, args, kind=None, kinds=None, prefix=""):
        selected = kinds if kinds is not None else (["fact","summary"] if kind=="derived" else ([kind] if kind else None))
        if selected is not None:
            if isinstance(selected,str):selected=[selected]
            if not isinstance(selected,(list,tuple)) or any(k not in ("raw","summary","fact") for k in selected):
                raise ValueError("invalid document kinds")
            if not selected:where.append("0")
            else:
                where.append(prefix+"kind IN ("+",".join("?" for _ in selected)+")");args.extend(selected)

    def documents(self, owner, conversation_id=None, as_of=None, include_archived=False, kind=None, kinds=None):
        where,args = ["owner=?"],[owner]
        where.append("status IN ('active','archived')" if include_archived else "status='active'")
        if conversation_id is not None:
            where.append("conversation_id=?");args.append(conversation_id)
        if as_of is not None:
            where.append("recorded_at<=?");args.append(_timestamp(as_of))
        self._kind_filter(where,args,kind,kinds)
        with self.lock:
            return [self._document(r) for r in self.conn.execute("SELECT documents.*, (SELECT max(dep.message_id) FROM document_dependencies dep WHERE dep.document_id=documents.id) AS max_dependency_id FROM documents WHERE "+" AND ".join(where)+" ORDER BY recorded_at,id",args)]

    def lexical_search(self, owner, query, conversation_id=None, limit=40, as_of=None, kind=None, include_archived=False, kinds=None, cutoff_message_id=None):
        tokens = list(dict.fromkeys(_lexical_tokens(str(query or ""))))[:256]
        if not tokens:
            return []
        expression = " OR ".join('"'+t.replace('"','""')+'"' for t in tokens)
        where,args = ["document_fts MATCH ?","d.owner=?",
            "d.status IN ('active','archived')" if include_archived else "d.status='active'"],[expression,owner]
        if conversation_id is not None:
            where.append("d.conversation_id=?");args.append(conversation_id)
        if as_of is not None:
            where.append("d.recorded_at<=?");args.append(_timestamp(as_of))
        if cutoff_message_id is not None:
            if type(cutoff_message_id) is not int or cutoff_message_id<=0:raise ValueError('invalid source cutoff')
            where.append('NOT EXISTS (SELECT 1 FROM document_dependencies dep WHERE dep.document_id=d.id AND dep.message_id>?)')
            args.append(cutoff_message_id)
        self._kind_filter(where,args,kind,kinds,prefix="d.")
        args.append(max(0,min(1000,int(limit))))
        with self.lock:
            found = self.conn.execute("""SELECT d.*,bm25(document_fts) AS lexical_score
                FROM document_fts JOIN documents d ON d.id=document_fts.document_id WHERE """+
                " AND ".join(where)+" ORDER BY lexical_score,d.recorded_at DESC,d.id LIMIT ?",args)
            return [self._document(r) for r in found]

    def get_document(self, owner, document_id):
        with self.lock:
            row=self.conn.execute("SELECT * FROM documents WHERE id=? AND owner=?",(document_id,owner)).fetchone()
            return self._document(row) if row else None

    def raw_documents_for_message(self, owner, message_id):
        with self.lock:
            return [self._document(r) for r in self.conn.execute("""SELECT DISTINCT d.* FROM documents d
              JOIN document_sources s ON s.document_id=d.id WHERE d.owner=? AND d.kind='raw' AND s.message_id=? ORDER BY d.id""",(owner,message_id))]

    def conversations(self, owner):
        with self.lock:
            return [{"id":r[0],"message_count":r[1],"last_message_at":r[2]} for r in self.conn.execute(
                "SELECT conversation_id,count(*),max(recorded_at) FROM messages WHERE owner=? GROUP BY conversation_id ORDER BY max(recorded_at) DESC,conversation_id",(owner,))]

    def get_message(self, owner, message_id):
        with self.lock:
            row = self.conn.execute("SELECT * FROM messages WHERE owner=? AND id=?",(owner,message_id)).fetchone()
            if not row:
                return None
            result = dict(row);result["metadata"] = json.loads(result["metadata"])
            return result

    def list_messages(self, owner, conversation_id=None, limit=50, offset=0):
        where,args = "owner=?",[owner]
        if conversation_id is not None:
            where += " AND conversation_id=?";args.append(conversation_id)
        args.extend([max(0,min(int(limit),1000)),max(0,int(offset))])
        with self.lock:
            result = []
            for r in self.conn.execute("SELECT * FROM messages WHERE "+where+" ORDER BY id DESC LIMIT ? OFFSET ?",args):
                item = dict(r);item["metadata"] = json.loads(item["metadata"]);result.append(item)
            return result

    def list_memories(self, owner, kind=None, status=None, limit=50):
        where,args = ["owner=?"],[owner]
        if kind:
            where.append("kind=?");args.append(kind)
        else:
            where.append("kind!='raw'")
        if status:
            where.append("status=?");args.append(status)
        else:
            where.append("status!='deleted'")
        args.append(max(0,min(1000,int(limit))))
        with self.lock:
            return [self._document(r) for r in self.conn.execute("SELECT * FROM documents WHERE "+" AND ".join(where)+" ORDER BY recorded_at DESC,id LIMIT ?",args)]

    def _set_vector(self, owner, document_id, vector, model):
        if not isinstance(vector,(list,tuple)) or not vector or len(vector)>65536:
            raise ValueError("vector must be a nonempty finite numeric array")
        if any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) for x in vector):
            raise ValueError("vector must contain finite numbers")
        values = [float(x) for x in vector]
        if not any(values):
            raise ValueError("zero vector is not a usable embedding")
        model = _identifier(model,"vector model",512)
        row = self.conn.execute("SELECT id FROM documents WHERE id=? AND owner=?",(document_id,owner)).fetchone()
        if not row:
            raise ValueError("document not found")
        self.conn.execute("""INSERT INTO vectors(document_id,owner,model,vector,dimensions) VALUES(?,?,?,?,?)
              ON CONFLICT(document_id) DO UPDATE SET owner=excluded.owner,model=excluded.model,
              vector=excluded.vector,dimensions=excluded.dimensions""",(document_id,owner,model,_json(values),len(values)))

    def set_vector(self, owner, document_id, vector, model):
        with self._transaction():
            self._set_vector(owner,document_id,vector,model)

    def update_memory(self, owner, document_id, patch):
        if not isinstance(patch,dict) or set(patch)-{"pin","pinned","importance","status","disclosure","disclosure_topics"}:
            raise ValueError("only pin, importance, status and disclosure settings can be changed")
        with self._transaction():
            row = self.conn.execute("SELECT * FROM documents WHERE id=? AND owner=?",(document_id,owner)).fetchone()
            if not row:
                raise ValueError("document not found")
            values = dict(row);meta = json.loads(values["metadata"])
            if "pin" in patch or "pinned" in patch:
                value = patch.get("pinned",patch.get("pin"))
                if not isinstance(value,bool):raise ValueError("pinned must be boolean")
                values["pinned"] = int(value)
            if "importance" in patch:values["importance"] = _importance(patch["importance"])
            if "status" in patch:
                if patch["status"] not in ("active","archived"):raise ValueError("use explicit DELETE to remove evidence")
                values["status"] = patch["status"]
            if "disclosure" in patch:
                if patch["disclosure"] not in DISCLOSURES:raise ValueError("invalid disclosure")
                values["disclosure"] = patch["disclosure"]
            if "disclosure_topics" in patch:meta["disclosure_topics"] = _topics(patch["disclosure_topics"])
            refs = self._document(row)["source_refs"]
            if row["kind"] != "raw":
                checked = self._dependency_sources(document_id,owner,row["conversation_id"])
                effective,topics,_ = self._inherited_disclosure(checked,values["disclosure"],meta)
                if effective != values["disclosure"]:raise ValueError("derived disclosure cannot weaken original source restrictions")
                meta["disclosure_topics"] = topics
            self.conn.execute("""UPDATE documents SET pinned=?,importance=?,status=?,disclosure=?,metadata=? WHERE id=? AND owner=?""",
                 (values["pinned"],values["importance"],values["status"],values["disclosure"],_json(meta),document_id,owner))
            if row["kind"] == "raw" and ({"disclosure","disclosure_topics"}&set(patch)):
                mid = refs[0]["message_id"]
                msg = self.conn.execute("SELECT metadata FROM messages WHERE id=? AND owner=?",(mid,owner)).fetchone()
                original_meta = json.loads(msg["metadata"])
                original_meta.update(disclosure=values["disclosure"],disclosure_topics=_topics(meta.get("disclosure_topics")))
                self.conn.execute("UPDATE messages SET metadata=? WHERE id=? AND owner=?",(_json(original_meta),mid,owner))
                affected = self.conn.execute("SELECT document_id FROM document_dependencies WHERE message_id=?",(mid,)).fetchall()
                for linked in affected:
                    d = self.conn.execute("SELECT * FROM documents WHERE id=?",(linked[0],)).fetchone()
                    dm = json.loads(d["metadata"])
                    checked = self._dependency_sources(d["id"],owner,d["conversation_id"])
                    effective,topics,_ = self._inherited_disclosure(checked,d["disclosure"] if d["kind"]!="raw" else values["disclosure"],dm)
                    dm["disclosure_topics"] = topics
                    self.conn.execute("UPDATE documents SET disclosure=?,metadata=? WHERE id=?",(effective,_json(dm),d["id"]))
            return self._document(self.conn.execute("SELECT * FROM documents WHERE id=?",(document_id,)).fetchone())

    def _remove_documents(self, ids):
        for docid in ids:
            self.conn.execute("DELETE FROM document_fts WHERE document_id=?",(docid,))
            self.conn.execute("DELETE FROM documents WHERE id=?",(docid,))

    def delete_message(self, owner, message_id):
        with self._transaction():
            row = self.conn.execute("SELECT id FROM messages WHERE owner=? AND id=?",(owner,message_id)).fetchone()
            if not row:return {"deleted":False,"id":message_id}
            messages={message_id};pending=[message_id]
            while pending:
                source=pending.pop()
                for linked in self.conn.execute("SELECT d.dependent_id FROM message_dependencies d JOIN messages m ON m.id=d.dependent_id WHERE d.source_id=? AND m.owner=?",(source,owner)).fetchall():
                    if linked[0] not in messages:messages.add(linked[0]);pending.append(linked[0])
            ids=set()
            for mid in messages:
                ids.update(r[0] for r in self.conn.execute("SELECT document_id FROM document_dependencies WHERE message_id=?",(mid,)))
            self._remove_documents(ids)
            for mid in messages:
                self.conn.execute("UPDATE ingest_keys SET deleted=1 WHERE owner=? AND message_db_id=?",(owner,mid))
                self.conn.execute("DELETE FROM messages WHERE owner=? AND id=?",(owner,mid))
            return {"deleted":True,"id":message_id,"removed_messages":sorted(messages),"removed_documents":sorted(ids)}

    def delete_memory(self, owner, document_id):
        with self._transaction():
            row = self.conn.execute("SELECT kind FROM documents WHERE owner=? AND id=?",(owner,document_id)).fetchone()
            if not row:return {"deleted":False,"id":document_id}
            if row["kind"]=="raw":raise ValueError("delete original evidence through delete_message")
            self._remove_documents([document_id])
            return {"deleted":True,"id":document_id}

    def stats(self, owner):
        with self.lock:
            kinds = dict(self.conn.execute("SELECT kind,count(*) FROM documents WHERE owner=? GROUP BY kind",(owner,)))
            statuses = dict(self.conn.execute("SELECT status,count(*) FROM documents WHERE owner=? GROUP BY status",(owner,)))
            messages = self.conn.execute("SELECT count(*) FROM messages WHERE owner=?",(owner,)).fetchone()[0]
            note_counts=self.conn.execute("""SELECT
                coalesce(sum(CASE WHEN json_extract(metadata,'$.source_kind')='imported_memory' THEN 1 ELSE 0 END),0),
                coalesce(sum(CASE WHEN json_extract(metadata,'$.source_kind')='owner_note' THEN 1 ELSE 0 END),0)
                FROM messages WHERE owner=?""",(owner,)).fetchone()
            vectors = self.conn.execute("SELECT count(*) FROM vectors WHERE owner=?",(owner,)).fetchone()[0]
            return {"messages":messages,"documents":sum(kinds.values()),"memories":sum(n for k,n in kinds.items() if k!="raw"),
                "vectors":vectors,"by_kind":kinds,"by_status":statuses,
                "original_messages":messages-note_counts[0]-note_counts[1],
                "imported_notes":note_counts[0],"owner_notes":note_counts[1]}

    def maintain(self, owner, now=None, rate=.01, archive_below=.2, archive_days=90):
        stamp=_timestamp(now)
        def finite_parameter(value,name,maximum=None):
            if isinstance(value,bool):raise ValueError("invalid "+name)
            try:result=float(value)
            except (ValueError,TypeError):raise ValueError("invalid "+name) from None
            if not math.isfinite(result) or result<0 or (maximum is not None and result>maximum):
                raise ValueError("invalid "+name)
            return result
        rate=finite_parameter(rate,"decay rate",1)
        threshold=finite_parameter(archive_below,"archive threshold",1)
        days=finite_parameter(archive_days,"archive age")
        result={"now":stamp,"decayed":0,"archived":0,"protected_raw":0,"protected_pinned":0,"deleted_raw":0}
        with self._transaction():
            result["protected_raw"]=self.conn.execute("SELECT count(*) FROM documents WHERE owner=? AND kind='raw'",(owner,)).fetchone()[0]
            for row in self.conn.execute("SELECT * FROM documents WHERE owner=? AND kind!='raw' AND status='active'",(owner,)).fetchall():
                meta=json.loads(row["metadata"])
                try:baseline=float(meta.get("last_decay_at",row["recorded_at"]))
                except (TypeError,ValueError):baseline=float(row["recorded_at"])
                if not math.isfinite(baseline):baseline=float(row["recorded_at"])
                if row["pinned"]:
                    result["protected_pinned"]+=1
                    if stamp>baseline:
                        meta["last_decay_at"]=stamp
                        self.conn.execute("UPDATE documents SET metadata=? WHERE id=?",(_json(meta),row["id"]))
                    continue
                elapsed=max(0,stamp-baseline)/86400
                value=float(row["importance"])*math.exp(-rate*elapsed)
                status=row["status"]
                if value<threshold and (stamp-row["recorded_at"])/86400>=days:
                    status="archived";result["archived"]+=1
                if elapsed>0:
                    meta["last_decay_at"]=stamp
                    if value!=row["importance"]:result["decayed"]+=1
                self.conn.execute("UPDATE documents SET importance=?,status=?,metadata=? WHERE id=?",
                    (value,status,_json(meta),row["id"]))
        return result

    def job_stats(self):
        with self.lock:
            states = dict(self.conn.execute("SELECT status,count(*) FROM jobs GROUP BY status"))
            by_type = {}
            for kind,status,n in self.conn.execute("SELECT kind,status,count(*) FROM jobs GROUP BY kind,status"):
                by_type.setdefault(kind,{})[status] = n
            errors = [dict(r) for r in self.conn.execute("SELECT id,kind,status,attempts,error,updated_at FROM jobs WHERE error!='' ORDER BY updated_at DESC LIMIT 10")]
            return {**{s:states.get(s,0) for s in ("pending","running","retry","waiting_provider","blocked_policy","done","failed")},
                "total":sum(states.values()),"by_status":states,"by_type":by_type,"recent_errors":errors}

    def backup(self, destination):
        destination = Path(destination).expanduser().resolve()
        if self.path != ":memory:" and destination == Path(self.path).resolve():raise ValueError("backup must not overwrite the active database")
        destination.parent.mkdir(parents=True,exist_ok=True)
        if destination.exists():raise ValueError("backup destination already exists")
        with self.lock:
            backup = sqlite3.connect(str(destination))
            try:
                self.conn.backup(backup)
                integrity = backup.execute("PRAGMA integrity_check").fetchone()[0]
            finally:backup.close()
        return {"path":str(destination),"integrity":integrity,"bytes":destination.stat().st_size}

    def export_json(self, owner):
        with self.lock:
            messages=[]
            for row in self.conn.execute("SELECT * FROM messages WHERE owner=? ORDER BY id",(owner,)):
                item=dict(row);item["metadata"]=json.loads(item["metadata"]);messages.append(item)
            return {"schema_version":1,"owner":owner,"exported_at":time.time(),"messages":messages,
                "documents":self.documents(owner,include_archived=True)}

    # Durable worker operations. Lease checks and derived writes share one transaction.
    def park_jobs(self, kind, reason="provider_unavailable"):
        with self._transaction():
            self.conn.execute("""UPDATE jobs SET status='waiting_provider',error=?,updated_at=?
                  WHERE kind=? AND status IN ('pending','retry')""",(reason,time.time(),kind))

    def refresh_policy_jobs(self, import_embedding_is_remote=False):
        condition="""(EXISTS(SELECT 1 FROM messages m WHERE m.id=jobs.message_id
            AND json_extract(m.metadata,'$.disclosure')='never_say')
            OR EXISTS(SELECT 1 FROM documents d WHERE d.id=jobs.document_id AND d.disclosure='never_say'))"""
        if import_embedding_is_remote:
            condition="("+condition+" OR (kind='embed' AND EXISTS(SELECT 1 FROM documents d WHERE d.id=jobs.document_id AND json_extract(d.metadata,'$.import_model_policy')='local_only')))"
        with self._transaction():
            self.conn.execute("UPDATE jobs SET status='blocked_policy',error='policy_model_access_blocked',updated_at=? WHERE status IN ('pending','retry','waiting_provider') AND "+condition,(time.time(),))
            self.conn.execute("UPDATE jobs SET status='pending',error='',available_at=0,updated_at=? WHERE status='blocked_policy' AND NOT "+condition,(time.time(),))

    def _dependency_sources(self, document_id, owner, conversation_id):
        return [(dict(r),0,len(r["text"])) for r in self.conn.execute("""SELECT m.* FROM document_dependencies d
          JOIN messages m ON m.id=d.message_id WHERE d.document_id=? AND m.owner=? AND m.conversation_id=?""",
          (document_id,owner,conversation_id))]

    def claim_jobs(self, kind, limit=1, worker_id=None, lease_seconds=120, grouped=False, group_disclosure=False):
        worker_id = worker_id or str(uuid.uuid4());now=time.time()
        with self._transaction():
            self.conn.execute("""UPDATE jobs SET status='retry',worker_id='',lease_until=0,
                available_at=?,error='lease_expired',updated_at=? WHERE status='running' AND lease_until<=?""",(now,now,now))
            where="kind=? AND status IN ('pending','retry','waiting_provider') AND available_at<=?";args=[kind,now]
            # Historical imports may have thousands of local embedding jobs.
            # Keep live evidence usable first; each priority class remains FIFO.
            order=("CASE WHEN EXISTS(SELECT 1 FROM documents d WHERE d.id=jobs.document_id "
                "AND json_extract(d.metadata,'$.import_model_policy')='local_only') THEN 1 ELSE 0 END,id") if kind=='embed' else "id"
            if grouped:
                first=self.conn.execute("SELECT owner,conversation_id FROM jobs WHERE "+where+" ORDER BY "+order+" LIMIT 1",args).fetchone()
                if not first:return []
                where+=" AND owner=? AND conversation_id=?";args.extend(first)
            args.append(max(1,int(limit)))
            jobs=self.conn.execute("SELECT * FROM jobs WHERE "+where+" ORDER BY "+order+" LIMIT ?",args).fetchall()
            if group_disclosure and jobs:
                def policy(job):
                    row=self.conn.execute("SELECT metadata FROM messages WHERE id=?",(job["message_id"],)).fetchone()
                    metadata=json.loads(row[0]) if row else {}
                    return metadata.get("disclosure","normal"),tuple(_topics(metadata.get("disclosure_topics")))
                first_policy=policy(jobs[0])
                jobs=[j for j in jobs if policy(j)==first_policy]
            for job in jobs:
                self.conn.execute("""UPDATE jobs SET status='running',worker_id=?,lease_until=?,attempts=attempts+1,
                    updated_at=?,error='' WHERE id=?""",(worker_id,now+lease_seconds,now,job["id"]))
            return [dict(self.conn.execute("SELECT * FROM jobs WHERE id=?",(j["id"],)).fetchone()) for j in jobs]

    def _verify_claim(self, job_ids, worker_id):
        if not job_ids:raise JobLeaseLost("no claimed work")
        now=time.time()
        for jid in job_ids:
            row=self.conn.execute("SELECT status,worker_id,lease_until FROM jobs WHERE id=?",(jid,)).fetchone()
            if not row or row["status"]!="running" or row["worker_id"]!=worker_id or row["lease_until"]<=now:
                raise JobLeaseLost("worker lease is no longer valid")

    def claim_derivation_jobs(self, limit=12, worker_id=None, lease_seconds=120,
                              min_batch=12, max_wait_seconds=900, force=False, now=None):
        """Choose an eligible *complete policy group*, then atomically claim it.

        Age is time spent in this durable queue, not a historical utterance date.
        An unrelated small/blocked group cannot hold up a ready conversation.
        Retry work is already part of a scheduled batch and need not fill again.
        """
        stamp=_timestamp(now);worker_id=worker_id or str(uuid.uuid4())
        cap=max(1,min(128,int(limit)));minimum=max(1,min(cap,int(min_batch)))
        with self._transaction():
            self.conn.execute("""UPDATE jobs SET status='retry',worker_id='',lease_until=0,
                available_at=?,error='lease_expired',updated_at=? WHERE status='running' AND lease_until<=?""",
                (stamp,stamp,stamp))
            rows=self.conn.execute("""SELECT j.*,m.metadata AS source_metadata FROM jobs j
                JOIN messages m ON m.id=j.message_id AND m.owner=j.owner AND m.conversation_id=j.conversation_id
                WHERE j.kind='derive' AND j.status IN ('pending','retry','waiting_provider')
                  AND j.available_at<=? ORDER BY j.id""",(stamp,)).fetchall()
            groups={}
            for row in rows:
                meta=json.loads(row['source_metadata'])
                if meta.get('disclosure','normal')=='never_say':continue
                policy=(row['owner'],row['conversation_id'],meta.get('disclosure','normal'),
                        tuple(_topics(meta.get('disclosure_topics'))))
                groups.setdefault(policy,[]).append(row)
            selected=[]
            for group in groups.values():
                oldest=min(r['created_at'] for r in group)
                due=force or len(group)>=minimum or stamp-oldest>=max(0,float(max_wait_seconds)) or any(r['attempts'] for r in group)
                if due:
                    # A restart must not reassemble a batch already split for output limits.
                    effective=min(cap,max(1,min(int(r['batch_limit']) for r in group)))
                    selected=group[:effective];break
            for row in selected:
                self.conn.execute("""UPDATE jobs SET status='running',worker_id=?,lease_until=?,
                  attempts=attempts+1,updated_at=?,error='' WHERE id=?""",
                  (worker_id,stamp+lease_seconds,stamp,row['id']))
            return [dict(self.conn.execute('SELECT * FROM jobs WHERE id=?',(r['id'],)).fetchone()) for r in selected]

    def renew_jobs(self, jobs, worker_id, lease_seconds=120):
        with self._transaction():
            ids=[j['id'] for j in jobs];self._verify_claim(ids,worker_id)
            until=time.time()+lease_seconds
            self.conn.executemany('UPDATE jobs SET lease_until=? WHERE id=? AND worker_id=?',
                [(until,jid,worker_id) for jid in ids])

    def limit_job_batch(self, jobs, worker_id, limit):
        with self._transaction():
            ids=[j['id'] for j in jobs];self._verify_claim(ids,worker_id)
            self.conn.executemany('UPDATE jobs SET batch_limit=min(batch_limit,?) WHERE id=? AND worker_id=?',
                [(max(1,int(limit)),jid,worker_id) for jid in ids])

    def release_jobs(self, jobs, worker_id, reason='derivation_paused'):
        with self._transaction():
            now=time.time()
            for j in jobs:
                # A late old worker cannot release a new worker's claim or an expired lease.
                self.conn.execute("""UPDATE jobs SET status='pending',worker_id='',lease_until=0,
                  available_at=?,updated_at=?,error=? WHERE id=? AND worker_id=? AND status='running' AND lease_until>?""",
                  (now,now,reason,j['id'],worker_id,now))

    def record_model_attempt(self, owner, event):
        """Persist metering metadata only; never prompts, responses, URLs or credentials."""
        owner=_identifier(owner,'owner',256)
        outcomes={'success','output_truncated','invalid_json','empty','transport','provider_http','provider_error'}
        if not isinstance(event,dict) or event.get('outcome') not in outcomes:raise ValueError('invalid usage outcome')
        ident=_identifier(event.get('attempt_id'),'attempt_id',128)
        started=_timestamp(event.get('started_at'))
        safe={'attempt_id':ident,'model':_identifier(event.get('model'),'model',256),
              'started_at':started,'outcome':event['outcome']}
        elapsed=event.get('elapsed_ms')
        if isinstance(elapsed,bool) or not isinstance(elapsed,(int,float)) or not math.isfinite(elapsed) or elapsed<0:
            raise ValueError('invalid attempt duration')
        safe['elapsed_ms']=elapsed
        safe['error']=event.get('error') if event.get('error') in outcomes else None
        safe['finish_reason']=event.get('finish_reason') if event.get('finish_reason') in ('stop','length','max_tokens','max_output_tokens','content_filter','tool_calls','function_call','other') else None
        status=event.get('http_status');safe['http_status']=status if type(status) is int and 100<=status<=599 else None
        usage=event.get('usage');safe['usage']=None
        if isinstance(usage,dict):
            safe['usage']={}
            for key in ('input_tokens','output_tokens','cache_hit_tokens','cache_miss_tokens','total_tokens'):
                v=usage.get(key)
                safe['usage'][key]=v if not isinstance(v,bool) and isinstance(v,(int,float)) and math.isfinite(v) and v>=0 else None
            if not any(v is not None for v in safe['usage'].values()):safe['usage']=None
        safe['usage_known']=safe['usage'] is not None
        context=event.get('request_context') or {};safe['request_context']={}
        for key in ('run_id','batch_id','parent_batch_id','root_batch_id','mode'):
            value=context.get(key)
            if isinstance(value,str) and 0<len(value)<=128 and all(c.isascii() and (c.isalnum() or c in '_.:-') for c in value):safe['request_context'][key]=value
        for key in ('message_count','job_count','chunk_count','split_depth'):
            value=context.get(key)
            if type(value) is int and value>=0:safe['request_context'][key]=value
        ids=context.get('job_ids')
        if isinstance(ids,list) and len(ids)<=128 and all(type(v) is int and v>=0 for v in ids):safe['request_context']['job_ids']=ids
        payload=_json(safe)
        with self._transaction():
            old=self.conn.execute('SELECT owner,event_json FROM model_attempts WHERE attempt_id=?',(ident,)).fetchone()
            if old:
                if old['owner']!=owner or old['event_json']!=payload:raise ValueError('attempt id already has different metadata')
                return {'recorded':False,'duplicate':True}
            self.conn.execute('INSERT INTO model_attempts VALUES(?,?,?,?,?)',(ident,owner,started,event['outcome'],payload))
        return {'recorded':True,'duplicate':False}

    def model_usage(self, owner):
        with self.lock:
            count=self.conn.execute('SELECT count(*),min(started_at) FROM model_attempts WHERE owner=?',(owner,)).fetchone()
            reported=self.conn.execute("SELECT count(*) FROM model_attempts WHERE owner=? AND json_extract(event_json,'$.usage_known')=1",(owner,)).fetchone()[0]
            result={'attempts':count[0],'reported_responses':reported,'unknown_usage_attempts':count[0]-reported,
                    'since':count[1],'scope':'background_derivation_only','legacy_usage_included':False,
                    'by_outcome':dict(self.conn.execute('SELECT outcome,count(*) FROM model_attempts WHERE owner=? GROUP BY outcome',(owner,))),
                    'missing_fields':{}}
            for key in ('input_tokens','output_tokens','cache_hit_tokens','cache_miss_tokens','total_tokens'):
                row=self.conn.execute("SELECT sum(json_extract(event_json,?)),count(json_extract(event_json,?)) FROM model_attempts WHERE owner=?",
                    ('$.usage.'+key,'$.usage.'+key,owner)).fetchone()
                result[key]=row[0];result['missing_fields'][key]=count[0]-row[1]
            return result

    def _finish_claim(self, job_ids, worker_id):
        self.conn.executemany("UPDATE jobs SET status='done',worker_id='',lease_until=0,error='',updated_at=? WHERE id=? AND worker_id=?",
            [(time.time(),jid,worker_id) for jid in job_ids])

    def complete_derivation(self, jobs, worker_id, documents):
        ids=[j["id"] for j in jobs]
        owner,conversation=jobs[0]["owner"],jobs[0]["conversation_id"]
        if any(j["kind"]!="derive" or j["owner"]!=owner or j["conversation_id"]!=conversation for j in jobs):
            raise ValueError("derivation jobs must share an owner and conversation")
        allowed={j["message_id"] for j in jobs}
        with self._transaction():
            self._verify_claim(ids,worker_id)
            results=[]
            for doc in documents:
                if any(r.get("message_id") not in allowed for r in doc.get("source_refs",[])):
                    raise ValueError("model referenced a message outside its assigned batch")
                results.append(self._add_derived(owner,conversation,**doc,dependency_message_ids=sorted(allowed)))
            self._finish_claim(ids,worker_id)
            return results

    def complete_vectors(self, jobs, worker_id, vectors, model):
        if len(jobs)!=len(vectors):raise ValueError("embedding count does not match claimed documents")
        with self._transaction():
            self._verify_claim([j["id"] for j in jobs],worker_id)
            for job,vector in zip(jobs,vectors):
                if job["kind"]!="embed":raise ValueError("not an embedding job")
                self._set_vector(job["owner"],job["document_id"],vector,model)
            self._finish_claim([j["id"] for j in jobs],worker_id)

    def fail_jobs(self, jobs, worker_id, error, max_attempts=8):
        with self._transaction():
            for job in jobs:
                row=self.conn.execute("SELECT * FROM jobs WHERE id=? AND worker_id=? AND status='running'",(job["id"],worker_id)).fetchone()
                if not row:continue
                status="failed" if row["attempts"]>=max_attempts else "retry"
                delay=min(300,2**min(row["attempts"],8))
                self.conn.execute("""UPDATE jobs SET status=?,worker_id='',lease_until=0,error=?,
                     available_at=?,updated_at=? WHERE id=? AND worker_id=?""",
                     (status,str(error)[:120],time.time()+delay,time.time(),job["id"],worker_id))

    def retry_failed_jobs(self, kind=None):
        with self._transaction():
            where="status='failed'";args=[]
            if kind:where+=" AND kind=?";args.append(kind)
            cur=self.conn.execute("UPDATE jobs SET status='pending',attempts=0,error='',available_at=0 WHERE "+where,args)
            return cur.rowcount
