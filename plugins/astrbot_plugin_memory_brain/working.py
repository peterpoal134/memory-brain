# -*- coding: utf-8 -*-
"""工作记忆：从聊天里认领「还没做完的事」，不调用模型。

只做三件事：
1. 从用户消息里抽待办/承诺；
2. 识别「好了/弄完了」这类收尾；
3. 生成注入块（最多 3 条）。
"""

from __future__ import annotations

import re

PATTERNS = (
    (r"(?:记得|别忘了|提醒我)([^。！？!?\n]{2,28})", "todo"),
    (r"(?:待会|等会|一会儿|等下|晚点|回头)([^。！？!?\n]{2,28})", "todo"),
    (r"(?:明天|后天|今晚|下午|晚上)([^。！？!?\n]{2,28})", "todo"),
    (r"我(?:得|要|准备)([^。！？!?\n]{2,28})", "todo"),
    (r"(?:答应|说好)([^。！？!?\n]{2,28})", "promise"),
)

DONE_RE = re.compile(r"(好了|弄完了|搞定了|不用了|完成了|做完了|改好了|发过去了|已经.{0,6}了)")


def extract(text: str) -> list[tuple[str, str]]:
    text = str(text or "").strip()
    if not text or len(text) < 4:
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for pattern, kind in PATTERNS:
        for m in re.finditer(pattern, text):
            phrase = re.sub(r"\s+", " ", m.group(0)).strip("，,。 ")
            if len(phrase) < 4 or phrase in seen:
                continue
            seen.add(phrase)
            out.append((phrase[:50], kind))
    return out[:3]


def is_done(text: str) -> bool:
    return bool(DONE_RE.search(str(text or "")))


def build_block(items: list[dict], limit: int = 3) -> str:
    if not items:
        return ""
    lines = ["【还没做完的事】"]
    for it in items[:limit]:
        prefix = "答应过" if it.get("kind") == "promise" else "待办"
        lines.append(f"- {prefix}：{it.get('text')}")
    return "\n".join(lines)
