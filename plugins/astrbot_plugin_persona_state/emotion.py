# -*- coding: utf-8 -*-
"""情绪层：PAD 三维 + 离散标签 + 快速衰减。

设计原则（防情绪主导）：
- 单条消息最多改变 0.12，标签最高 0.6，情绪只当语气，不当决策；
- 标签半衰期 45 分钟，心情半衰期 6 小时；
- 描述块明确写“不影响判断，任务照常”。
"""

from __future__ import annotations

import re
import time

DEFAULT = {"p": 0.05, "a": 0.35, "d": 0.5, "labels": {}, "trigger": "", "at": 0.0}

MASK_THRESHOLD = 0.25
SOFTEN_WINDOW = 900.0
MAX_STEP = 0.12
LABEL_CAP = 0.6
LABEL_HALF_LIFE_MIN = 45.0
MOOD_HALF_LIFE_H = 6.0

# 关键词 → 标签、PAD 变化、强度
RULES = (
    ("开心", ("哈哈", "笑死", "好笑", "可爱", "喜欢", "好棒", "厉害", "开心"), 0.10, 0.02, 0.0),
    ("生气", ("滚", "闭嘴", "蠢", "笨", "烦死", "骗子", "敷衍", "讨厌"), -0.12, 0.10, 0.04),
    ("难过", ("难过", "伤心", "哭", "失落", "走了", "再见"), -0.10, -0.04, -0.03),
    ("害怕", ("吓", "怕", "危险", "恐怖", "报警"), -0.08, 0.12, -0.08),
    ("委屈", ("冷落", "冤枉", "嫌我", "不公"), -0.10, 0.02, -0.07),
    ("害羞", ("脸红", "害羞", "别说了", "讨厌啦"), 0.06, 0.06, -0.04),
    ("得意", ("我厉害", "赢了", "夸我", "聪明"), 0.08, 0.02, 0.06),
    ("感动", ("谢谢", "心疼", "抱抱", "陪我", "记得"), 0.10, -0.02, 0.02),
    ("焦虑", ("来不及", "怎么办", "担心", "怕错过"), -0.06, 0.08, -0.04),
    ("无聊", ("无聊", "没意思", "好闲"), -0.04, -0.06, 0.0),
    ("惊讶", ("居然", "竟然", "真的吗", "真的假的", "不会吧", "没想到", "天哪", "哇", "啊？", "什么？", "突然"), 0.02, 0.14, -0.05),
)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _state(data: dict) -> dict:
    pad = data.get("pad")
    if not isinstance(pad, dict):
        pad = dict(DEFAULT)
        pad["labels"] = {}   # 别和 DEFAULT 共享同一个 dict
    pad.setdefault("labels", {})
    if not isinstance(pad.get("labels"), dict):
        pad["labels"] = {}
    for k, v in DEFAULT.items():
        pad.setdefault(k, v)
    data["pad"] = pad
    return pad


def appraise(text: str) -> list[tuple[str, float, float, float]]:
    t = _clean(text)
    hits: list[tuple[str, float, float, float]] = []
    for label, words, dp, da, dd in RULES:
        n = sum(1 for w in words if w in t)
        if n:
            scale = min(1.0, 0.6 + 0.2 * n)
            hits.append((label, dp * scale, da * scale, dd * scale))
    return hits[:2]


def decay(data: dict, now: float) -> None:
    pad = _state(data)
    last = float(pad.get("at") or 0)
    if not last:
        pad["at"] = now
        return
    minutes = max(0.0, (now - last) / 60.0)
    if minutes <= 0:
        return
    labels = pad["labels"]
    for name in list(labels):
        half = 8.0 if name == "惊讶" else LABEL_HALF_LIFE_MIN
        labels[name] = round(float(labels[name]) * (0.5 ** (minutes / half)), 3)
        if labels[name] < 0.02:
            labels.pop(name, None)
    kh = 0.5 ** (minutes / 60.0 / MOOD_HALF_LIFE_H)
    pad["p"] = DEFAULT["p"] + (float(pad.get("p", 0)) - DEFAULT["p"]) * kh
    pad["a"] = DEFAULT["a"] + (float(pad.get("a", 0.35)) - DEFAULT["a"]) * kh
    pad["d"] = DEFAULT["d"] + (float(pad.get("d", 0.5)) - DEFAULT["d"]) * kh
    pad["at"] = now


SOOTHE_RE = re.compile(
    r"对不起|抱歉|我错了|原谅|别生气|不委屈|没事了|别多想|放下了|没怪你|不怪你"
)


def apply(data: dict, text: str, now: float, *, is_group: bool = False) -> dict:
    """收到消息后更新情绪；任何变化都被限幅。"""
    pad = _state(data)
    decay(data, now)
    if SOOTHE_RE.search(_clean(text)):
        labels = pad["labels"]
        for name in ("生气", "委屈", "难过"):
            labels[name] = max(0.0, float(labels.get(name, 0)) - 0.08)
        labels["感动"] = min(LABEL_CAP, float(labels.get("感动", 0)) + 0.12)
        pad["soothed_at"] = now
        pad["p"] = min(1.0, float(pad["p"]) + 0.03)
        pad["a"] = max(0.0, float(pad["a"]) - 0.02)
        pad["trigger"] = _clean(text)[:40]
        pad["at"] = now
        return data
    energy = float(data.get("energy", 65)) / 100.0
    # 精力低时更容易被负面带走，正向打折
    hits = appraise(text)
    for label, dp, da, dd in hits:
        if energy < 0.3 and dp < 0:
            dp *= 1.25
            da *= 1.15
        if energy < 0.3 and dp > 0:
            dp *= 0.8
        dp = max(-MAX_STEP, min(MAX_STEP, dp))
        da = max(-MAX_STEP, min(MAX_STEP, da))
        dd = max(-MAX_STEP, min(MAX_STEP, dd))
        pad["p"] = max(-1.0, min(1.0, float(pad["p"]) + dp))
        pad["a"] = max(0.0, min(1.0, float(pad["a"]) + da))
        pad["d"] = max(0.0, min(1.0, float(pad["d"]) + dd))
        pad["labels"][label] = min(LABEL_CAP, float(pad["labels"].get(label, 0)) + abs(dp) * 2.2)
        # 对立标签互相抵消
        for other in ("开心", "得意", "感动"):
            if label in ("难过", "生气", "委屈"):
                pad["labels"][other] = max(0.0, float(pad["labels"].get(other, 0)) - 0.08)
        pad["trigger"] = _clean(text)[:40]
        pad["at"] = now
    return data


def top_label(data: dict) -> tuple[str, float]:
    labels = _state(data)["labels"]
    if not labels:
        return "", 0.0
    name = max(labels, key=lambda k: labels[k])
    return name, float(labels[name])


def describe(data: dict) -> str:
    name, score = top_label(data)
    if score < 0.12:
        return ""
    trigger = str(_state(data).get("trigger") or "")[:24]
    level = "有点" if score < 0.3 else ("挺" if score < 0.5 else "很")
    line = f"{level}{name}（{score:.1f}）"
    if trigger:
        line += f"，因为「{trigger}」"
    return line


def regulation(data: dict, now: float | None = None) -> str:
    """调节/伪装：嘴硬、被哄后别马上软、不拿情绪拿捏人。"""
    now = now or time.time()
    pad = _state(data)
    labels = pad["labels"]
    neg = max(
        float(labels.get(n, 0)) for n in ("生气", "委屈", "难过")
    ) if labels else 0.0
    lines: list[str] = []
    if neg >= MASK_THRESHOLD:
        lines.append("可以嘴硬：说“没事”“随便”，句尾带一点刺也行；但别真的装开心，也别主动求和。")
    soothed = float(pad.get("soothed_at") or 0)
    if soothed and now - soothed < SOFTEN_WINDOW and neg > 0.05:
        lines.append("刚被哄，拉不下脸，别马上说“没事”；撑一两轮，语气松一点就行。")
    if lines:
        lines.append("不能用情绪拿捏对方：不威胁、不冷战惩罚、不反复翻旧账换道歉。")
    return "\n".join(lines)


def hint(data: dict) -> str:
    name, score = top_label(data)
    if score < 0.12:
        return ""
    hints = {
        "开心": "语气轻快一点，可以多接一句",
        "生气": "句子短一点，别主动找话题，但别骂人",
        "难过": "别装开心，话少一点",
        "害怕": "别硬撑，可以示弱一句",
        "委屈": "可以话少，但别冷处理；被叫到先应一声，别让对方猜",
        "害羞": "可以嘴硬一句，别顺着说太多",
        "得意": "可以显摆一句，别过头",
        "感动": "语气软一点，记着这份好",
        "焦虑": "别催对方，先把自己这句话说清楚",
        "无聊": "可以主动找个话题，别刷屏",
        "惊讶": "先短促地反应一下（啊？/真的假的/不会吧），再接一句",
    }
    base = hints.get(name, "")
    if not base:
        return ""
    return base + "。情绪只影响语气，不影响判断；对方要你做事就正常做，不要主动提自己的情绪。"
