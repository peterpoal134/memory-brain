# -*- coding: utf-8 -*-
"""语感分析：从真人聊天里统计“大家最近怎么说话”。

不调模型，纯统计。用来做两件事：
1. 给注入块提供真人基准（句长、标点、语气词、例子）；
2. 自检她自己的回复和真人差多少。
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from typing import Any

PARTICLES = ("吧", "哦", "呢", "嘛", "呀", "啊", "哈", "啦", "呗", "咯", "诶", "唉", "嘿")
QQ_FACE_RE = re.compile(r"[\[［][^\]］]{1,6}[\]］]")
EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")
PUNCT_END_RE = re.compile(r"[。！？!?…]$")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def analyze(rows: list[dict]) -> dict[str, Any]:
    texts = []
    for r in rows:
        t = _clean(r.get("text"))
        # 去掉 “[管理员的朋友 xxx]” 这类引用前缀，只统计真正说的话
        t = re.sub(r"^(\[[^\]]{0,40}\]\s*)+", "", t).strip()
        if len(t) >= 2:
            texts.append(t)
    if not texts:
        return {"n": 0}

    lens = [len(t) for t in texts]
    particles: Counter = Counter()
    for t in texts:
        for p in PARTICLES:
            if p in t:
                particles[p] += 1

    # 例子：最近的、6~40 字、去重，最多 5 条
    examples: list[str] = []
    seen: set[str] = set()
    for t in reversed(texts):
        if any(bad in t for bad in ("[引用", "[At:", "[图片", "[表情", "http://", "https://",
                                     "请查看图片", "[对方发来")):
            continue
        t = re.sub(r"^(\[[^\]]{0,40}\]\s*)+", "", t).strip()
        if 6 <= len(t) <= 40 and t not in seen:
            seen.add(t)
            examples.append(t)
        if len(examples) >= 5:
            break

    return {
        "n": len(texts),
        "median_len": statistics.median(lens),
        "avg_len": round(statistics.mean(lens), 1),
        "short_ratio": round(sum(1 for x in lens if x <= 5) / len(lens), 2),
        "no_punct_ratio": round(sum(1 for t in texts if not PUNCT_END_RE.search(t)) / len(texts), 2),
        "qq_face_ratio": round(sum(1 for t in texts if QQ_FACE_RE.search(t)) / len(texts), 2),
        "emoji_ratio": round(sum(1 for t in texts if EMOJI_RE.search(t)) / len(texts), 2),
        "particles": [p for p, _ in particles.most_common(5)],
        "examples": examples,
    }


def build_block(stats: dict[str, Any], examples: list[str] | None = None) -> str:
    if not stats or stats.get("n", 0) < 20:
        return ""
    examples = examples if examples is not None else stats.get("examples") or []
    lines = [
        "【最近的聊天习惯】",
        f"真人聊天：中位 {int(stats['median_len'])} 字，"
        f"{int(stats['short_ratio'] * 100)}% 的消息不超过 5 字，"
        f"{int(stats['no_punct_ratio'] * 100)}% 句尾不带标点，"
        f"常用语气词 {'/'.join(stats.get('particles') or []) or '吧/哦/呢'}。",
    ]
    if examples:
        lines.append("这几句是最近的真人说法，学节奏，不要照抄：")
        lines.extend(f'- "{e}"' for e in examples[:4])
    return "\n".join(lines)
