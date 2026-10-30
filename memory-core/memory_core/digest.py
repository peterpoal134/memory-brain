# -*- coding: utf-8 -*-
"""把 episode 消化成事实、反思、人物画像。通用提示词，不绑定角色。"""
from __future__ import annotations

import json
import time
from typing import Any

from . import extractor, profile, reflection


class DigestService:
    def __init__(self, memory):
        self.memory = memory

    def extract(self, text: str, visibility: str = "private", source: str = "digest") -> dict | None:
        raw = self.memory.complete(extractor.build_prompt(text), extractor.SYSTEM_PROMPT, 700)
        data = extractor.normalize(extractor.parse_json(raw))
        if not data:
            data = extractor.fallback([("user", text)])
        entities = [(e.get("name"), e.get("type")) for e in data.get("entities") or []]
        relations = [(r.get("src"), r.get("dst"), r.get("relation")) for r in data.get("relations") or []]
        mid = self.memory.remember({
            "text": data["summary"], "kind": data.get("kind") or "episode",
            "importance": data.get("importance") or 0.5,
            "valence": data.get("valence") or 0,
            "visibility": visibility, "source": source,
            "entities": entities, "relations": relations,
            "meta": {"digest": True},
        })
        return {"id": mid.get("id"), "kind": data.get("kind"), "summary": data["summary"]}

    def reflect(self, day: str | None = None, visibility: str = "private") -> dict | None:
        day = day or time.strftime("%Y-%m-%d")
        start = time.mktime(time.strptime(day, "%Y-%m-%d"))
        end = start + 86400
        rows = []
        for m in self.memory.store.list_memories(limit=500):
            if start <= float(m.get("created_at") or 0) < end:
                rows.append(m)
        if not rows:
            return None
        raw = self.memory.complete(reflection.build_prompt(day, rows), "只写一段中文，不要列表。", 500)
        text = reflection.clean(raw)
        if len(text) < 8:
            return None
        mid = self.memory.remember({"text": text, "kind": "reflection", "importance": 0.7,
                                    "visibility": visibility, "source": "digest",
                                    "meta": {"date": day}})
        return {"id": mid.get("id"), "date": day, "text": text}

    def profile(self, name: str, visibility: str = "private") -> dict | None:
        name = str(name or "").strip()
        if not name:
            return None
        rows = [m for m in self.memory.store.list_memories(limit=500) if name in str(m.get("text") or "")][:12]
        if not rows:
            return None
        prompt = profile.build_prompt(name, "person", rows, [])
        text = profile.clean(self.memory.complete(prompt, profile.SYSTEM_PROMPT, 400))
        if len(text) < 6:
            return None
        mid = self.memory.remember({"text": text, "kind": "profile", "importance": 0.65,
                                    "visibility": visibility, "source": "digest",
                                    "meta": {"entity": name}})
        return {"id": mid.get("id"), "entity": name, "text": text}
