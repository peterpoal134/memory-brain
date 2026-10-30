# -*- coding: utf-8 -*-
"""可配置的聊天层：人设、情绪、表达都来自配置，不绑定具体角色。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import persona, style

DEFAULT_CONFIG: dict[str, Any] = {
    "character": {
        "name": "助手",
        "system_prompt": "你是一个自然、友善的聊天助手。",
        "max_chars": 600,
        "max_lines": 3,
    },
    "emotion": {
        "baseline": [0.3, 0.4, 0.5],
        "half_life_min": 45,
        "max_step": 0.12,
        "threshold": 0.12,
        "rules": [
            {"label": "开心", "words": ["开心", "哈哈", "太好了", "喜欢"], "delta": [0.05, 0.03, 0.0], "hint": "语气轻快一点"},
            {"label": "难过", "words": ["难过", "想哭", "失落", "不开心"], "delta": [-0.06, -0.03, -0.02], "hint": "话少一点，别装开心"},
            {"label": "生气", "words": ["生气", "烦死", "滚", "闭嘴"], "delta": [-0.03, 0.06, 0.04], "hint": "句子短一点，别骂人"},
            {"label": "惊讶", "words": ["居然", "真的吗", "不会吧", "没想到"], "delta": [0.02, 0.10, -0.03], "hint": "先短促反应一下，再接一句"},
        ],
    },
}


class AgentService:
    def __init__(self, memory, config_path: str | None = None):
        self.memory = memory
        self.cfg = json.loads(Path(config_path).read_text(encoding="utf-8")) if config_path and Path(config_path).exists() else json.loads(json.dumps(DEFAULT_CONFIG))
        self.cfg.setdefault("character", DEFAULT_CONFIG["character"])
        self.cfg.setdefault("emotion", DEFAULT_CONFIG["emotion"])
        self.conn = memory.store.conn
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS agent_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL)"
        )
        self.conn.commit()

    # ---------------------------------------------------------------- 状态
    def _get(self, key: str) -> dict:
        row = self.conn.execute("SELECT value FROM agent_state WHERE key=?", (key,)).fetchone()
        if not row:
            return {}
        try:
            return json.loads(row[0])
        except Exception:
            return {}

    def _put(self, key: str, value: dict) -> None:
        self.conn.execute(
            "INSERT INTO agent_state(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), time.time()),
        )
        self.conn.commit()

    def mood(self) -> dict:
        m = self._get("mood") or {"p": 0.3, "a": 0.4, "d": 0.5, "labels": {}, "at": time.time()}
        return persona.decay(m, self.cfg["emotion"])

    def user(self, user_id: str) -> dict:
        u = self._get("user:" + str(user_id)) or {"affinity": 50.0, "msgs": 0, "last": 0}
        return u

    def state(self, user_id: str = "default") -> dict:
        m = self.mood()
        u = self.user(user_id)
        name, score = persona.top(m)
        return {
            "character": self.cfg["character"].get("name") or "助手",
            "user_id": user_id,
            "affinity": round(float(u.get("affinity") or 0), 1),
            "msgs": int(u.get("msgs") or 0),
            "p": round(float(m.get("p") or 0), 3),
            "a": round(float(m.get("a") or 0), 3),
            "d": round(float(m.get("d") or 0), 3),
            "label": name,
            "label_score": round(score, 2),
            "hint": persona.hint(m, self.cfg["emotion"]),
        }

    # ---------------------------------------------------------------- 聊天
    def chat(self, data: dict) -> dict:
        text = str(data.get("message") or "").strip()
        if not text:
            raise ValueError("message 不能为空")
        user_id = str(data.get("user_id") or "default")
        group = bool(data.get("is_group")) or "group" in str(data.get("session_id") or "")
        m = self.mood()
        persona.apply(m, persona.appraise(text, self.cfg["emotion"]), self.cfg["emotion"])
        u = self.user(user_id)
        u["affinity"] = round(min(100.0, float(u.get("affinity") or 0) + 0.2), 1)
        u["msgs"] = int(u.get("msgs") or 0) + 1
        u["last"] = time.time()
        recall = self.memory.recall({
            "query": text, "k": 5,
            "visibility": ["public", "group"] if group else ["public", "private"],
        })
        block = recall.get("block") or ""
        system = str(self.cfg["character"].get("system_prompt") or "你是一个聊天助手。")
        system += f"\n\n[此刻] 愉悦 {m.get('p', 0):.2f}，唤醒 {m.get('a', 0):.2f}，支配 {m.get('d', 0):.2f}"
        name, score = persona.top(m)
        if name:
            system += f"；主要情绪：{name} {score:.2f}"
        h = persona.hint(m, self.cfg["emotion"])
        if h:
            system += f"\n[表达] {h}"
        if block:
            system += "\n\n[你记得的旧事]\n" + block
        out = self.memory.chat({
            "model": data.get("model"), "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            "temperature": data.get("temperature", 0.7),
            "max_tokens": data.get("max_tokens"),
            "_skip_recall": True,
        })
        try:
            reply = out["choices"][0]["message"]["content"] or ""
        except Exception:
            reply = ""
        reply = style.clean(reply, self.cfg["character"])
        try:
            out["choices"][0]["message"]["content"] = reply
        except Exception:
            pass
        if data.get("remember", True):
            try:
                self.memory.remember({"text": text, "kind": "episode", "importance": 0.4,
                                      "visibility": "group" if group else "private", "source": "agent"})
            except Exception:
                pass
        self._put("mood", m)
        self._put("user:" + user_id, u)
        return {"reply": reply, "state": self.state(user_id), "raw": out}
