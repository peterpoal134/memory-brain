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
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import numpy as np

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
AUTO_REMEMBER = os.environ.get("MEMORY_AUTO_REMEMBER", "0") == "1"


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


class MemoryService:
    def __init__(self, db: str | None = None, embed=True):
        self.store = Store(Path(db or DB))
        self.engine = MemoryEngine(self.store, embed=_embed if embed else None)

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

    def stats(self) -> dict:
        s = self.store.stats()
        s["avg_importance"] = round(self.engine._avg_importance(), 3)
        return s

    def chat(self, data: dict) -> dict:
        """OpenAI 兼容入口：自动召回并拼进 system，再转发给上游模型。"""
        if not LLM_BASE:
            raise RuntimeError("没有配置 MEMORY_LLM_BASE")
        msgs = list(data.get("messages") or [])
        user = ""
        for m in reversed(msgs):
            if m.get("role") == "user":
                user = str(m.get("content") or "")
                break
        block = self.recall({"query": user, "k": 5}).get("block") if user else ""
        if block:
            msgs = [{"role": "system", "content": "以下是你记得的旧事，用来接话，不要逐条复述：\n" + block}] + msgs
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
        req = urllib.request.Request(
            LLM_BASE + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {LLM_KEY}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            out = json.loads(r.read().decode())
        if AUTO_REMEMBER and user:
            try:
                self.remember({"text": user, "kind": "episode", "importance": 0.4, "visibility": "private", "source": "chat"})
            except Exception:
                pass
        return out

    def report(self) -> str:
        from . import health

        return health.report(self.store)


SERVICE: MemoryService | None = None


def service() -> MemoryService:
    global SERVICE
    if SERVICE is None:
        SERVICE = MemoryService()
    return SERVICE


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
        if self.path == "/v1/models":
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
            data = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception as exc:
            self._send(400, {"status": "error", "message": f"bad json: {exc}"})
            return
        try:
            if self.path == "/v1/chat/completions":
                out = service().chat(data)
                if data.get("stream"):
                    text = ""
                    try:
                        text = out["choices"][0]["message"]["content"]
                    except Exception:
                        pass
                    chunk = {"id": out.get("id", "chatcmpl"), "object": "chat.completion.chunk",
                             "created": out.get("created", int(time.time())), "model": out.get("model", "memory-core"),
                             "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]}
                    done = {"id": out.get("id", "chatcmpl"), "object": "chat.completion.chunk",
                            "created": out.get("created", int(time.time())), "model": out.get("model", "memory-core"),
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                    body = ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n" +
                            "data: " + json.dumps(done, ensure_ascii=False) + "\n\ndata: [DONE]\n\n").encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self._send(200, out)
            elif self.path == "/remember":
                self._send(200, {"status": "ok", "data": service().remember(data)})
            elif self.path == "/recall":
                self._send(200, {"status": "ok", "data": service().recall(data)})
            elif self.path == "/decay":
                n = service().engine.decay(
                    float(data.get("rate") or 0.01),
                    protect_names=data.get("protect_names") or [],
                    core_floor=float(data.get("core_floor") or 0.45),
                    emotion_factor=float(data.get("emotion_factor") or 0.4),
                )
                self._send(200, {"status": "ok", "data": {"affected": n}})
            elif self.path == "/archive":
                n = service().engine.archive(
                    importance_below=float(data.get("importance_below") or 0.3),
                    older_than_days=float(data.get("older_than_days") or 30),
                    protect_days=float(data.get("protect_days") or 30),
                )
                self._send(200, {"status": "ok", "data": {"archived": n}})
            else:
                self._send(404, {"status": "error", "message": "not found"})
        except Exception as exc:
            self._send(500, {"status": "error", "message": str(exc)})


def main():
    Path(DB).parent.mkdir(parents=True, exist_ok=True)
    print(f"[memory-core] db={DB} listen=http://{HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
