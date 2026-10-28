# -*- coding: utf-8 -*-
"""记忆体检：重复、孤儿、僵尸记忆、冲突。"""

from __future__ import annotations

import numpy as np


def _cos(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def duplicates(store, kind: str | None = None, threshold: float = 0.92) -> list[tuple[int, int, float]]:
    rows = store.list_memories(kind=kind, limit=500) if kind else store.list_memories(limit=500)
    ids, mat = store.all_vectors()
    index = {int(mid): i for i, mid in enumerate(ids)}
    rows = [r for r in rows if int(r["id"]) in index]
    out = []
    for i in range(len(rows)):
        a = int(rows[i]["id"])
        for j in range(i + 1, len(rows)):
            b = int(rows[j]["id"])
            sim = _cos(mat[index[a]], mat[index[b]])
            if sim >= threshold:
                out.append((a, b, round(sim, 3)))
    return out


def orphans(store) -> list[int]:
    rows = store.conn.execute(
        """SELECT id FROM memories WHERE status='active' AND id NOT IN (
               SELECT DISTINCT memory_id FROM memory_entities) LIMIT 200"""
    ).fetchall()
    return [int(r["id"]) for r in rows]


def stale_facts(store, days: int = 120, limit: int = 50) -> list[int]:
    """很久没被访问、重要性又低的事实。"""
    import time

    cutoff = time.time() - days * 86400
    rows = store.conn.execute(
        """SELECT id FROM memories
           WHERE status='active' AND kind='fact' AND importance < 0.45
             AND (last_access = 0 OR last_access < ?)
           ORDER BY importance ASC LIMIT ?""",
        (cutoff, int(limit)),
    ).fetchall()
    return [int(r["id"]) for r in rows]


def report(store) -> str:
    st = store.stats()
    dup = duplicates(store)
    orp = orphans(store)
    stale = stale_facts(store)
    conflicts = store.list_conflicts(limit=50)
    lines = [
        "【记忆体检】",
        f"记忆 {st.get('active')} 条（归档 {st.get('archived')}），实体 {st.get('entities')}，关系 {st.get('relations')}",
        f"重复（≥0.92）：{len(dup)} 组" + (f"，例如 #{dup[0][0]}↔#{dup[0][1]}" if dup else ""),
        f"没有实体的记忆：{len(orp)} 条" + (f"（如 #{orp[0]}）" if orp else ""),
        f"低价值旧事实：{len(stale)} 条" + (f"（如 #{stale[0]}）" if stale else ""),
        f"未处理冲突：{len(conflicts)} 组",
    ]
    return "\n".join(lines)


def fix(store, engine=None) -> dict[str, int]:
    """自动合并重复记忆、给孤儿挂上实体。"""
    merged = 0
    for a, b, _sim in duplicates(store):
        if store.get_memory(b):
            store.absorb_memory(b, a)
            merged += 1
    attached = 0
    for mid in orphans(store):
        mem = store.get_memory(mid)
        if not mem:
            continue
        text = str(mem.get("text") or "")
        rows = store.conn.execute("SELECT name, type FROM entities").fetchall()
        hit = [(str(r["name"]), str(r["type"])) for r in rows if len(str(r["name"])) >= 2 and str(r["name"]) in text]
        if hit and engine is not None:
            engine.link_entities(mid, hit[:2])
            attached += 1
    return {"merged": merged, "attached": attached}
