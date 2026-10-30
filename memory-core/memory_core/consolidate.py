# -*- coding: utf-8 -*-
"""睡觉整理：合并别名、合并重复记忆。

不调模型就能做的两件事（省 token、可重复跑）：
1. 实体归一：去掉 emoji/标点后同名的合并；「我 / 助手」这类由配置指定。
2. 记忆归并：向量相似度超过阈值的两条记忆合成一条。

模型相关的（人物画像、今日三件事）留给上层，需要时再调。
"""

from __future__ import annotations

import re
import time
from typing import Any, Iterable

import numpy as np

from .store import Store

_STRIP_RE = re.compile(r"[^\w\u4e00-\u9fff]+")


def normalize_name(name: str) -> str:
    return _STRIP_RE.sub("", str(name or "")).lower()


def canonical_name(name: str, alias_map: dict[str, str] | None = None) -> str:
    """先查别名表，再退回原名归一。"""
    raw = str(name or "").strip()
    for key, target in (alias_map or {}).items():
        if raw == key:
            return target
    return raw


def merge_duplicate_entities(
    store: Store,
    *,
    alias_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """把同名/别名实体并成一个。

    规则：
    - 显式 alias_map（别名 → 正名）优先；
    - 否则按 normalize_name 后完全相同的合并；
    - 保留 mention_count 最大的那个，其余并进去。
    """
    rows = store.conn.execute(
        "SELECT id, name, type, mention_count FROM entities ORDER BY mention_count DESC, id ASC"
    ).fetchall()

    # 只按名字分组：同一个「我」可能是 person 也可能是 thing，别名合并要跨类型
    groups: dict[str, list[dict]] = {}
    for r in rows:
        name = canonical_name(r["name"], alias_map)
        groups.setdefault(normalize_name(name), []).append(dict(r))

    merged = 0
    moved_edges = 0
    for _, items in groups.items():
        if len(items) < 2:
            continue
        keep = items[0]
        canonical = canonical_name(keep["name"], alias_map)
        for item in items[1:]:
            merged += 1
            moved_edges += store.absorb_entity(int(item["id"]), int(keep["id"]))
        # 统一成正名（别名表给了就用别名表，否则保留原样）
        if canonical and canonical != keep["name"]:
            store.rename_entity(int(keep["id"]), canonical)
    return {"merged": merged, "moved_edges": moved_edges}


def merge_similar_memories(
    store: Store,
    *,
    threshold: float = 0.92,
    max_pairs: int = 200,
) -> dict[str, Any]:
    """把向量相似度超过阈值的记忆并成一条。

    保留 importance 高的（相同则留新的），合并访问次数和实体，删掉被并的那条。
    """
    ids, mat = store.all_vectors()
    if not ids or mat is None or len(ids) < 2:
        return {"merged": 0, "pairs": 0}

    norms = np.linalg.norm(mat, axis=1) + 1e-8
    unit = mat / norms[:, None]
    sim = unit @ unit.T
    np.fill_diagonal(sim, 0.0)

    tried: set[tuple[int, int]] = set()
    pairs = 0
    merged = 0
    # 只看上三角，按相似度从高到低
    idx = np.argwhere(np.triu(sim, k=1) >= threshold)
    scores = [(float(sim[i, j]), int(ids[i]), int(ids[j])) for i, j in idx]
    scores.sort(reverse=True)

    for score, a, b in scores[:max_pairs]:
        if (a, b) in tried:
            continue
        tried.add((a, b))
        pairs += 1
        ma, mb = store.get_memory(a), store.get_memory(b)
        if not ma or not mb or ma["status"] != "active" or mb["status"] != "active":
            continue
        keep, drop = (a, b) if (ma["importance"], ma["created_at"]) >= (mb["importance"], mb["created_at"]) else (b, a)
        store.absorb_memory(int(drop), int(keep), similarity=score)
        merged += 1

    return {"merged": merged, "pairs": pairs, "threshold": threshold}


def sleep(
    store: Store,
    *,
    alias_map: dict[str, str] | None = None,
    similarity: float = 0.92,
) -> dict[str, Any]:
    """一次完整的离线整理（不含模型）。"""
    started = time.time()
    ents = merge_duplicate_entities(store, alias_map=alias_map)
    mems = merge_similar_memories(store, threshold=similarity)
    return {
        "entities_merged": ents["merged"],
        "edges_moved": ents["moved_edges"],
        "memories_merged": mems["merged"],
        "pairs_checked": mems["pairs"],
        "seconds": round(time.time() - started, 2),
    }
