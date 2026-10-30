# -*- coding: utf-8 -*-
"""上下文压缩：超长对话滚动摘要，保留最近若干轮。"""
from __future__ import annotations

import hashlib
import time


class ContextCompressor:
    def __init__(self, memory, budget_chars: int = 20000, keep_recent: int = 10,
                 max_turns: int = 24, keep_ratio: float = 0.15, summary_memory: bool = True):
        self.memory = memory
        self.budget = max(2000, int(budget_chars))
        self.keep = max(2, int(keep_recent))
        self.max_turns = max(1, int(max_turns))
        self.keep_ratio = max(0.05, min(0.5, float(keep_ratio)))
        self.summary_memory = bool(summary_memory)
        self.conn = memory.store.conn
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS ctx_summary (
                   session TEXT PRIMARY KEY,
                   summary TEXT NOT NULL,
                   updated_at REAL NOT NULL
               )"""
        )
        self.conn.commit()

    def _cache(self, key: str) -> str:
        row = self.conn.execute("SELECT summary FROM ctx_summary WHERE session=?", (key,)).fetchone()
        return str(row[0]) if row else ""

    def _save(self, key: str, summary: str) -> None:
        self.conn.execute(
            "INSERT INTO ctx_summary(session,summary,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(session) DO UPDATE SET summary=excluded.summary, updated_at=excluded.updated_at",
            (key, summary, time.time()),
        )
        self.conn.commit()

    def compress(self, messages: list[dict], key: str = "default", visibility: str = "private") -> list[dict]:
        msgs = [m for m in messages if isinstance(m, dict)]
        total = sum(len(str(m.get("content") or "")) for m in msgs)
        system = [m for m in msgs if m.get("role") == "system"]
        convo = [m for m in msgs if m.get("role") != "system"]
        if total <= self.budget and len(convo) <= self.max_turns * 2:
            return msgs
        keep = max(self.keep, int(len(convo) * self.keep_ratio))
        if len(convo) <= keep:
            return msgs
        old, recent = convo[:-keep], convo[-keep:]
        old_text = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in old)[-16000:]
        prev = self._cache(key)
        text = (("已有摘要：\n" + prev + "\n\n") if prev else "") + "新增对话：\n" + old_text
        try:
            summary = str(self.memory.summarize(text) or "").strip()
        except Exception:
            summary = ""
        if not summary:
            summary = prev or old_text[-800:]
        summary = summary[:2000]
        changed = summary != prev
        self._save(key, summary)
        if self.summary_memory and changed:
            try:
                self.memory.remember({
                    "text": "[对话摘要] " + summary, "kind": "episode", "importance": 0.55,
                    "visibility": visibility, "source": "compression",
                    "meta": {"session": key},
                })
            except Exception:
                pass
        out = list(system)
        out.append({"role": "system", "content": "[之前对话摘要] " + summary})
        out.extend(recent)
        return out

    @staticmethod
    def key_for(user: str, system: str = "") -> str:
        h = hashlib.sha1(str(system or "").encode("utf-8")).hexdigest()[:8]
        return f"{user or 'default'}:{h}"
