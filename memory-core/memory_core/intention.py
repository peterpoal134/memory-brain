# -*- coding: utf-8 -*-
"""从一句话里识别“提醒我 / 叫我”这类将来要做的事。"""
from __future__ import annotations

import datetime
import re

CUE = re.compile(r"提醒我|叫我|别忘了|记得|到点|到时")
NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
       "十一": 11, "十二": 12}
WEEK = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def _hour(text: str) -> tuple[int, int] | None:
    m = re.search(r"(\d{1,2})\s*[:：]\s*(\d{2})", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"([一二三四五六七八九十两\d]{1,2})\s*点\s*(半|\d{1,2})?", text)
    if not m:
        return None
    h = NUM.get(m.group(1), None)
    if h is None:
        try:
            h = int(m.group(1))
        except ValueError:
            return None
    minute = 30 if m.group(2) == "半" else int(m.group(2) or 0)
    if any(w in text for w in ("下午", "晚上", "傍晚")) and h < 12:
        h += 12
    if "中午" in text and h < 11:
        h += 12
    return h % 24, minute


def parse(text: str, now: datetime.datetime | None = None):
    """返回 (due_ts, 任务文本, 时间标签) 或 None。"""
    now = now or datetime.datetime.now()
    t = (text or "").strip()
    if not CUE.search(t):
        return None
    hm = _hour(t)
    day = None
    label = ""
    if "大后天" in t:
        day = now.date() + datetime.timedelta(days=3); label = "大后天"
    elif "后天" in t:
        day = now.date() + datetime.timedelta(days=2); label = "后天"
    elif "明早" in t or "明天早上" in t:
        day = now.date() + datetime.timedelta(days=1); label = "明天早上"
    elif "明晚" in t or "明天晚上" in t:
        day = now.date() + datetime.timedelta(days=1); label = "明天晚上"
    elif "明天" in t:
        day = now.date() + datetime.timedelta(days=1); label = "明天"
    elif "今晚" in t or "今天晚上" in t:
        day = now.date(); label = "今晚"
    elif "今天" in t:
        day = now.date(); label = "今天"
    else:
        m = re.search(r"(?:周|星期|礼拜)([一二三四五六日天])", t)
        if m:
            target = WEEK[m.group(1)]
            days = (target - now.weekday()) % 7
            if days == 0 and (hm is None or (hm[0], hm[1]) <= (now.hour, now.minute)):
                days = 7
            day = now.date() + datetime.timedelta(days=days)
            label = f"周{m.group(1)}"
    if day is None:
        if hm is None:
            return None
        # 只说“X点提醒我”：今天，过了就明天
        day = now.date()
        if (hm[0], hm[1]) <= (now.hour, now.minute):
            day = day + datetime.timedelta(days=1)
            label = "明天"
        else:
            label = "今天"
    if hm is None:
        if label in ("明早", "明天早上"):
            hm = (8, 0)
        elif label in ("今晚", "明晚", "明天晚上"):
            hm = (20, 0)
        else:
            hm = (9, 0) if day != now.date() else ((now.hour + 1) % 24, 0)
    due = datetime.datetime.combine(day, datetime.time(hm[0], hm[1]))
    if due <= now - datetime.timedelta(minutes=1):
        return None
    when = label or due.strftime("%m-%d")
    return due.timestamp(), t, f"{when} {due.strftime('%H:%M')}"
