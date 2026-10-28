# -*- coding: utf-8 -*-
"""事件 → 事实：从一天的流水账里抽出长期有效的事实/偏好。

只做提示词 + 解析 + 裁剪，不碰 AstrBot，方便单测。
"""

from __future__ import annotations

import json
import re
from typing import Any

SYSTEM_PROMPT = "你在整理一个角色的长期记忆。只输出 JSON 数组，不要解释、不要 Markdown。"

PROMPT = """下面是角色这一天记住的事（按重要性排序）：

{memories}

请从中提取「长期有效、以后还会用到」的事实、偏好或关系，忽略一次性的过程。
输出 JSON 数组，每项：
{{"fact": "一句不超过 60 字的事实", "importance": 0.0, "entities": [{{"name": "实体", "type": "person|place|thing|topic|skill"}}]}}

规则：
- 只写能从上面直接推出来的，不要编造、不要总结感受。
- 一次性的事（今天吃了什么、今天做了什么）不要写；长期有效的才写。
- 同一件事只写一条。
- 最多 6 条；没有合适的就输出 []。
"""

ENTITY_TYPES = {"person", "place", "thing", "topic", "skill"}


def build_prompt(memories: list[dict]) -> str:
    lines = []
    for m in memories[:20]:
        lines.append(f"- {str(m.get('text') or '')[:160]}")
    return PROMPT.format(memories="\n".join(lines) or "（没有记忆）")


def parse_array(text: str) -> list[dict[str, Any]]:
    raw = str(text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(raw[start: end + 1])
    except json.JSONDecodeError:
        return []
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


def normalize(items: list[dict[str, Any]], limit: int = 6) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        fact = re.sub(r"\s+", " ", str(item.get("fact") or "")).strip()
        if len(fact) < 6 or len(fact) > 120:
            continue
        key = re.sub(r"[\s\W_]+", "", fact).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        try:
            importance = float(item.get("importance", 0.6))
        except (TypeError, ValueError):
            importance = 0.6
        importance = max(0.3, min(1.0, importance))
        entities = []
        for e in item.get("entities") or []:
            if not isinstance(e, dict):
                continue
            name = str(e.get("name") or "").strip()[:30]
            etype = str(e.get("type") or "thing").strip().lower()
            if name and etype in ENTITY_TYPES:
                entities.append({"name": name, "type": etype})
        out.append({"fact": fact, "importance": importance, "entities": entities[:6]})
        if len(out) >= limit:
            break
    return out
