# -*- coding: utf-8 -*-
"""离线整理：实体归一、重复记忆合并、冲突检测。不调模型。"""
from __future__ import annotations

from . import conflict, consolidate


class MaintenanceService:
    def __init__(self, memory):
        self.memory = memory

    def run(self, similarity: float = 0.92, alias_map: dict | None = None) -> dict:
        out = consolidate.sleep(self.memory.store, alias_map=alias_map, similarity=similarity)
        facts = []
        try:
            rows = self.memory.store.conn.execute(
                """SELECT m.id, m.text, GROUP_CONCAT(me.entity_id) AS ents
                   FROM memories m LEFT JOIN memory_entities me ON me.memory_id=m.id
                   WHERE m.status='active' AND m.kind='fact'
                   GROUP BY m.id LIMIT 2000"""
            ).fetchall()
            for r in rows:
                ids = {int(x) for x in str(r["ents"] or "").split(",") if x.strip()}
                if ids:
                    facts.append({"id": int(r["id"]), "text": str(r["text"] or ""), "entity_ids": ids})
        except Exception:
            facts = []
        added = 0
        for a, b, reason in conflict.find(facts):
            if self.memory.store.add_conflict(a, b, reason):
                added += 1
        out["conflicts"] = added
        return out

    def conflicts(self, limit: int = 50) -> list[dict]:
        out = []
        for row in self.memory.store.list_conflicts(limit=limit):
            a = self.memory.store.get_memory(int(row["a_id"]))
            b = self.memory.store.get_memory(int(row["b_id"]))
            out.append({
                "id": int(row["id"]), "reason": row.get("reason") or "",
                "a": {"id": int(row["a_id"]), "text": (a or {}).get("text", "")},
                "b": {"id": int(row["b_id"]), "text": (b or {}).get("text", "")},
            })
        return out

    def resolve(self, conflict_id: int, keep: str = "a") -> bool:
        rows = [c for c in self.memory.store.list_conflicts(limit=100) if int(c["id"]) == int(conflict_id)]
        if not rows:
            return False
        row = rows[0]
        keep_id = int(row["a_id"] if keep == "a" else row["b_id"])
        drop_id = int(row["b_id"] if keep == "a" else row["a_id"])
        self.memory.store.resolve_conflict(int(conflict_id), keep_id)
        try:
            self.memory.store.delete_memory(drop_id)
        except Exception:
            pass
        return True
