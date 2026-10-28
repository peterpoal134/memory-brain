# -*- coding: utf-8 -*-
"""每日运行报表：记忆增减、token、注入命中、各通道行为、错误数。"""

from __future__ import annotations
# 路径都从 AstrBot 数据目录推导，不写死个人机器
import os
from pathlib import Path as _P

_DATA = _P(__file__).resolve().parents[2]
_LIFE_DIR = _P(os.environ.get("ASTRBOT_LIFE_DIR", str(_DATA.parent / "life")))


import json
import re
import sqlite3
import time
from pathlib import Path

ASTRBOT_DB = (_DATA / "data_v4.db")
LOG = (_DATA.parent / "logs/astrbot.out.log")
LIFE_USAGE = (_LIFE_DIR / "usage.jsonl")


def _day_start(now: float | None = None) -> float:
    t = time.localtime(now or time.time())
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def tokens_today(now: float | None = None) -> tuple[int, int, dict[str, int]]:
    """AstrBot 侧今天的输入/输出 token，按模型分。"""
    start = _day_start(now)
    try:
        con = sqlite3.connect(f"file:{ASTRBOT_DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """SELECT provider_model,
                      SUM(token_input_other) AS tin_new,
                      SUM(token_input_cached) AS tin_cached,
                      SUM(token_output) AS tout
               FROM provider_stats WHERE created_at >= ?
               GROUP BY provider_model""",
            (start,),
        ).fetchall()
        con.close()
    except Exception:
        return 0, 0, {}
    tin = sum(int(r["tin_new"] or 0) for r in rows)
    cached = sum(int(r["tin_cached"] or 0) for r in rows)
    tout = sum(int(r["tout"] or 0) for r in rows)
    by_model = {
        str(r["provider_model"]): int(r["tin_new"] or 0) + int(r["tin_cached"] or 0) + int(r["tout"] or 0)
        for r in rows
    }
    return tin, cached, tout, by_model


def life_tokens_today(now: float | None = None) -> int:
    start = _day_start(now)
    total = 0
    try:
        for line in LIFE_USAGE.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if float(row.get("ts") or 0) >= start:
                total += int(row.get("total_tokens") or 0)
    except OSError:
        pass
    return total


def errors_today(now: float | None = None) -> int:
    start = _day_start(now)
    count = 0
    try:
        with LOG.open("rb") as fh:
            try:
                fh.seek(-6_000_000, 2)
            except OSError:
                fh.seek(0)
            for raw in fh:
                line = raw.decode("utf-8", "ignore")
                if "[ERRO]" not in line:
                    continue
                m = re.search(r"\[(\d\d):(\d\d):(\d\d)\.", line)
                if m:
                    h, mi, s = map(int, m.groups())
                    ts = time.mktime(time.localtime(start)[:3] + (h, mi, s, 0, 0, -1))
                    if ts >= start:
                        count += 1
                else:
                    count += 1
    except OSError:
        pass
    return count


def build(store, now: float | None = None) -> str:
    now = now or time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    st = store.stats()
    start = _day_start(now)
    added = store.count_memories(since=start)
    stats = store.daily_stats(day)
    tin, cached, tout, by_model = tokens_today(now)
    life_tok = life_tokens_today(now)
    errs = errors_today(now)

    kinds = "、".join(f"{k} {v}" for k, v in sorted(st.get("kinds", {}).items()))
    lines = [
        f"【{day} 运行报表】",
        f"记忆：{st.get('active')} 条（{kinds}），今天新增 {added}",
        f"实体 {st.get('entities')} / 关系 {st.get('relations')} / 归档 {st.get('archived')}",
        f"对话 token：新增输入 {tin:,} / 缓存命中 {cached:,} / 输出 {tout:,}",
        f"生活侧 token：{life_tok:,}",
        "今天发生："
        + (
            "，".join(f"{k} {int(v)}" for k, v in sorted(stats.items()) if v)
            or "无"
        ),
        f"日志错误：{errs} 条",
    ]
    if by_model:
        top = sorted(by_model.items(), key=lambda x: -x[1])[:3]
        lines.append("模型消耗：" + "，".join(f"{m} {v:,}" for m, v in top))
    return "\n".join(lines)
