# -*- coding: utf-8 -*-
"""情绪与好感度的纯状态层（不依赖 AstrBot，可单独测试）。

情绪三轴：
- valence    心情   -100 ~ 100，基线 +10
- energy     精力     0 ~ 100，基线 65
- clinginess 黏人     0 ~ 100，由「多久没说话」+「最近互动」合成，基线 35

好感度：
- 每个聊过的人一个 0 ~ 100 的值，基线 30
- 等级：陌生 / 眼熟 / 朋友 / 亲近 / 重要的人
- 每天有正向收益上限（默认 +6），负向不设上限
- 超过 7 天没说话开始每天 -1，可配置下限（floors）

设计原则：
- 只在收到消息时计算，不做后台定时任务；时间衰减按「距上次更新多少小时」一次性补算。
- 所有数值都有上下限，规则误判最多让状态偏一点，不会失控。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- 常量

MOOD_BASELINE = {"valence": 10.0, "energy": 65.0, "cling_base": 35.0}

# 每小时回归基线的速度
DECAY_PER_HOUR = {"valence": 3.0, "energy": 2.0, "cling_adj": 1.2}

AFFINITY_BASE = 30.0
AFFINITY_DAILY_GAIN_CAP = 6.0
AFFINITY_IDLE_GRACE_DAYS = 7.0
AFFINITY_IDLE_DECAY_PER_DAY = 1.0

PRAISE_WORDS = (
    "厉害", "可爱", "好棒", "爱你", "想你", "抱抱", "摸摸", "亲亲", "乖", "辛苦",
    "谢谢", "喜欢你", "做得不错", "好看", "好听", "聪明", "宝贝", "棒", "想你了",
)
INSULT_WORDS = (
    "滚", "闭嘴", "烦死", "蠢", "笨", "讨厌", "别烦", "不理你", "垃圾", "废物",
    "傻", "走开", "烦人",
)


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def today_str(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now or time.time()))


def _toward(value: float, target: float, step: float) -> float:
    if value > target:
        return max(target, value - step)
    if value < target:
        return min(target, value + step)
    return value


def analyze_text(text: str) -> tuple[float, float, float]:
    """粗粒度情绪规则，返回 (心情增量, 黏人增量, 好感增量)。

    只做加减，不做判断分支；误判的代价被权重控制得很小。
    """
    t = str(text or "")
    praise = any(w in t for w in PRAISE_WORDS)
    insult = any(w in t for w in INSULT_WORDS)
    if insult:
        return -7.0, -1.0, -3.5
    if praise:
        return 4.0, 2.0, 1.2
    # 被搭理本身有一点点正向
    return 0.5, 0.0, 0.35


# ---------------------------------------------------------------- 情绪

def default_mood(now: float | None = None) -> dict[str, Any]:
    ts = now or time.time()
    return {
        "valence": MOOD_BASELINE["valence"],
        "energy": MOOD_BASELINE["energy"],
        "cling_adj": 0.0,
        "updated_at": ts,
        "last_user_msg_at": 0.0,
        "day": today_str(ts),
        "day_msgs": 0,
    }


def decay_mood(data: dict[str, Any], now: float) -> dict[str, Any]:
    """按经过的小时数，把三个值拉回基线。"""
    hours = (now - float(data.get("updated_at", now))) / 3600.0
    if hours > 0:
        data["valence"] = _toward(
            float(data.get("valence", MOOD_BASELINE["valence"])),
            MOOD_BASELINE["valence"],
            DECAY_PER_HOUR["valence"] * hours,
        )
        # 精力按时间段恢复：夜里睡觉、午间小睡回得更快
        mid_hour = time.localtime((now + float(data.get("updated_at", now))) / 2).tm_hour
        rate, target = DECAY_PER_HOUR["energy"], MOOD_BASELINE["energy"]
        if mid_hour >= 23 or mid_hour < 7:
            rate, target = 7.0, 80.0      # 夜间睡眠
        elif 12 <= mid_hour < 14:
            rate, target = 8.0, 65.0      # 午睡
        data["energy"] = _toward(
            float(data.get("energy", MOOD_BASELINE["energy"])),
            target,
            rate * hours,
        )
        data["cling_adj"] = _toward(
            float(data.get("cling_adj", 0.0)), 0.0, DECAY_PER_HOUR["cling_adj"] * hours
        )
    data["updated_at"] = now
    if data.get("day") != today_str(now):
        data["day"] = today_str(now)
        data["day_msgs"] = 0
    return data


def apply_message(
    data: dict[str, Any],
    now: float,
    text: str,
    *,
    is_group: bool = False,
) -> dict[str, Any]:
    """收到一条消息后的状态变化。"""
    decay_mood(data, now)
    dv, dc, _ = analyze_text(text)
    hour = time.localtime(now).tm_hour

    data["valence"] = clamp(float(data.get("valence", 10)) + dv, -100, 100)
    # 聊天本身耗精力；深夜更明显
    drain = 0.06 if is_group else 0.15
    if 0 <= hour < 6:
        drain += 0.4
    data["energy"] = clamp(float(data.get("energy", 65)) - drain, 0, 100)
    # 被搭理会缓解黏人（一次别降太狠），被夸会更黏
    data["cling_adj"] = clamp(float(data.get("cling_adj", 0)) - 0.8 + dc, -20, 60)

    data["last_user_msg_at"] = now
    data["day_msgs"] = int(data.get("day_msgs", 0)) + 1
    return data


def mood_view(data: dict[str, Any], now: float) -> dict[str, Any]:
    """算出对外展示的三个值（含黏人的合成项）。"""
    silent_h = 0.0
    if data.get("last_user_msg_at"):
        silent_h = max(0.0, (now - float(data["last_user_msg_at"])) / 3600.0)
    silence_bonus = min(60.0, 6.0 * max(0.0, silent_h - 2.0))
    cling = clamp(
        MOOD_BASELINE["cling_base"] + silence_bonus + float(data.get("cling_adj", 0.0))
    )
    return {
        "valence": clamp(float(data.get("valence", 10)), -100, 100),
        "energy": clamp(float(data.get("energy", 65))),
        "clinginess": cling,
        "silent_hours": silent_h,
        "day_msgs": int(data.get("day_msgs", 0)),
    }


def mood_labels(view: dict[str, Any]) -> dict[str, str]:
    v = view["valence"]
    e = view["energy"]
    c = view["clinginess"]
    if v <= -40:
        vl = "很差"
    elif v <= -10:
        vl = "不太好"
    elif v < 20:
        vl = "平静"
    elif v < 50:
        vl = "不错"
    else:
        vl = "很好"

    if e < 20:
        el = "快没电了"
    elif e < 45:
        el = "有点累"
    elif e < 75:
        el = "正常"
    else:
        el = "精神"

    if c < 25:
        cl = "想一个人待着"
    elif c < 50:
        cl = "正常"
    elif c < 75:
        cl = "有点黏"
    else:
        cl = "很想被理"
    return {"valence": vl, "energy": el, "clinginess": cl}


# ---------------------------------------------------------------- 好感度

def default_affinity() -> dict[str, Any]:
    return {"users": {}}


def affinity_level(score: float) -> str:
    if score < 20:
        return "陌生"
    if score < 40:
        return "眼熟"
    if score < 60:
        return "朋友"
    if score < 80:
        return "亲近"
    return "重要的人"


def touch_affinity(
    data: dict[str, Any],
    user_id: str,
    now: float,
    *,
    name: str = "",
    text: str = "",
    floors: dict[str, float] | None = None,
) -> tuple[dict[str, Any], float]:
    """更新一个人的好感度，返回 (记录, 本次变化量)。"""
    floors = floors or {}
    users = data.setdefault("users", {})
    rec = users.get(user_id)
    if not isinstance(rec, dict):
        rec = {
            "name": name or user_id,
            "score": AFFINITY_BASE,
            "first_seen": now,
            "last_seen": now,
            "msgs": 0,
            "gain_day": today_str(now),
            "gain_today": 0.0,
        }

    # 长期不联系：超过宽限期后每天掉一点（在触摸时一次性补算）
    last_seen = float(rec.get("last_seen", now))
    idle_days = (now - last_seen) / 86400.0
    if idle_days > AFFINITY_IDLE_GRACE_DAYS:
        loss = (idle_days - AFFINITY_IDLE_GRACE_DAYS) * AFFINITY_IDLE_DECAY_PER_DAY
        rec["score"] = clamp(float(rec.get("score", AFFINITY_BASE)) - loss)
    elif idle_days > 0:
        # 避免重复扣：把 last_seen 推到 now，但 idle 衰减只在超过宽限期时结算
        pass

    if rec.get("gain_day") != today_str(now):
        rec["gain_day"] = today_str(now)
        rec["gain_today"] = 0.0

    before = float(rec.get("score", AFFINITY_BASE))
    _, _, da = analyze_text(text)

    if da > 0:
        room = max(0.0, AFFINITY_DAILY_GAIN_CAP - float(rec.get("gain_today", 0.0)))
        delta = min(da, room)
        rec["gain_today"] = float(rec.get("gain_today", 0.0)) + max(0.0, delta)
    else:
        delta = da

    after = before + delta
    floor = float(floors.get(user_id, 0.0))
    rec["score"] = round(clamp(max(after, floor)), 2)
    rec["last_seen"] = now
    rec["msgs"] = int(rec.get("msgs", 0)) + 1
    if name:
        rec["name"] = name
    users[user_id] = rec
    return rec, rec["score"] - before


def affinity_view(rec: dict[str, Any] | None) -> dict[str, Any] | None:
    if not rec:
        return None
    score = float(rec.get("score", AFFINITY_BASE))
    return {
        "name": rec.get("name") or rec.get("user_id") or "对方",
        "score": score,
        "level": affinity_level(score),
        "msgs": int(rec.get("msgs", 0)),
    }


# ---------------------------------------------------------------- 读写

class JsonStore:
    """极小的 JSON 持久化，原子写 + 一份 .bak。"""

    def __init__(self, path: str | Path, factory):
        self.path = Path(path)
        self.factory = factory
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("不是对象")
            return data
        except FileNotFoundError:
            return self.factory()
        except Exception:
            try:
                self.path.replace(self.path.with_suffix(".json.bad"))
            except Exception:
                pass
            return self.factory()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.data, ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".state.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            if self.path.exists():
                try:
                    self.path.with_suffix(".json.bak").write_text(
                        self.path.read_text(encoding="utf-8"), encoding="utf-8"
                    )
                except OSError:
                    pass
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
