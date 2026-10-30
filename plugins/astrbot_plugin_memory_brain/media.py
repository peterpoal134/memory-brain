# -*- coding: utf-8 -*-
"""媒体知识通道：把看过的视频/文章/评论导入记忆。

只影响「知道什么」，不进语感统计。
数据来自 data/self/learning_journal.jsonl 和 share_queue.jsonl。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

MAX_TEXT = 180


def load_items(paths: list[Path], limit: int = 200) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and data.get("title"):
                data["_file"] = path.name
                items.append(data)
    return items


def load_watched(path: Path, limit: int = 120) -> list[dict[str, Any]]:
    """读 life_loop 的 watched.jsonl：一条记录可能包含多条视频。"""
    items: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return items
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = float(row.get("ts") or 0)
        note = str(row.get("note") or "")
        for i, it in enumerate(row.get("items") or []):
            desc = str(it.get("desc") or "").strip()
            if len(desc) < 8:
                continue
            items.append({
                "title": desc[:80],
                "note": note,
                "url": f"douyin:{ts:.0f}:{i}",
                "source": "抖音",
                "created_at": ts,
            })
    return items


def load_life_notes(path: Path, limit: int = 200) -> list[dict[str, Any]]:
    """读 life/notes.jsonl：她自己记的临时/长期笔记。"""
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = str(row.get("text") or "").strip()
        if len(text) < 4:
            continue
        rows.append({
            "ts": float(row.get("ts") or 0),
            "kind": str(row.get("kind") or "note"),
            "keep": "long" if row.get("keep") == "long" else "temp",
            "text": text[:400],
            "date": str(row.get("date") or ""),
        })
    return rows


def item_key(item: dict[str, Any]) -> str:
    raw = str(item.get("url") or item.get("title") or "")
    return re.sub(r"\s+", "", raw)[:200]


def to_text(item: dict[str, Any], limit: int = MAX_TEXT) -> str:
    title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip()
    body = str(item.get("note") or item.get("comment") or "").strip()
    body = re.sub(r"\s+", " ", body)
    extra = ""
    if body:
        extra = "：" + body[: max(20, limit - len(title) - 1)]
    text = (title + extra)[:limit]
    return text.strip()


def meta_of(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "url": str(item.get("url") or "")[:300],
        "title": str(item.get("title") or "")[:120],
        "media_source": str(item.get("source") or item.get("_file") or "")[:80],
        "date": str(item.get("created_at") or ""),
    }
