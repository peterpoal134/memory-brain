# -*- coding: utf-8 -*-
"""把一段对话整理成记忆：摘要 + 类型 + 情绪 + 实体 + 关系。

只做「提示词 + 解析 + 兜底」，不碰 AstrBot，方便单测。
模型输出不合法时返回 None，由调用方走规则兜底。
"""

from __future__ import annotations

import json
import re
from typing import Any

KINDS = {"episode", "fact", "preference", "relation", "skill"}
ENTITY_TYPES = {"person", "place", "thing", "topic", "skill"}

SYSTEM_PROMPT = (
    "你在整理一个角色的长期记忆。只输出 JSON，不要解释、不要 Markdown 代码块。"
)

USER_PROMPT = """下面是一段对话，请整理成一条记忆，输出 JSON：

{{
  "summary": "一句话概括，不超过 100 字，用第一人称或对话里的名字",
  "kind": "episode|fact|preference|relation|skill",
  "importance": 0.0,
  "valence": 0.0,
  "entities": [{{"name": "实体名", "type": "person|place|thing|topic|skill"}}],
  "relations": [{{"src": "实体A", "dst": "实体B", "relation": "关系词"}}]
}}

规则：
- 只写对话里真实出现的信息，不编造、不脑补。
- summary 要能单独看懂，别写“他们聊了聊”这种空话。
- entities 只取有长期价值的人、地点、物件、话题；忽略“嗯”“哈哈”这类语气词。
- relations 只在两个实体确实有关系时输出，关系词 2-4 个字（如“喜欢”“讨厌”“在做”“住在”）。
- importance：寒暄 0.2、日常 0.4、约定/偏好 0.6、重要事件或情绪波动 0.8。
- valence：-1 很糟 到 1 很好。

对话：
{conversation}
"""


def _clip(value: Any, low: float, high: float, default: float = 0.0) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return default


def parse_json(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出第一个 JSON 对象。"""
    if not text:
        return None
    raw = str(text).strip()
    # 去掉 ```json ... ``` 包裹
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start: end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def normalize(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """校验并裁剪模型输出；没有可用摘要就当失败。"""
    if not isinstance(payload, dict):
        return None

    summary = re.sub(r"\s+", " ", str(payload.get("summary") or "")).strip()
    if len(summary) < 4:
        return None

    kind = str(payload.get("kind") or "episode").strip().lower()
    if kind not in KINDS:
        kind = "episode"

    entities: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in payload.get("entities") or []:
        if isinstance(item, str):
            name, etype = item, "thing"
        elif isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            etype = str(item.get("type") or "thing").strip().lower()
        else:
            continue
        name = name[:30]
        if not name or name in seen or len(name) < 1:
            continue
        if etype not in ENTITY_TYPES:
            etype = "thing"
        seen.add(name)
        entities.append({"name": name, "type": etype})
        if len(entities) >= 12:
            break

    relations: list[dict[str, str]] = []
    known = {e["name"] for e in entities}
    for item in payload.get("relations") or []:
        if not isinstance(item, dict):
            continue
        src = str(item.get("src") or "").strip()[:30]
        dst = str(item.get("dst") or "").strip()[:30]
        rel = str(item.get("relation") or "").strip()[:12]
        if not src or not dst or not rel or src == dst:
            continue
        # 关系里的实体没声明就补上，避免丢边
        for name in (src, dst):
            if name not in known:
                known.add(name)
                entities.append({"name": name, "type": "thing"})
        relations.append({"src": src, "dst": dst, "relation": rel})
        if len(relations) >= 12:
            break

    return {
        "summary": summary[:300],
        "kind": kind,
        "importance": _clip(payload.get("importance"), 0.05, 1.0, 0.45),
        "valence": _clip(payload.get("valence"), -1.0, 1.0, 0.0),
        "entities": entities,
        "relations": relations,
    }


def build_prompt(conversation: str) -> str:
    return USER_PROMPT.format(conversation=conversation[:3000])


def fallback(rows: list[tuple[str, str]]) -> dict[str, Any]:
    """没有模型可用时的规则兜底：拼原文 + 发言人当人物节点。"""
    text = "；".join(f"{name}：{msg[:120]}" for name, msg in rows)[:600]
    names = {name for name, _ in rows if name}
    return {
        "summary": text,
        "kind": "episode",
        "importance": 0.4,
        "valence": 0.0,
        "entities": [{"name": n, "type": "person"} for n in list(names)[:8]],
        "relations": [],
        "fallback": True,
    }
