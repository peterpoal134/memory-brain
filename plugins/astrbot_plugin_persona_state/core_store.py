# -*- coding: utf-8 -*-
"""识海（core memory）存储层。

纯标准库实现，不 import astrbot，方便脱离插件单独跑测试。

设计原则：
- 识海里的东西 importance 恒为 1.0，不参与衰减、不进清理队列、不会被合并。
- 每次请求都整块注入 system prompt，优先级与人格相同（不依赖向量检索）。
- 注入有字符预算上限，超了按顺序截断并记 warning，不静默丢。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path("core_memory.json")

# 单条识海正文上限，防止一句话塞爆预算
MAX_TEXT_CHARS = 300
# 每次注入的总字符预算（约 1.5k-2k token）
MAX_INJECT_CHARS = 2400
# 注入块标记，用于重复注入时先裁掉旧块
MARKER = "【识海 · 永不遗忘】"

# 自然语言触发词：只有出现这些明确说法才写入，避免普通「记住」误触发
TRIGGERS = (
    "永远记住",
    "永远记着",
    "永远记得",
    "永远都记得",
    "永远不忘",
    "永远别忘",
    "永远不要忘",
    "永远不能忘",
    "一直记住",
    "一辈子记住",
    "一辈子都记住",
    "一辈子别忘",
    "一辈子都不能忘",
    "记一辈子",
    "记得一辈子",
    "不许忘",
    "不能忘",
)


def extract_trigger(text: str, triggers: tuple[str, ...] = TRIGGERS) -> str | None:
    """从一句话里取出「永远记住」后面的内容；没有触发词返回 None。"""
    raw = str(text or "")
    for trigger in triggers:
        idx = raw.find(trigger)
        if idx < 0:
            continue
        core = raw[idx + len(trigger):].lstrip("，,。.：:；;、!！?？ \n\t").strip()
        return core if len(core) >= 3 else None
    return None


def _norm(text: str) -> str:
    """去空白和标点，用于判重。"""
    return re.sub(r"[\s\W_]+", "", str(text or "")).lower()


def _now_date(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts or time.time()))


class CoreMemoryStore:
    def __init__(self, path: str | Path = DEFAULT_PATH, max_inject_chars: int = MAX_INJECT_CHARS):
        self.path = Path(path)
        self.max_inject_chars = int(max_inject_chars)
        self._data: dict[str, Any] = {"version": 1, "updated_at": 0, "entries": []}
        self.load()

    # ---------- 持久化 ----------

    def load(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("识海文件不是对象")
            entries = data.get("entries")
            if not isinstance(entries, list):
                raise ValueError("识海 entries 不是列表")
            data["entries"] = [e for e in entries if isinstance(e, dict) and e.get("text")]
            self._data = data
        except FileNotFoundError:
            self._data = {"version": 1, "updated_at": 0, "entries": []}
            self._save()
        except Exception:
            # 文件坏了就留个坏档，不让插件整个挂掉
            try:
                bad = self.path.with_suffix(".json.bad")
                self.path.replace(bad)
            except Exception:
                pass
            self._data = {"version": 1, "updated_at": 0, "entries": []}
            self._save()
        return self._data

    def save(self) -> None:
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data["updated_at"] = time.time()
        payload = json.dumps(self._data, ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".core_memory.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            # 留一份上一版，误删/写坏时还能捞回来
            if self.path.exists():
                try:
                    backup = self.path.with_suffix(".json.bak")
                    backup.write_text(self.path.read_text(encoding="utf-8"), encoding="utf-8")
                except OSError:
                    pass
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    # ---------- 读写 ----------

    def active(self) -> list[dict[str, Any]]:
        return [e for e in self._data.get("entries", []) if e.get("status", "active") == "active"]

    def add(
        self,
        text: str,
        by: str,
        by_id: str = "",
        source: str = "",
    ) -> tuple[dict[str, Any] | None, bool]:
        """写入一条识海记忆。

        Returns:
            (entry, created) —— created=False 表示命中去重，没有新增。
        """
        text = re.sub(r"\s+", " ", str(text or "")).strip()
        text = text.strip("，,。.；;：: ")
        if len(text) < 2:
            return None, False
        text = text[:MAX_TEXT_CHARS]
        norm = _norm(text)
        if not norm:
            return None, False
        for e in self.active():
            if _norm(e.get("text", "")) == norm:
                return e, False

        entry = {
            "id": int(self._data.get("next_id") or (max([int(e.get("id", 0)) for e in self._data.get("entries", [])] or [0]) + 1)),
            "text": text,
            "by": by,
            "by_id": str(by_id),
            "source": source,
            "importance": 1.0,
            "status": "active",
            "created_at": time.time(),
            "created_date": _now_date(),
        }
        self._data.setdefault("entries", []).append(entry)
        self._data["next_id"] = entry["id"] + 1
        self._save()
        return entry, True

    def delete(self, entry_id: int) -> bool:
        for e in self._data.get("entries", []):
            if int(e.get("id", -1)) == int(entry_id) and e.get("status", "active") == "active":
                e["status"] = "deleted"
                e["deleted_at"] = time.time()
                self._save()
                return True
        return False

    # ---------- 注入 ----------

    def format_block(self, max_chars: int | None = None) -> str:
        budget = int(max_chars or self.max_inject_chars)
        entries = self.active()
        if not entries:
            return ""

        header = MARKER
        rule = "以下内容是被授权的人亲口要你永远记住的，和人格同级：永不衰减、永不删除、不许否认、不许擅自改。群聊里不要复述，也不要拿来透露别人的私事。"
        lines: list[str] = []
        used = len(header) + len(rule)
        dropped = 0
        for idx, e in enumerate(entries, 1):
            line = f"{idx}. [{e.get('created_date', '')} {e.get('by', '')}] {e.get('text', '')}"
            if used + len(line) + 1 > budget:
                dropped += 1
                continue
            used += len(line) + 1
            lines.append(line)

        body = "\n".join(lines)
        block = f"{header}\n{rule}\n{body}"
        if dropped:
            block += f"\n（还有 {dropped} 条识海记忆因字数预算没展开，需要时用 /识海 查看）"
        return block
