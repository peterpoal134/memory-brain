# -*- coding: utf-8 -*-
"""今日三件事：每天把当天最重要的记忆压成一段自我回顾。

这是「元记忆」：不是又记一件事实，而是记「我这一天是怎么过的、什么让我有感觉」。
"""

from __future__ import annotations

import re
from typing import Any

SYSTEM_PROMPT = "你在替一个角色回顾她的一天。只输出正文，不要标题、不要解释、不要 Markdown。"

PROMPT = """下面是这个角色在 {date} 记住的事（按重要性排序）：

{memories}

请用第一人称写「今天印象最深的三件事」：
- 正好三句，每句不超过 40 字，用换行分开，不要编号、不要加“第一/第二”。
- 只写上面真实出现过的，不要编，不要写“今天没什么特别的”。
- 可以带一点自己的感受，但别抒情过头。
- 不要出现“根据记忆”“这些事说明”这种话。
"""


def build_prompt(date: str, memories: list[dict]) -> str:
    lines = []
    for m in memories[:12]:
        kind = m.get("kind") or "episode"
        text = str(m.get("text") or "").strip()[:160]
        if text:
            lines.append(f"- [{kind}] {text}")
    return PROMPT.format(date=date, memories="\n".join(lines) or "（没有记忆）")


def clean(text: str) -> str:
    raw = str(text or "").strip()
    raw = re.sub(r"^```.*?\n|```$", "", raw, flags=re.S).strip()
    # 去掉“1.” “1、” “第一，”这类编号
    raw = re.sub(r"^\s*(?:\d+[.、)]|第[一二三]件[：:，,]?)\s*", "", raw, flags=re.M)
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in raw.splitlines() if ln.strip()]
    lines = [ln for ln in lines if len(ln) >= 4]
    return "\n".join(lines[:3])[:400]
