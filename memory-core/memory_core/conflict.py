# -*- coding: utf-8 -*-
"""冲突检测：同一实体下，两条语义相近但极性相反的事实。

纯规则，不调模型：
- 必须共享实体；
- 文本相似度足够高（说的是同一件事）；
- 一个带正面词、另一个带对应的反面词。
"""

from __future__ import annotations

import re

from .store import _bigram_sim

ANTONYMS = (
    ("喜欢", "讨厌"), ("喜欢", "不喜欢"), ("爱吃", "不爱吃"), ("爱吃", "讨厌"),
    ("擅长", "不擅长"), ("会", "不会"), ("能", "不能"), ("是", "不是"),
    ("有", "没有"), ("同意", "不同意"), ("支持", "反对"), ("愿意", "不愿意"),
    ("要去", "不去"), ("想", "不想"), ("经常", "从不"), ("总是", "从不"),
    ("害怕", "不怕"), ("接受", "不接受"), ("允许", "不允许"),
)

MIN_SIMILARITY = 0.45


def _norm(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", str(text or ""))


def polarity(text: str) -> set[str]:
    """返回文本里出现的正/反词。"""
    t = _norm(text)
    hits: set[str] = set()
    for pos, neg in ANTONYMS:
        if pos in t:
            hits.add(pos)
        if neg in t:
            hits.add(neg)
    return hits


def is_contradiction(a: str, b: str) -> bool:
    if _bigram_sim(a, b) < MIN_SIMILARITY:
        return False
    pa, pb = polarity(a), polarity(b)
    for pos, neg in ANTONYMS:
        if (pos in pa and neg in pb) or (neg in pa and pos in pb):
            return True
    return False


def find(facts: list[dict]) -> list[tuple[int, int, str]]:
    """facts: [{id, text, entity_ids:set}] → [(a_id, b_id, 原因)]"""
    out: list[tuple[int, int, str]] = []
    for i in range(len(facts)):
        a = facts[i]
        for j in range(i + 1, len(facts)):
            b = facts[j]
            if not (a.get("entity_ids") & b.get("entity_ids")):
                continue
            if not is_contradiction(a["text"], b["text"]):
                continue
            shared = a["entity_ids"] & b["entity_ids"]
            out.append((a["id"], b["id"], f"共享实体 {','.join(map(str, sorted(shared)))}"))
    return out
