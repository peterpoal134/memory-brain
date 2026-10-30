# -*- coding: utf-8 -*-
"""独立记忆服务：不依赖 AstrBot，任何桥/窗口都能用 HTTP 调。

启动：
    MEMORY_DB=/path/brain.db MEMORY_TOKEN=xxx python -m memory_core.server

接口：
    POST /remember   写入一条记忆
    POST /recall     召回（返回可直接拼进 system prompt 的文本）
    POST /decay      每日衰减
    POST /archive    归档
    GET  /stats      统计
    GET  /health     体检
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import numpy as np

from .agent import AgentService
from .compress import ContextCompressor
from .eval import EvalService
from .life import LifeService
from .maintenance import MaintenanceService
from .engine import MemoryEngine
from .store import Store

DB = os.environ.get("MEMORY_DB", str(Path.home() / ".memory_core" / "brain.db"))
HOST = os.environ.get("MEMORY_HOST", "127.0.0.1")
PORT = int(os.environ.get("MEMORY_PORT", "8780"))
TOKEN = os.environ.get("MEMORY_TOKEN", "")
EMBED_URL = os.environ.get("MEMORY_EMBED_URL", "")
EMBED_MODEL = os.environ.get("MEMORY_EMBED_MODEL", "bge-m3")
LLM_BASE = os.environ.get("MEMORY_LLM_BASE", "").rstrip("/")
LLM_KEY = os.environ.get("MEMORY_LLM_KEY", "")
LLM_MODEL = os.environ.get("MEMORY_LLM_MODEL", "deepseek-chat")
AUTO_REMEMBER = os.environ.get("MEMORY_AUTO_REMEMBER", "1") == "1"
SUMMARY_MEMORY = os.environ.get("MEMORY_SUMMARY_MEMORY", "1") == "1"
COMPRESS = os.environ.get("MEMORY_COMPRESS", "1") != "0"
COMPRESS_CHARS = int(os.environ.get("MEMORY_COMPRESS_CHARS", "20000"))
COMPRESS_KEEP = int(os.environ.get("MEMORY_COMPRESS_KEEP", "10"))
COMPRESS_TURNS = int(os.environ.get("MEMORY_COMPRESS_TURNS", "24"))
COMPRESS_RATIO = float(os.environ.get("MEMORY_COMPRESS_KEEP_RATIO", "0.15"))
RAW_KEEP = int(os.environ.get("MEMORY_RAW_KEEP", "2000"))
BATCH_SIZE = int(os.environ.get("MEMORY_BATCH_SIZE", "12"))


_EMBED_DOWN_UNTIL = 0.0


def _embed(text: str):
    global _EMBED_DOWN_UNTIL
    if not EMBED_URL or time.time() < _EMBED_DOWN_UNTIL:
        return None
    try:
        body = json.dumps({"model": EMBED_MODEL, "input": text}).encode()
        req = urllib.request.Request(
            EMBED_URL, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            d = json.loads(r.read().decode())
        return np.asarray((d.get("embeddings") or [d["embedding"]])[0], dtype=np.float32)
    except Exception:
        _EMBED_DOWN_UNTIL = time.time() + 300
        return None


def _locked(fn):
    def wrapper(self, *args, **kwargs):
        with self.lock:
            return fn(self, *args, **kwargs)

    return wrapper


class MemoryService:
    def __init__(self, db: str | None = None, embed=True):
        self.store = Store(Path(db or DB))
        self.engine = MemoryEngine(self.store, embed=_embed if embed else None)
        self.lock = threading.RLock()
        self.store.conn.executescript(
            """CREATE TABLE IF NOT EXISTS raw_messages (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   user_id TEXT NOT NULL,
                   session_id TEXT NOT NULL,
                   role TEXT NOT NULL,
                   text TEXT NOT NULL,
                   created_at REAL NOT NULL
               );
               CREATE TABLE IF NOT EXISTS chat_batch_state (
                   session TEXT PRIMARY KEY,
                   last_id INTEGER NOT NULL
               );"""
        )
        self.store.conn.execute(
            """CREATE TABLE IF NOT EXISTS reminders (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   user_id TEXT NOT NULL,
                   text TEXT NOT NULL,
                   due_at REAL NOT NULL,
                   done INTEGER NOT NULL DEFAULT 0,
                   created_at REAL NOT NULL,
                   meta TEXT NOT NULL DEFAULT '{}'
               )"""
        )
        self.store.conn.commit()

    @_locked
    def remember(self, data: dict) -> dict:
        text = str(data.get("text") or "").strip()
        if not text:
            raise ValueError("text 不能为空")
        entities = []
        for e in data.get("entities") or []:
            if isinstance(e, (list, tuple)) and len(e) >= 1:
                entities.append((str(e[0]), str(e[1]) if len(e) > 1 else "thing"))
        mid = self.engine.remember(
            text,
            kind=str(data.get("kind") or "episode"),
            importance=float(data.get("importance") or 0.5),
            valence=float(data.get("valence") or 0),
            arousal=float(data.get("arousal") or 0),
            source=str(data.get("source") or "http"),
            meta=data.get("meta") or {},
            entities=entities,
            visibility=str(data.get("visibility") or "public"),
        )
        return {"id": mid}

    @_locked
    def recall(self, data: dict) -> dict:
        q = str(data.get("query") or "").strip()
        if not q:
            return {"hits": [], "block": ""}
        vis = set(data.get("visibility") or ["public", "private", "group"])
        hits = self.engine.recall(
            q,
            k=int(data.get("k") or 5),
            rerank=bool(data.get("rerank", True)),
            visibility_allow=vis,
            mood_valence=data.get("mood_valence"),
        )
        out = []
        for h in hits:
            out.append({
                "id": h.get("id"),
                "text": h.get("text"),
                "kind": h.get("kind"),
                "score": round(float(h.get("score") or 0), 4),
                "importance": round(float(h.get("importance") or 0), 4),
                "visibility": h.get("visibility"),
                "created_at": h.get("created_at"),
            })
        lines = []
        for i, h in enumerate(out, 1):
            day = time.strftime("%Y-%m-%d", time.localtime(float(h.get("created_at") or time.time())))
            lines.append(f"{i}. [{day}] {h['text']}")
        block = "\n".join(lines)
        return {"hits": out, "block": block}

    @_locked
    def graph(self) -> dict:
        raw = self.store.graph(limit=240)
        nodes = [
            {"id": int(n["id"]), "name": n["name"], "type": n["type"], "degree": int(n["degree"])}
            for n in raw["nodes"]
        ]
        keep = {n["id"] for n in nodes}
        edges = []
        for e in raw["edges"]:
            if int(e["src_id"]) in keep and int(e["dst_id"]) in keep:
                edges.append({"src": int(e["src_id"]), "dst": int(e["dst_id"]),
                              "kind": "relation", "label": e["relation"], "weight": float(e["weight"])})
        for e in self.store.cooccurrence_edges(limit=1200):
            if e["src"] in keep and e["dst"] in keep:
                edges.append({"src": e["src"], "dst": e["dst"], "kind": "cooccur", "weight": float(e["weight"])})
        rows = self.store.conn.execute(
            """SELECT m.id, m.text, m.kind, m.importance, m.created_at,
                      GROUP_CONCAT(me.entity_id) AS ents
               FROM memories m
               LEFT JOIN memory_entities me ON me.memory_id = m.id
               WHERE m.status='active'
               GROUP BY m.id ORDER BY m.created_at DESC LIMIT 300"""
        ).fetchall()
        memories = [{
            "id": int(r["id"]), "text": str(r["text"])[:120], "kind": r["kind"],
            "importance": round(float(r["importance"] or 0), 2),
            "created_at": float(r["created_at"]),
            "entities": [int(x) for x in str(r["ents"] or "").split(",") if x.strip()],
        } for r in rows]
        return {"nodes": nodes, "edges": edges, "memories": memories}

    def complete(self, prompt: str, system: str = "", max_tokens: int = 500) -> str:
        if not LLM_BASE:
            return ""
        payload = {
            "model": LLM_MODEL,
            "messages": ([{"role": "system", "content": system}] if system else []) +
                        [{"role": "user", "content": str(prompt or "")[:16000]}],
            "temperature": 0.3,
            "max_tokens": int(max_tokens),
        }
        req = urllib.request.Request(
            LLM_BASE + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {LLM_KEY}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            out = json.loads(r.read().decode())
        return str(out["choices"][0]["message"]["content"] or "")

    def summarize(self, text: str) -> str:
        """用上游模型压缩一段旧对话；不注入记忆，避免递归。"""
        if not LLM_BASE:
            return ""
        payload = {
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": "把下面的旧对话压缩成 200 字以内的摘要，保留人物、事实、约定和未完成的事。只输出摘要。"},
                {"role": "user", "content": str(text or "")[:16000]},
            ],
            "temperature": 0.2,
            "max_tokens": 320,
        }
        req = urllib.request.Request(
            LLM_BASE + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {LLM_KEY}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            out = json.loads(r.read().decode())
        return str(out["choices"][0]["message"]["content"] or "")

    @_locked
    def remember_turn(self, user_id: str, session_id: str, role: str, text: str) -> None:
        """混合模式：原文进有上限的 raw 缓冲，攒够一批再总结成一条长期记忆。"""
        text = str(text or "").strip()
        if not text:
            return
        uid = str(user_id or "default")
        sid = str(session_id or "private")
        cur = self.store.conn.execute(
            "INSERT INTO raw_messages(session_id,role,sender,text,created_at) VALUES(?,?,?,?,?)",
            (sid, str(role or "user"), uid, text[:4000], time.time()),
        )
        self.store.conn.commit()
        self.store.conn.execute(
            "DELETE FROM raw_messages WHERE id NOT IN (SELECT id FROM raw_messages ORDER BY id DESC LIMIT ?)",
            (int(RAW_KEEP),),
        )
        self.store.conn.commit()
        key = f"{uid}:{sid}"
        row = self.store.conn.execute("SELECT last_id FROM chat_batch_state WHERE session=?", (key,)).fetchone()
        last_id = int(row[0]) if row else 0
        rows = self.store.conn.execute(
            "SELECT id,role,text FROM raw_messages WHERE sender=? AND session_id=? AND id>? ORDER BY id ASC LIMIT ?",
            (uid, sid, last_id, int(BATCH_SIZE)),
        ).fetchall()
        if len(rows) < int(BATCH_SIZE):
            return
        body = "\n".join(f"{r['role']}: {r['text']}" for r in rows)
        try:
            summary = str(self.summarize(body) or "").strip()
        except Exception:
            summary = ""
        if not summary:
            summary = body[:600]
        vis = "group" if "group" in sid else "private"
        self.remember({
            "text": "[对话批次] " + summary, "kind": "episode", "importance": 0.45,
            "visibility": vis, "source": "chat_batch",
            "meta": {"session": sid, "count": len(rows)},
        })
        if os.environ.get("MEMORY_DIGEST", "1") == "1" and LLM_BASE:
            try:
                from .digest import DigestService
                DigestService(self).extract(summary, visibility=vis, source="digest")
            except Exception:
                pass
        self.store.conn.execute(
            "INSERT INTO chat_batch_state(session,last_id) VALUES(?,?) "
            "ON CONFLICT(session) DO UPDATE SET last_id=excluded.last_id",
            (key, int(rows[-1]["id"])),
        )
        self.store.conn.commit()

    @_locked
    def add_reminder(self, data: dict) -> dict:
        text = str(data.get("text") or "").strip()
        due = float(data.get("due_at") or 0)
        if not text or due <= 0:
            raise ValueError("text / due_at 不能为空")
        cur = self.store.conn.execute(
            "INSERT INTO reminders(user_id,text,due_at,created_at,meta) VALUES(?,?,?,?,?)",
            (str(data.get("user_id") or "default"), text, due, time.time(),
             json.dumps(data.get("meta") or {}, ensure_ascii=False)),
        )
        self.store.conn.commit()
        return {"id": int(cur.lastrowid)}

    @_locked
    def due_reminders(self, user_id: str = "") -> list[dict]:
        sql = "SELECT * FROM reminders WHERE done=0 AND due_at<=?"
        args = [time.time()]
        if user_id:
            sql += " AND user_id=?"
            args.append(user_id)
        rows = self.store.conn.execute(sql + " ORDER BY due_at ASC LIMIT 50", args).fetchall()
        return [{"id": int(r["id"]), "user_id": r["user_id"], "text": r["text"],
                 "due_at": float(r["due_at"]), "meta": json.loads(r["meta"] or "{}")} for r in rows]

    @_locked
    def done_reminder(self, rid: int) -> bool:
        cur = self.store.conn.execute("UPDATE reminders SET done=1 WHERE id=?", (int(rid),))
        self.store.conn.commit()
        return cur.rowcount > 0

    @_locked
    def stats(self) -> dict:
        s = self.store.stats()
        s["avg_importance"] = round(self.engine._avg_importance(), 3)
        return s

    def _chat_payload(self, data: dict) -> dict:
        if not LLM_BASE:
            raise RuntimeError("没有配置 MEMORY_LLM_BASE")
        msgs = list(data.get("messages") or [])
        user = ""
        for m in reversed(msgs):
            if m.get("role") == "user":
                user = str(m.get("content") or "")
                break
        block = "" if data.get("_skip_recall") else (self.recall({"query": user, "k": 5}).get("block") if user else "")
        if block:
            memory_system = "以下是你记得的旧事，用来接话，不要逐条复述：\n" + block
            if msgs and msgs[0].get("role") == "system":
                msgs = list(msgs)
                msgs[0] = dict(msgs[0])
                msgs[0]["content"] = memory_system + "\n\n" + str(msgs[0].get("content") or "")
            else:
                msgs = [{"role": "system", "content": memory_system}] + msgs
        payload = {
            "model": data.get("model") or LLM_MODEL,
            "messages": msgs,
            "temperature": data.get("temperature", 0.7),
        }
        for key in ("top_p", "stop", "presence_penalty", "frequency_penalty",
                    "response_format", "seed", "tools", "tool_choice", "parallel_tool_calls"):
            if key in data:
                payload[key] = data[key]
        if data.get("max_tokens"):
            payload["max_tokens"] = int(data["max_tokens"])
        elif data.get("max_completion_tokens"):
            payload["max_tokens"] = int(data["max_completion_tokens"])
        return payload

    def chat(self, data: dict) -> dict:
        """OpenAI 兼容入口：自动召回并拼进 system，再转发给上游模型。"""
        payload = self._chat_payload(data)
        user = ""
        for m in payload["messages"]:
            if m.get("role") == "user":
                user = str(m.get("content") or "")
        req = urllib.request.Request(
            LLM_BASE + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {LLM_KEY}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            out = json.loads(r.read().decode())
        if AUTO_REMEMBER and user and data.get("auto_remember", True):
            vis = "group" if "group" in str(data.get("session_id") or "") else "private"
            try:
                uid = str(data.get("user") or "default")
                sid = str(data.get("session_id") or ("group" if vis == "group" else "private"))
                self.remember_turn(uid, sid, "user", user)
                reply = ""
                try:
                    reply = str(out["choices"][0]["message"]["content"] or "")
                except Exception:
                    pass
                if reply:
                    self.remember_turn(uid, sid, "assistant", reply)
            except Exception:
                pass
        return out

    def stream_chat(self, data: dict):
        """真流式：把上游 SSE 逐块转给客户端。"""
        from http.client import HTTPConnection, HTTPSConnection
        from urllib.parse import urlparse

        payload = self._chat_payload(data)
        payload["stream"] = True
        u = urlparse(LLM_BASE + "/chat/completions")
        cls = HTTPSConnection if u.scheme == "https" else HTTPConnection
        conn = cls(u.hostname, u.port or (443 if u.scheme == "https" else 80), timeout=180)
        path = u.path + (("?" + u.query) if u.query else "")
        conn.request("POST", path, json.dumps(payload, ensure_ascii=False).encode(),
                     {"Content-Type": "application/json", "Authorization": f"Bearer {LLM_KEY}"})
        resp = conn.getresponse()
        if resp.status != 200:
            body = resp.read().decode(errors="replace")
            conn.close()
            raise RuntimeError(f"upstream {resp.status}: {body[:200]}")
        try:
            while True:
                chunk = resp.read(2048)
                if not chunk:
                    break
                yield chunk
        finally:
            conn.close()

    @_locked
    def report(self) -> str:
        from . import health

        return health.report(self.store)

    @_locked
    def decay(self, data: dict) -> dict:
        return {"affected": self.engine.decay(
            float(data.get("rate") or 0.01),
            protect_names=data.get("protect_names") or [],
            core_floor=float(data.get("core_floor") or 0.45),
            emotion_factor=float(data.get("emotion_factor") or 0.4),
        )}

    @_locked
    def archive(self, data: dict) -> dict:
        return {"archived": self.engine.archive(
            importance_below=float(data.get("importance_below") or 0.3),
            older_than_days=float(data.get("older_than_days") or 30),
            protect_days=float(data.get("protect_days") or 30),
        )}


SERVICE: MemoryService | None = None
AGENT: AgentService | None = None
LIFE: LifeService | None = None
EVAL: EvalService | None = None
MAINT: MaintenanceService | None = None
COMPRESSOR: ContextCompressor | None = None


def service() -> MemoryService:
    global SERVICE
    if SERVICE is None:
        SERVICE = MemoryService()
    return SERVICE


def agent() -> AgentService:
    global AGENT
    if AGENT is None:
        AGENT = AgentService(service(), os.environ.get("AGENT_CONFIG"))
    return AGENT


def compressor() -> ContextCompressor:
    global COMPRESSOR
    if COMPRESSOR is None:
        COMPRESSOR = ContextCompressor(service(), COMPRESS_CHARS, COMPRESS_KEEP,
                                       COMPRESS_TURNS, COMPRESS_RATIO, SUMMARY_MEMORY)
    return COMPRESSOR


def maintenance() -> MaintenanceService:
    global MAINT
    if MAINT is None:
        MAINT = MaintenanceService(service())
    return MAINT


def eval_service() -> EvalService:
    global EVAL
    if EVAL is None:
        EVAL = EvalService(service(), os.environ.get("MEMORY_EVAL"))
    return EVAL


def life() -> LifeService:
    global LIFE
    if LIFE is None:
        LIFE = LifeService(service(), os.environ.get("LIFE_CONFIG") or os.environ.get("AGENT_CONFIG"))
    return LIFE


def openai_agent(data: dict) -> dict:
    msgs = data.get("messages") or []
    if COMPRESS and msgs:
        base = next((str(m.get("content") or "") for m in msgs if m.get("role") == "system"), "")
        msgs = compressor().compress(msgs, ContextCompressor.key_for(str(data.get("user") or "default"), base), visibility="group" if "group" in str(data.get("session_id") or "") else "private")
    text = ""
    for m in reversed(msgs):
        if m.get("role") == "user":
            text = str(m.get("content") or "")
            break
    msgs = data.get("messages") or []
    base_system = "\n\n".join(str(m.get("content") or "") for m in msgs if m.get("role") == "system")
    history = []
    last_user = -1
    for i, m in enumerate(msgs):
        if m.get("role") == "user":
            last_user = i
    for i, m in enumerate(msgs):
        if i != last_user and m.get("role") in ("user", "assistant") and m.get("content"):
            history.append({"role": m["role"], "content": m["content"]})
    uid = str(data.get("user") or "default")
    out = agent().chat({"message": text, "user_id": uid,
                        "session_id": str(data.get("session_id") or ""),
                        "model": data.get("model"), "temperature": data.get("temperature", 0.7),
                        "max_tokens": data.get("max_tokens"),
                        "base_system": base_system,
                        "keep_system": os.environ.get("MEMORY_AGENT_KEEP_SYSTEM") == "1",
                        "history": history})
    return {"id": "chatcmpl-agent", "object": "chat.completion", "created": int(time.time()),
            "model": data.get("model") or "agent-core",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": out["reply"]}, "finish_reason": "stop"}],
            "agent_state": out.get("state")}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("[memory-core] " + (fmt % args), flush=True)

    def _send(self, code: int, obj: Any):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _auth(self) -> bool:
        if not TOKEN:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {TOKEN}"

    def do_OPTIONS(self):
        self._send(200, {"ok": True})

    def do_GET(self):
        if not self._auth():
            self._send(401, {"status": "error", "message": "unauthorized"})
            return
        if self.path == "/panel":
            try:
                body = (Path(__file__).resolve().parent / "panel.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                self._send(500, {"status": "error", "message": str(exc)})
        elif self.path == "/graph":
            try:
                self._send(200, {"status": "ok", "data": service().graph()})
            except Exception as exc:
                self._send(500, {"status": "error", "message": str(exc)})
        elif self.path == "/conflicts":
            try:
                self._send(200, {"status": "ok", "data": {"conflicts": maintenance().conflicts()}})
            except Exception as exc:
                self._send(500, {"status": "error", "message": str(exc)})
        elif self.path == "/eval":
            try:
                self._send(200, {"status": "ok", "data": eval_service().latest()})
            except Exception as exc:
                self._send(500, {"status": "error", "message": str(exc)})
        elif self.path == "/eval/queries":
            try:
                self._send(200, {"status": "ok", "data": {"queries": eval_service().queries()}})
            except Exception as exc:
                self._send(500, {"status": "error", "message": str(exc)})
        elif self.path == "/life/state":
            try:
                self._send(200, {"status": "ok", "data": life().state()})
            except Exception as exc:
                self._send(500, {"status": "error", "message": str(exc)})
        elif self.path.startswith("/life/events"):
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            try:
                after = int((q.get("after") or ["0"])[0])
                pending = (q.get("pending") or ["0"])[0] == "1"
                self._send(200, {"status": "ok", "data": life().events(after, include_delivered=not pending)})
            except Exception as exc:
                self._send(500, {"status": "error", "message": str(exc)})
        elif self.path.startswith("/reminders/due"):
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            try:
                self._send(200, {"status": "ok", "data": service().due_reminders(str((q.get("user_id") or [""])[0]))})
            except Exception as exc:
                self._send(500, {"status": "error", "message": str(exc)})
        elif self.path.startswith("/agent/state"):
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            try:
                self._send(200, {"status": "ok", "data": agent().state(str((q.get("user_id") or ["default"])[0]))})
            except Exception as exc:
                self._send(500, {"status": "error", "message": str(exc)})
        elif self.path == "/agent/config":
            try:
                c = agent().cfg
                self._send(200, {"status": "ok", "data": {"name": (c.get("character") or {}).get("name"),
                                                          "emotion_rules": [r.get("label") for r in (c.get("emotion") or {}).get("rules") or []]}})
            except Exception as exc:
                self._send(500, {"status": "error", "message": str(exc)})
        elif self.path == "/v1/models":
            self._send(200, {"object": "list", "data": [{"id": "memory-core", "object": "model"}]})
        elif self.path == "/health":
            try:
                self._send(200, {"status": "ok", "report": service().report()})
            except Exception as exc:
                self._send(500, {"status": "error", "message": str(exc)})
        elif self.path == "/stats":
            try:
                self._send(200, {"status": "ok", "data": service().stats()})
            except Exception as exc:
                self._send(500, {"status": "error", "message": str(exc)})
        else:
            self._send(404, {"status": "error", "message": "not found"})

    def do_POST(self):
        if not self._auth():
            self._send(401, {"status": "error", "message": "unauthorized"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 8 * 1024 * 1024:
                self._send(413, {"status": "error", "message": "body too large"})
                return
            data = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception as exc:
            self._send(400, {"status": "error", "message": f"bad json: {exc}"})
            return
        try:
            if self.path == "/maintenance/run":
                out = maintenance().run(float(data.get("similarity") or 0.92))
                self._send(200, {"status": "ok", "data": out})
            elif self.path == "/conflicts/resolve":
                ok = maintenance().resolve(int(data.get("id") or 0), str(data.get("keep") or "a"))
                self._send(200, {"status": "ok", "data": {"done": ok}})
            elif self.path == "/digest/extract":
                from .digest import DigestService
                self._send(200, {"status": "ok", "data": DigestService(service()).extract(str(data.get("text") or ""), str(data.get("visibility") or "private"))})
            elif self.path == "/digest/reflect":
                from .digest import DigestService
                self._send(200, {"status": "ok", "data": DigestService(service()).reflect(data.get("date"), str(data.get("visibility") or "private"))})
            elif self.path == "/digest/profile":
                from .digest import DigestService
                self._send(200, {"status": "ok", "data": DigestService(service()).profile(str(data.get("name") or ""), str(data.get("visibility") or "private"))})
            elif self.path == "/eval/run":
                self._send(200, {"status": "ok", "data": eval_service().run()})
            elif self.path == "/eval/query":
                self._send(200, {"status": "ok", "data": eval_service().add_query(data)})
            elif self.path == "/life/step":
                with service().lock:
                    out = life().step(data.get("force") or None)
                self._send(200, {"status": "ok", "data": out})
            elif self.path == "/life/ack":
                self._send(200, {"status": "ok", "data": {"done": life().ack(int(data.get("id") or 0))}})
            elif self.path == "/v1/agent/chat/completions":
                self._send(200, openai_agent(data))
            elif self.path == "/agent/chat":
                self._send(200, {"status": "ok", "data": agent().chat(data)})
            elif self.path == "/v1/chat/completions":
                if os.environ.get("MEMORY_AGENT") == "1":
                    out = openai_agent(data)
                    if data.get("stream"):
                        c = out["choices"][0]["message"]["content"]
                        chunk = {"id": out["id"], "object": "chat.completion.chunk", "created": out["created"],
                                 "model": out["model"], "choices": [{"index": 0, "delta": {"role": "assistant", "content": c}, "finish_reason": None}]}
                        body = ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\ndata: [DONE]\n\n").encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.end_headers()
                        self.wfile.write(body)
                    else:
                        self._send(200, out)
                elif data.get("stream"):
                    try:
                        gen = service().stream_chat(data)
                        first = next(gen)
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                        self.send_header("Cache-Control", "no-cache")
                        self.send_header("Connection", "close")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.end_headers()
                        self.close_connection = True
                        if first:
                            self.wfile.write(first)
                        for chunk in gen:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        self.wfile.flush()
                    except StopIteration:
                        pass
                    except Exception as exc:
                        try:
                            self._send(500, {"status": "error", "message": str(exc)})
                        except Exception:
                            pass
                else:
                    if COMPRESS and data.get("messages"):
                        data = dict(data)
                        base = next((str(m.get("content") or "") for m in data["messages"] if m.get("role") == "system"), "")
                        data["messages"] = compressor().compress(data["messages"], ContextCompressor.key_for(str(data.get("user") or "default"), base), visibility="group" if "group" in str(data.get("session_id") or "") else "private")
                    self._send(200, service().chat(data))
            elif self.path == "/reminder":
                self._send(200, {"status": "ok", "data": service().add_reminder(data)})
            elif self.path == "/reminder/done":
                ok = service().done_reminder(int(data.get("id") or 0))
                self._send(200, {"status": "ok", "data": {"done": ok}})
            elif self.path == "/remember":
                self._send(200, {"status": "ok", "data": service().remember(data)})
            elif self.path == "/recall":
                self._send(200, {"status": "ok", "data": service().recall(data)})
            elif self.path == "/decay":
                self._send(200, {"status": "ok", "data": service().decay(data)})
            elif self.path == "/archive":
                self._send(200, {"status": "ok", "data": service().archive(data)})
            else:
                self._send(404, {"status": "error", "message": "not found"})
        except Exception as exc:
            self._send(500, {"status": "error", "message": str(exc)})


def main():
    Path(DB).parent.mkdir(parents=True, exist_ok=True)
    if os.environ.get("LIFE_AUTO") == "1":
        import threading
        from .life import run_loop

        threading.Thread(target=run_loop, args=(life(),), daemon=True).start()
    print(f"[memory-core] db={DB} listen=http://{HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
