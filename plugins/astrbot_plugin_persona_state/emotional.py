# -*- coding: utf-8 -*-
"""情绪记忆：由较大情绪波动产生的事件，永久保留，但平时不注入。

和普通长期记忆的区别：
- 只在情绪波动足够大时写入（委屈/生气/难过/感动），不按轮次总结，所以量很小。
- 永不衰减、不进清理队列；只在「下一次类似情绪/相近话题」时才有概率被想起。
- 平时零 token；被想起来时只占一两行。
- 想不想提出来由她自己决定（注入里会说明「不想翻就当没想起来」）。

匹配用字符二元组重叠，不依赖分词；中英文都能用。
"""

from __future__ import annotations

import random
import re
import time
from typing import Any

# 触发「记仇/记恩」的波动阈值（单次心情变化）
SINGLE_EVENT_DELTA = 8.0
# 短时间窗口内累计负向波动阈值
WINDOW_EVENT_DELTA = 12.0
WINDOW_SECONDS = 900

HURT_WORDS = ("委屈", "冤枉", "不理", "不在乎", "骗我", "敷衍", "嫌弃", "丢下", "凶", "骂", "滚", "闭嘴")
MOVE_WORDS = ("谢谢", "感动", "记得", "专门", "偷偷", "惊喜", "等你")

_STOP_BIGRAMS = {
    "我们", "你们", "他们", "什么", "怎么", "这个", "那个", "就是", "不是", "可以",
    "一个", "没有", "知道", "觉得", "现在", "时候", "自己", "因为", "所以", "但是",
}

# 注入块的标记，重复注入时先裁掉旧块
MARKER = "【压在心里的旧事】"


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def bigrams(text: str) -> set[str]:
    """中文友好的二元组集合，去掉标点和常见虚词组合。"""
    t = re.sub(r"[^\w\u4e00-\u9fff]+", "", _clean(text))
    if not t:
        return set()
    grams = {t[i:i + 2] for i in range(len(t) - 1)}
    grams -= _STOP_BIGRAMS
    return grams


def similarity(a: str, b: str) -> float:
    """重叠系数：交集 / 较小集合，0~1。"""
    ga, gb = bigrams(a), bigrams(b)
    if not ga or not gb:
        return 0.0
    inter = len(ga & gb)
    if not inter:
        return 0.0
    return inter / min(len(ga), len(gb))


def infer_emotion(text: str, delta: float) -> str:
    t = _clean(text)
    if any(w in t for w in ("骗", "敷衍", "不在乎", "不理", "冤", "嫌", "冷落")):
        return "委屈"
    if any(w in t for w in ("滚", "骂", "蠢", "笨", "烦", "闭嘴")):
        return "生气"
    if delta > 0:
        return "感动"
    if any(w in t for w in ("难过", "伤心", "哭", "失落")):
        return "难过"
    return "委屈" if delta <= -10 else "难受"


def make_event(
    *,
    user_id: str,
    user_name: str,
    trigger_text: str,
    delta: float,
    emotion: str = "",
    reason: str = "",
    source: str = "规则",
    now: float | None = None,
) -> dict[str, Any]:
    now = now or time.time()
    text = _clean(trigger_text)[:200]
    return {
        "user_id": str(user_id),
        "user_name": _clean(user_name)[:24],
        "trigger_text": text,
        "reason": _clean(reason)[:120],
        "delta": round(float(delta), 1),
        "intensity": round(min(1.0, abs(float(delta)) / 25.0), 2),
        "emotion": emotion or infer_emotion(text, delta),
        "source": source,
        "created_at": now,
        "date": time.strftime("%Y-%m-%d", time.localtime(now)),
        "recalls": 0,
        "last_recalled": 0.0,
        "resolved": False,
    }


def add_event(data: dict[str, Any], event: dict[str, Any]) -> dict[str, Any] | None:
    """写入一条情绪记忆；24 小时内文本几乎相同的不重复写。"""
    events = data.setdefault("events", [])
    for e in events:
        if (
            e.get("user_id") == event.get("user_id")
            and abs(float(e.get("created_at", 0)) - float(event.get("created_at", 0))) < 86400
            and similarity(e.get("trigger_text", ""), event.get("trigger_text", "")) >= 0.8
        ):
            return None
    event["id"] = int(data.get("next_id") or (max([int(x.get("id", 0)) for x in events] or [0]) + 1))
    data["next_id"] = event["id"] + 1
    events.append(event)
    # 永久保留，但防止异常刷屏
    if len(events) > 800:
        del events[: len(events) - 800]
    return event


def score_candidates(
    data: dict[str, Any],
    *,
    user_id: str,
    current_text: str,
    current_delta: float,
    current_valence: float,
    grudge: float,
    now: float,
) -> list[tuple[float, dict[str, Any]]]:
    """给每条旧账打分（0~100 的「被想起概率」），按分数降序。"""
    events = data.get("events", [])
    if not events:
        return []

    out: list[tuple[float, dict[str, Any]]] = []
    for e in events:
        if e.get("resolved"):
            continue
        if str(e.get("user_id")) != str(user_id):
            continue

        sim = similarity(current_text, e.get("trigger_text", ""))
        # 既不是情绪复现、话题也不像，就完全不考虑 —— 平时不调用
        if current_delta > -2 and sim < 0.15:
            continue

        p = float(grudge) * 0.15          # 记仇度决定基础概率
        if current_delta <= -7:
            p += 12
        elif current_delta <= -3:
            p += 7
        elif current_delta < 0:
            p += 3
        p += 45 * sim                      # 话题像不像才是主因
        p += 10 * float(e.get("intensity", 0.5))
        if current_valence < -20:
            p += 5
        if sim < 0.15:                     # 只凭情绪、话题不沾边：再压一档
            p *= 0.6
        p -= min(12.0, float(e.get("recalls", 0)) * 4.0)
        # 同一条旧账 6 小时内不重复翻
        if now - float(e.get("last_recalled", 0)) < 6 * 3600:
            p = 0

        p = max(0.0, min(85.0, p))
        if p > 0:
            out.append((p, e))

    out.sort(key=lambda x: x[0], reverse=True)
    return out


def pick(
    data: dict[str, Any],
    *,
    user_id: str,
    current_text: str,
    current_delta: float,
    current_valence: float,
    grudge: float,
    now: float,
    rng: random.Random | None = None,
) -> dict[str, Any] | None:
    """按概率决定这次要不要翻某一条旧账。"""
    rng = rng or random.Random()
    for p, event in score_candidates(
        data,
        user_id=user_id,
        current_text=current_text,
        current_delta=current_delta,
        current_valence=current_valence,
        grudge=grudge,
        now=now,
    ):
        if rng.random() * 100 < p:
            return event
    return None


def mark_recalled(data: dict[str, Any], event_id: int, now: float) -> None:
    for e in data.get("events", []):
        if int(e.get("id", -1)) == int(event_id):
            e["recalls"] = int(e.get("recalls", 0)) + 1
            e["last_recalled"] = now
            return


def format_block(event: dict[str, Any]) -> str:
    who = event.get("user_name") or "对方"
    text = event.get("trigger_text") or event.get("reason") or ""
    emotion = event.get("emotion") or "难受"
    date = event.get("date") or ""
    return (
        f"{MARKER}\n"
        f"你忽然想起 {date} 那天「{who}」说过的话：「{text[:100]}」。"
        f"当时你挺{emotion}的，一直没提。\n"
        f"要不要翻出来，你自己看着办；不想提，就当没想起来。"
    )
