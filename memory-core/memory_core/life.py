# -*- coding: utf-8 -*-
"""通用生活层：状态、动作、事件 outbox。不绑定任何平台。"""
from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Any

DEFAULTS = {
    "enabled": True,
    "interval_min": 20,
    "books_dir": "./books",
    "topics": ["最近在学的东西", "一个一直没弄懂的问题", "今天看到的一个现象"],
    "weights": {"idle": 1.0, "rest": 1.0, "reflect": 1.0, "book": 1.4, "phone": 1.2, "nap": 0.5},
}


class LifeService:
    def __init__(self, memory, config_path: str | None = None):
        self.memory = memory
        self.cfg = json.loads(Path(config_path).read_text(encoding="utf-8")) if config_path and Path(config_path).exists() else json.loads(json.dumps(DEFAULTS))
        for k, v in DEFAULTS.items():
            self.cfg.setdefault(k, v)
        self.conn = memory.store.conn
        self.conn.executescript(
            """CREATE TABLE IF NOT EXISTS life_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL);
               CREATE TABLE IF NOT EXISTS life_events (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   action TEXT NOT NULL,
                   text TEXT NOT NULL,
                   created_at REAL NOT NULL,
                   delivered INTEGER NOT NULL DEFAULT 0
               );"""
        )
        self.conn.commit()

    def _get(self, key: str, default: dict) -> dict:
        row = self.conn.execute("SELECT value FROM life_state WHERE key=?", (key,)).fetchone()
        if not row:
            return dict(default)
        try:
            return json.loads(row[0])
        except Exception:
            return dict(default)

    def _put(self, key: str, value: dict) -> None:
        self.conn.execute(
            "INSERT INTO life_state(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), time.time()),
        )
        self.conn.commit()

    def state(self) -> dict:
        s = self._get("state", {"energy": 0.7, "boredom": 0.5, "social": 0.5, "curiosity": 0.5,
                                "play": 0.5, "rest": 0.7, "last": time.time(), "book_offset": 0})
        return s

    def _recover(self, s: dict) -> None:
        now = time.time()
        hours = max(0.0, (now - float(s.get("last") or now)) / 3600.0)
        s["energy"] = min(1.0, float(s.get("energy", 0.7)) + hours * 0.03)
        s["boredom"] = min(1.0, float(s.get("boredom", 0.5)) + hours * 0.04)
        s["social"] = min(1.0, float(s.get("social", 0.5)) + hours * 0.02)
        s["curiosity"] = min(1.0, float(s.get("curiosity", 0.5)) + hours * 0.02)
        s["last"] = now

    def _choose(self, s: dict) -> str:
        hour = time.localtime().tm_hour
        if s.get("energy", 1) < 0.25:
            return "nap" if (12 <= hour < 14) else "rest"
        if 12 <= hour < 14 and random.random() < 0.25:
            return "nap"
        if s.get("boredom", 0) > 0.7 and random.random() < 0.45:
            return "phone"
        if s.get("curiosity", 0) > 0.65 and random.random() < 0.5:
            return "book"
        choices = [a for a in ("idle", "rest", "reflect", "book", "phone") if self.cfg["weights"].get(a, 1) > 0]
        weights = [float(self.cfg["weights"].get(a, 1)) for a in choices]
        return random.choices(choices, weights=weights, k=1)[0]

    def _book(self, s: dict) -> str:
        folder = Path(str(self.cfg.get("books_dir") or "./books"))
        books = sorted(folder.glob("*.txt")) if folder.exists() else []
        if not books:
            return ""
        book = books[int(s.get("book_index") or 0) % len(books)]
        text = book.read_text(encoding="utf-8", errors="ignore")
        off = int(s.get("book_offset") or 0)
        if off >= len(text):
            off = 0
            s["book_index"] = int(s.get("book_index") or 0) + 1
        chunk = text[off:off + 1500]
        s["book_offset"] = off + len(chunk)
        s["book_title"] = book.stem
        return f"读了一会儿《{book.stem}》：" + chunk.strip().replace("\n", " ")[:200]

    def step(self, force: str | None = None) -> dict:
        s = self.state()
        self._recover(s)
        action = force or self._choose(s)
        text = ""
        if action == "rest":
            s["energy"] = min(1.0, float(s.get("energy", 0.7)) + 0.12)
            s["boredom"] = max(0.0, float(s.get("boredom", 0.5)) - 0.03)
            text = "躺了一会儿，缓了缓。"
        elif action == "nap":
            s["energy"] = min(1.0, float(s.get("energy", 0.7)) + 0.25)
            s["boredom"] = max(0.0, float(s.get("boredom", 0.5)) - 0.05)
            text = "眯了一会儿，醒来有点恍惚。"
        elif action == "book":
            text = self._book(s)
            s["curiosity"] = max(0.0, float(s.get("curiosity", 0.5)) - 0.15)
            s["boredom"] = max(0.0, float(s.get("boredom", 0.5)) - 0.10)
            if not text:
                text = "想看书，但书架上还没有书。"
        elif action == "phone":
            s["boredom"] = max(0.0, float(s.get("boredom", 0.5)) - 0.18)
            s["curiosity"] = max(0.0, float(s.get("curiosity", 0.5)) - 0.05)
            s["energy"] = max(0.0, float(s.get("energy", 0.7)) - 0.02)
            text = "刷了会儿手机，看了些视频，没记住多少。"
        elif action == "reflect":
            s["boredom"] = max(0.0, float(s.get("boredom", 0.5)) - 0.08)
            text = "坐着想了一会儿最近的事。"
        elif action == "study":
            topics = self.cfg.get("topics") or []
            topic = str(topics[int(time.time()) % len(topics)]) if topics else "一个没弄懂的问题"
            s["curiosity"] = max(0.0, float(s.get("curiosity", 0.5)) - 0.15)
            text = f"查了点「{topic}」的资料，先记个印象。"
        s["last"] = time.time()
        self._put("state", s)
        if text and action != "idle":
            try:
                self.memory.remember({"text": text, "kind": "episode", "importance": 0.45,
                                      "visibility": "private", "source": "life"})
            except Exception:
                pass
        eid = None
        if text and action != "idle":
            cur = self.conn.execute(
                "INSERT INTO life_events(action,text,created_at) VALUES(?,?,?)",
                (action, text, time.time()),
            )
            self.conn.commit()
            eid = int(cur.lastrowid)
        return {"action": action, "text": text, "event_id": eid, "state": s}

    def events(self, after: int = 0, include_delivered: bool = True) -> list[dict]:
        sql = "SELECT * FROM life_events WHERE id>?"
        args: list[Any] = [int(after)]
        if not include_delivered:
            sql += " AND delivered=0"
        rows = self.conn.execute(sql + " ORDER BY id ASC LIMIT 100", args).fetchall()
        return [{"id": int(r["id"]), "action": r["action"], "text": r["text"],
                 "created_at": float(r["created_at"]), "delivered": bool(r["delivered"])} for r in rows]

    def ack(self, event_id: int) -> bool:
        cur = self.conn.execute("UPDATE life_events SET delivered=1 WHERE id=?", (int(event_id),))
        self.conn.commit()
        return cur.rowcount > 0


def run_loop(life: LifeService, stop: callable | None = None) -> None:
    import threading

    interval = max(60, int(float(life.cfg.get("interval_min") or 20) * 60))
    while not (stop and stop()):
        try:
            with life.memory.lock:
                life.step()
        except Exception:
            pass
        time.sleep(interval)
