# -*- coding: utf-8 -*-
"""程序记忆：把“刚做完的一件事”整理成可复用的步骤。

只在用户确认完成时触发，一次任务只写一条 skill。
"""

from __future__ import annotations

import json
import re
from typing import Any

SYSTEM_PROMPT = "你在把一次刚完成的任务整理成可复用的做法。只输出 JSON 对象，不要解释。"

PROMPT = """下面是刚完成的一件事的对话片段：

{transcript}

请输出 JSON：
{{"task": "一句话任务名", "steps": ["步骤1", "步骤2", "步骤3"], "entities": [{{"name": "", "type": "person|place|thing|topic|skill"}}]}}

规则：
- 步骤 3–6 条，每条不超过 20 字，写这次实际做过的，不要编。
- 如果只是闲聊、没有可复用的做法，输出 {{}}。
- 任务名不超过 20 字。
"""

CONFIRM_RE = re.compile(r"(好了|可以了|成功了|过了|对了|收到|谢谢|弄好了|搞定了|完成了|没问题|OK|ok)")
ENTITY_TYPES = {"person", "place", "thing", "topic", "skill"}


def build_prompt(messages: list[dict]) -> str:
    lines = []
    for m in messages[-16:]:
        who = "对方" if m.get("role") == "user" else "我"
        text = re.sub(r"\s+", " ", str(m.get("text") or ""))[:120]
        if text:
            lines.append(f"{who}：{text}")
    return PROMPT.format(transcript="\n".join(lines) or "（没有内容）")


def parse_object(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start: end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def normalize(data: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    task = re.sub(r"\s+", " ", str(data.get("task") or "")).strip()[:30]
    steps = [
        re.sub(r"\s+", " ", str(s)).strip()[:30]
        for s in (data.get("steps") or [])
        if str(s).strip()
    ][:6]
    if len(task) < 3 or len(steps) < 2:
        return None
    entities = []
    for e in data.get("entities") or []:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name") or "").strip()[:30]
        etype = str(e.get("type") or "thing").strip().lower()
        if name and etype in ENTITY_TYPES:
            entities.append({"name": name, "type": etype})
    return {"task": task, "steps": steps, "entities": entities[:6]}


def to_text(skill: dict[str, Any]) -> str:
    return skill["task"] + "：" + "；".join(
        f"{i + 1}. {s}" for i, s in enumerate(skill["steps"])
    )
