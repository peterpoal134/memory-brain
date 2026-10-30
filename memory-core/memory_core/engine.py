# -*- coding: utf-8 -*-
"""记忆大脑的引擎：写入、检索、遗忘。

打分不是单一余弦，而是「语义 + 关键词 + 重要性 + 新鲜度 + 图谱」五路融合，
并且把每一路的分数带出来（`explain=True`），方便调参和排查「她为什么想起这个」。

存储细节全在 store.py；这里只做策略。
"""

from __future__ import annotations

import math
import time
from typing import Any, Callable, Iterable

import numpy as np

from .store import Store

# 打分权重（加起来 1.0；按评估集调过：中文短 query 下关键词比语义更可靠）
W_SEMANTIC = 0.18
W_KEYWORD = 0.46
W_IMPORTANCE = 0.14
W_RECENCY = 0.11
W_GRAPH = 0.11

# 查询意图 → 类型偏好（问规则/喜好偏向事实，问往事偏向事件）
INTENT_HINTS = {
    "fact": ("规则", "喜欢", "讨厌", "习惯", "叫什么", "生日", "不能", "永远", "是不是", "会不会"),
    "episode": ("那天", "上次", "当时", "记得", "之前", "昨天", "今天", "说好", "答应", "后来"),
}
KIND_BOOST = 0.05
DEDUP_SIMILARITY = 0.82

# 新鲜度半衰期（天）
RECENCY_HALF_LIFE_DAYS = 14.0


def _rank_map(pairs: list[tuple[int, float]]) -> dict[int, int]:
    """按分数排名，分数相同的并列同一名次（避免同分时按 rowid 抢先）。"""
    best: dict[int, float] = {}
    for mid, score in pairs:
        best[int(mid)] = float(score)
    ranks: dict[int, int] = {}
    prev: float | None = None
    rank = -1
    for i, (mid, score) in enumerate(sorted(best.items(), key=lambda kv: kv[1])):
        key = round(score, 6)
        if key != prev:
            rank = i
            prev = key
        ranks[mid] = rank
    return ranks


def _cosine(query: np.ndarray, mat: np.ndarray) -> np.ndarray:
    q = np.asarray(query, dtype=np.float32).reshape(-1)
    if q.size == 0 or mat.size == 0:
        return np.zeros((0,), dtype=np.float32)
    qn = q / (np.linalg.norm(q) + 1e-8)
    norms = np.linalg.norm(mat, axis=1) + 1e-8
    return (mat @ qn) / norms


class MemoryEngine:
    def __init__(self, store: Store, embed: Callable[[str], np.ndarray | None] | None = None):
        self.store = store
        self.embed = embed

    # ---------------------------------------------------------------- 写入

    def remember(
        self,
        text: str,
        *,
        kind: str = "episode",
        importance: float = 0.5,
        valence: float = 0.0,
        arousal: float = 0.0,
        source: str = "",
        meta: dict | None = None,
        entities: Iterable[tuple[str, str]] | None = None,
        relations: Iterable[tuple[str, str, str]] | None = None,
        vector: np.ndarray | None = None,
        now: float | None = None,
        visibility: str = "public",
    ) -> int:
        text = (text or "").strip()
        if not text:
            raise ValueError("记忆内容不能为空")

        if vector is None and self.embed is not None:
            try:
                vector = self.embed(text)
            except Exception:
                vector = None

        memory_id = self.store.insert_memory(
            text=text,
            kind=kind,
            importance=float(max(0.0, min(1.0, importance))),
            valence=float(max(-1.0, min(1.0, valence))),
            arousal=float(max(0.0, min(1.0, arousal))),
            source=source,
            meta=meta,
            vector=vector,
            created_at=now,
            visibility=visibility,
        )

        name_to_id: dict[str, int] = {}
        for name, etype in entities or []:
            name = (name or "").strip()
            if not name:
                continue
            entity_id = self.store.upsert_entity(name, etype or "thing", now=now)
            self.store.link_entity(memory_id, entity_id)
            name_to_id[name] = entity_id

        for src, dst, relation in relations or []:
            src, dst, relation = (src or "").strip(), (dst or "").strip(), (relation or "").strip()
            if not src or not dst or not relation or src == dst:
                continue
            src_id = name_to_id.get(src) or self.store.upsert_entity(src, "thing", now=now)
            dst_id = name_to_id.get(dst) or self.store.upsert_entity(dst, "thing", now=now)
            self.store.link_relation(src_id, dst_id, relation, memory_id=memory_id)

        return memory_id

    def link_entities(self, memory_id: int, entities: Iterable[tuple[str, str]]) -> None:
        for name, etype in entities:
            name = (name or "").strip()
            if not name:
                continue
            eid = self.store.upsert_entity(name, etype or "thing")
            self.store.link_entity(memory_id, eid)

    # ---------------------------------------------------------------- 检索

    def recall(
        self,
        query: str,
        k: int = 5,
        *,
        now: float | None = None,
        explain: bool = False,
        touch: bool = True,
        query_vec: np.ndarray | None = None,
        rerank: bool = True,
        visibility_allow: set[str] | None = None,
        mood_valence: float | None = None,
    ) -> list[dict[str, Any]]:
        now = now or time.time()
        query = (query or "").strip()
        if not query:
            return []

        kw = self.store.fts_search(query, limit=60)
        kw_rank = _rank_map(kw)
        kw_size = max(1, len(kw_rank) - 1)

        sem: dict[int, float] = {}
        if query_vec is None and self.embed is not None:
            try:
                query_vec = self.embed(query)
            except Exception:
                query_vec = None
        if query_vec is not None:
            ids, mat = self.store.all_vectors()
            if ids:
                scores = _cosine(query_vec, mat)
                for mid, score in zip(ids, scores.tolist()):
                    sem[int(mid)] = float(score)

        candidates: set[int] = set(kw_rank) | set(sem)
        # 图谱扩散：先用现有候选的实体找一圈邻居
        seed_ids = list(kw_rank)[:5] + [mid for mid, _ in sorted(sem.items(), key=lambda x: -x[1])[:5]]
        neighbors = self.store.neighbor_memories(seed_ids, limit=30)
        candidates |= set(neighbors)
        if not candidates:
            return []

        seed_entities = self.store.entities_of(seed_ids)
        mem_entity_cache: dict[int, set[int]] = {}

        rows: list[dict[str, Any]] = []
        for mid in candidates:
            memory = self.store.get_memory(mid)
            if not memory or memory.get("status") != "active":
                continue
            if visibility_allow is not None and str(memory.get("visibility") or "public") not in visibility_allow:
                continue

            s_sem = max(0.0, sem.get(mid, 0.0))
            rank = kw_rank.get(mid)
            s_kw = 1.0 - (rank / kw_size) if rank is not None else 0.0
            s_imp = float(memory.get("importance", 0.5))

            # 情绪一致：心情差时更容易想起负面的事，反之亦然（只给很小的权重）
            if mood_valence is not None:
                mv = max(-1.0, min(1.0, float(mood_valence)))
                ev = float(memory.get("valence") or 0.0)
                if mv < -0.15 and ev < -0.1:
                    s_imp = min(1.0, s_imp + 0.03)
                elif mv > 0.15 and ev > 0.1:
                    s_imp = min(1.0, s_imp + 0.03)

            age_days = max(0.0, (now - float(memory.get("created_at", now))) / 86400.0)
            s_rec = 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)

            if mid not in mem_entity_cache:
                mem_entity_cache[mid] = set(self.store.memory_entities(mid))
            shared = len(mem_entity_cache[mid] & seed_entities) if seed_entities else 0
            s_graph = min(1.0, shared / 3.0)

            score = (
                W_SEMANTIC * s_sem
                + W_KEYWORD * s_kw
                + W_IMPORTANCE * s_imp
                + W_RECENCY * s_rec
                + W_GRAPH * s_graph
            )
            # 用户钉住的事实（账号、称呼、约定）多给一点权重，避免被闲聊挤下去
            try:
                import json as _json
                meta = memory.get("meta") or {}
                if isinstance(meta, str):
                    meta = _json.loads(meta)
                # 钉住只在“问对了”时拉满：身份事实看问法，阅读状态看读书，其余看关键词
                if meta.get("pin"):
                    kind = str(meta.get("kind") or "")
                    q_low = query.lower()
                    if kind in ("reading_state", "book", "study"):
                        relevant = any(w in query for w in (
                            "在读", "读到", "读什么", "读哪", "读的什么", "进度",
                            "第几章", "翻到哪", "最近读", "看到哪",
                            "看书", "读书", "读的", "上次看", "看什么书", "读到哪",
                        ))
                    elif meta.get("subject"):
                        relevant = any(w in q_low for w in (
                            "是谁", "谁是", "谁啊", "叫什么", "叫啥", "昵称", "qq", "账号",
                            "号码", "名字", "抖音号",
                        ))
                    else:
                        relevant = s_kw >= 0.5 or s_sem >= 0.6
                    if relevant:
                        score = max(score, 0.9)
            except Exception:
                pass
            if score <= 0:
                continue

            item: dict[str, Any] = {
                "id": mid,
                "text": memory.get("text", ""),
                "kind": memory.get("kind", "episode"),
                "importance": s_imp,
                "created_at": memory.get("created_at"),
                "visibility": memory.get("visibility") or "public",
                "score": round(score, 4),
            }
            if explain:
                item["why"] = {
                    "semantic": round(s_sem, 3),
                    "keyword": round(s_kw, 3),
                    "importance": round(s_imp, 3),
                    "recency": round(s_rec, 3),
                    "graph": round(s_graph, 3),
                    "neighbor": mid in neighbors,
                }
            rows.append(item)

        rows.sort(key=lambda x: x["score"], reverse=True)
        if rerank:
            rows = self._rerank(query, rows)
        top = rows[: int(k)]
        if touch and top:
            self.store.touch([x["id"] for x in top], now=now)
        return top

    def _rerank(self, query: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """本地重排：意图偏好 + 去重，不调模型。"""
        q = str(query or "")
        # 先看这句话更偏“问事实”还是“问往事”，只给占优的那一类加权
        counts = {
            kind: sum(1 for h in hints if h in q)
            for kind, hints in INTENT_HINTS.items()
        }
        best_kind, best_hits = "", 0
        for kind, n in counts.items():
            if n > best_hits:
                best_kind, best_hits = kind, n
        if best_hits:
            for item in rows:
                if str(item.get("kind") or "") == best_kind:
                    item["score"] = round(float(item["score"]) + KIND_BOOST, 4)

        rows.sort(key=lambda x: x["score"], reverse=True)

        picked: list[dict[str, Any]] = []
        for item in rows:
            text = str(item.get("text") or "")
            if any(self._similar(text, str(p.get("text") or "")) >= DEDUP_SIMILARITY for p in picked):
                continue
            picked.append(item)
        return picked

    @staticmethod
    def _similar(a: str, b: str) -> float:
        from .store import bigrams

        ga, gb = set(bigrams(a)), set(bigrams(b))
        if not ga or not gb:
            return 0.0
        inter = len(ga & gb)
        return inter / min(len(ga), len(gb))

    # ---------------------------------------------------------------- 遗忘

    def decay(self, rate: float = 0.01, now: float | None = None, **kwargs) -> int:
        return self.store.decay(rate, now=now, **kwargs)

    def archive(
        self,
        *,
        importance_below: float = 0.3,
        older_than_days: float = 30.0,
        now: float | None = None,
        protect_days: float = 30.0,
    ) -> int:
        return self.store.archive(
            importance_below, older_than_days, now=now, protect_days=protect_days
        )

    def purge(self, keep_days: float = 180.0, now: float | None = None) -> int:
        return self.store.purge_archived(keep_days, now=now)

    def cleanup_orphans(self) -> int:
        return self.store.drop_orphan_entities()

    # ---------------------------------------------------------------- 统计

    def stats(self) -> dict[str, Any]:
        s = self.store.stats()
        s["avg_importance"] = self._avg_importance()
        return s

    def _avg_importance(self) -> float:
        row = self.store.conn.execute(
            "SELECT AVG(importance) AS v FROM memories WHERE status='active'"
        ).fetchone()
        return round(float(row["v"] or 0.0), 3)
