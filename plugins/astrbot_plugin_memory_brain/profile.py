# -*- coding: utf-8 -*-
"""人物画像：把某个实体相关的记忆压成一段印象。

不依赖 AstrBot；调用方负责把 memories / relations 取好传进来。
"""

from __future__ import annotations

import re
from typing import Any

SYSTEM_PROMPT = "你在帮一个角色整理对某个人/话题的印象。只输出一段中文，不要列表、不要 JSON、不要解释。"

PROMPT = """下面是角色记住的、和「{name}」有关的{kind}：

{memories}

{relations}

请用 2-4 句话写一段「角色对 {name} 的印象」：
- 只根据上面的事实，不要编。
- 写身份/关系、喜好和雷点、最近的状态；没有的就不写。
- 用第三人称陈述，不要出现“根据记忆”“这些记忆显示”这种话。
- 不超过 120 字。
"""

KIND_WORD = {"person": "人", "topic": "话题", "place": "地方", "thing": "东西", "skill": "技能"}


def build_prompt(name: str, etype: str, memories: list[dict], relations: list[dict]) -> str:
    lines = []
    for m in memories[:12]:
        stamp = str(m.get("date") or "")[:10]
        lines.append(f"- {stamp} {str(m.get('text') or '')[:160]}")
    rel_text = ""
    if relations:
        bits = []
        for r in relations[:8]:
            arrow = "→" if r.get("direction") == "out" else "←"
            bits.append(f"{arrow}{r.get('other')}（{r.get('relation')}）")
        rel_text = "关系：" + "、".join(bits)
    return PROMPT.format(
        name=name,
        kind=KIND_WORD.get(etype, "事"),
        memories="\n".join(lines) or "（没有具体记忆）",
        relations=rel_text,
    )


def clean(text: str) -> str:
    """去掉模型常见的客套前缀和多余空白。"""
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    raw = re.sub(r"^(印象|画像|总结)[：:]\s*", "", raw)
    return raw[:400]
