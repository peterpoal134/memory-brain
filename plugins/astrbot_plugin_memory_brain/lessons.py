# -*- coding: utf-8 -*-
"""从她的笔记/看过的内容里提炼「可以照着做的道理」。

只做提示词 + 解析，不碰 AstrBot，方便单测。
"""

from __future__ import annotations

import json
import re
from typing import Any

SYSTEM_PROMPT = "你在帮一个角色把最近的见闻整理成做事的准则。只输出 JSON 数组，不要解释。"

PROMPT = """这是角色最近看到、读到、想到的东西：

{items}

请提炼 2–3 条「可以照着做的道理或方法」，要求：
- 具体、能指导下一次的行为，不要空泛鸡汤（不要“要善良”“做自己”这种）
- 每条不超过 40 字，用第一人称或祈使句都行
- 只从上面内容里推，不要编；推不出来就输出 []

输出 JSON 数组，每项：{{"lesson": "一句道理", "why": "为什么（20 字内）"}}
"""


def build_prompt(items: list[str]) -> str:
    body = "\n".join(f"- {x[:160]}" for x in items[:30])
    return PROMPT.format(items=body or "（没有内容）")


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


def normalize(items: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        lesson = re.sub(r"\s+", " ", str(item.get("lesson") or "")).strip("，。 ")
        if not (6 <= len(lesson) <= 60):
            continue
        key = re.sub(r"[\s\W_]+", "", lesson)
        if key in seen:
            continue
        seen.add(key)
        why = re.sub(r"\s+", " ", str(item.get("why") or "")).strip()[:40]
        out.append({"lesson": lesson, "why": why})
        if len(out) >= limit:
            break
    return out


def to_text(item: dict[str, Any]) -> str:
    why = item.get("why") or ""
    return f"{item['lesson']}" + (f"（{why}）" if why else "")
