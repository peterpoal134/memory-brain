# -*- coding: utf-8 -*-
"""记忆大脑的页面接口：概览、图谱、记忆列表、检索、删除、维护。"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger
from astrbot.api.web import request

PLUGIN_NAME = "astrbot_plugin_memory_brain"
PREFIX = f"/{PLUGIN_NAME}/page"


def _ok(data: Any) -> dict[str, Any]:
    return {"status": "ok", "data": data}


def _err(message: str) -> dict[str, Any]:
    return {"status": "error", "message": message}


class PageApi:
    def __init__(self, plugin) -> None:
        self.plugin = plugin

    def register_routes(self) -> None:
        register = self.plugin.context.register_web_api
        routes = [
            ("/overview", self.overview, ["GET"], "概览"),
            ("/graph", self.graph, ["GET"], "图谱"),
            ("/memories", self.memories, ["GET"], "记忆列表"),
            ("/entity", self.entity, ["GET"], "实体详情"),
            ("/search", self.search, ["POST"], "检索"),
            ("/memory/delete", self.delete_memory, ["POST"], "删除记忆"),
            ("/maintain", self.maintain, ["POST"], "衰减归档"),
            ("/sleep", self.sleep, ["POST"], "睡觉整理"),
            ("/profile", self.make_profile, ["POST"], "生成画像"),
            ("/reflection", self.make_reflection, ["POST"], "今日反思"),
            ("/abstract", self.make_facts, ["POST"], "沉淀事实"),
            ("/media", self.import_media, ["POST"], "导入媒体知识"),
            ("/lessons", self.make_lessons, ["POST"], "提炼道理"),
            ("/report", self.report, ["GET"], "运行报表"),
            ("/health", self.health, ["GET"], "记忆体检"),
            ("/health/fix", self.health_fix, ["POST"], "体检修复"),
            ("/conflicts", self.conflicts, ["GET"], "冲突列表"),
            ("/conflicts/resolve", self.resolve_conflict, ["POST"], "保留一条冲突"),
            ("/forget", self.forget, ["POST"], "忘记一条记忆"),
        ]
        for path, handler, methods, desc in routes:
            register(f"{PREFIX}{path}", handler, methods, f"[memory_brain] {desc}")

    # ---------------------------------------------------------------- 读取

    async def overview(self) -> dict[str, Any]:
        try:
            s = self.plugin.engine.stats()
            return _ok(
                {
                    "active": s["active"],
                    "archived": s["archived"],
                    "entities": s["entities"],
                    "relations": s["relations"],
                    "vectors": s["vectors"],
                    "avg_importance": s["avg_importance"],
                    "kinds": s["kinds"],
                    "oldest": s["oldest"],
                    "embedding": bool(self.plugin._embedding) or s["vectors"] > 0,
                    "k": self.plugin.k,
                }
            )
        except Exception as exc:
            logger.exception("[brain] 概览失败")
            return _err(str(exc))

    async def graph(self) -> dict[str, Any]:
        try:
            store = self.plugin.store
            raw = store.graph(limit=240)
            nodes = [
                {
                    "id": int(n["id"]),
                    "name": n["name"],
                    "type": n["type"],
                    "degree": int(n["degree"]),
                }
                for n in raw["nodes"]
            ]
            keep = {n["id"] for n in nodes}
            edges: list[dict[str, Any]] = []
            for e in raw["edges"]:
                if int(e["src_id"]) in keep and int(e["dst_id"]) in keep:
                    edges.append(
                        {
                            "src": int(e["src_id"]),
                            "dst": int(e["dst_id"]),
                            "kind": "relation",
                            "label": e["relation"],
                            "weight": float(e["weight"]),
                        }
                    )
            for e in store.cooccurrence_edges(limit=1200):
                if e["src"] in keep and e["dst"] in keep:
                    edges.append(
                        {
                            "src": e["src"],
                            "dst": e["dst"],
                            "kind": "cooccur",
                            "label": "",
                            "weight": float(e["weight"]),
                        }
                    )
            # 第二层：记忆本身也画成球，连到它涉及的实体
            mem_rows = store.conn.execute(
                """SELECT m.id, m.text, m.kind, m.importance, m.created_at,
                          GROUP_CONCAT(me.entity_id) AS ents
                   FROM memories m
                   LEFT JOIN memory_entities me ON me.memory_id = m.id
                   WHERE m.status = 'active'
                   GROUP BY m.id
                   ORDER BY m.created_at DESC
                   LIMIT 300"""
            ).fetchall()
            memories = [
                {
                    "id": int(r["id"]),
                    "text": str(r["text"])[:120],
                    "kind": r["kind"],
                    "importance": round(float(r["importance"]), 2),
                    "created_at": float(r["created_at"]),
                    "entities": [int(x) for x in str(r["ents"] or "").split(",") if x.strip()],
                }
                for r in mem_rows
            ]
            return _ok({"nodes": nodes, "edges": edges, "memories": memories})
        except Exception as exc:
            logger.exception("[brain] 图谱失败")
            return _err(str(exc))

    async def memories(self) -> dict[str, Any]:
        try:
            limit = min(200, max(1, int(request.query.get("limit", 30))))
            offset = max(0, int(request.query.get("offset", 0)))
            kind = request.query.get("kind") or None
            rows = self.plugin.store.list_memories(limit=limit, offset=offset, kind=kind)
            return _ok(
                {
                    "items": [
                        {
                            "id": int(r["id"]),
                            "text": r["text"],
                            "kind": r["kind"],
                            "importance": round(float(r["importance"]), 2),
                            "valence": round(float(r["valence"]), 2),
                            "created_at": float(r["created_at"]),
                            "date": time.strftime("%Y-%m-%d %H:%M", time.localtime(float(r["created_at"]))),
                            "access": int(r["access_count"]),
                        }
                        for r in rows
                    ]
                }
            )
        except Exception as exc:
            logger.exception("[brain] 记忆列表失败")
            return _err(str(exc))

    async def entity(self) -> dict[str, Any]:
        try:
            eid = int(request.query.get("id", 0))
            if not eid:
                return _err("缺少 id")
            row = self.plugin.store.get_entity(eid) or {}
            rows = self.plugin.store.memories_of_entity(eid, limit=12)
            return _ok(
                {
                    "id": eid,
                    "name": row.get("name") or "",
                    "type": row.get("type") or "",
                    "profile": row.get("profile") or "",
                    "profile_at": float(row.get("profile_at") or 0),
                    "memories": [
                        {
                            "id": int(r["id"]),
                            "text": r["text"],
                            "date": time.strftime("%Y-%m-%d", time.localtime(float(r["created_at"]))),
                        }
                        for r in rows
                    ],
                }
            )
        except Exception as exc:
            logger.exception("[brain] 实体详情失败")
            return _err(str(exc))

    # ---------------------------------------------------------------- 动作

    async def search(self) -> dict[str, Any]:
        try:
            payload = await request.json(default={}) or {}
            query = str(payload.get("query") or "").strip()
            if not query:
                return _err("请输入要检索的内容")
            vec = await self.plugin._embed(query)
            hits = self.plugin.engine.recall(
                query, k=min(10, self.plugin.k + 5), query_vec=vec, touch=False, explain=True
            )
            return _ok({"query": query, "hits": hits})
        except Exception as exc:
            logger.exception("[brain] 检索失败")
            return _err(str(exc))

    async def delete_memory(self) -> dict[str, Any]:
        try:
            payload = await request.json(default={}) or {}
            mid = int(payload.get("id") or 0)
            if not mid:
                return _err("缺少 id")
            if not self.plugin.store.delete_memory(mid):
                return _err("没有这条记忆")
            return _ok({"ok": True})
        except Exception as exc:
            logger.exception("[brain] 删除失败")
            return _err(str(exc))

    async def sleep(self) -> dict[str, Any]:
        try:
            from . import consolidate

            return _ok(consolidate.sleep(self.plugin.store, alias_map=self.plugin.alias_map))
        except Exception as exc:
            logger.exception("[brain] 睡觉整理失败")
            return _err(str(exc))

    async def make_profile(self) -> dict[str, Any]:
        try:
            payload = await request.json(default={}) or {}
            eid = int(payload.get("id") or 0)
            if not eid:
                return _err("缺少 id")
            done = await self.plugin._generate_profiles(force_id=eid)
            row = self.plugin.store.get_entity(eid) or {}
            return _ok({"ok": bool(done), "profile": row.get("profile") or ""})
        except Exception as exc:
            logger.exception("[brain] 生成画像失败")
            return _err(str(exc))

    async def make_reflection(self) -> dict[str, Any]:
        try:
            done = await self.plugin._generate_reflection()
            rows = self.plugin.store.list_memories(kind="reflection", limit=1)
            return _ok({"ok": bool(done), "text": rows[0]["text"] if rows else ""})
        except Exception as exc:
            logger.exception("[brain] 生成反思失败")
            return _err(str(exc))

    async def health(self) -> dict[str, Any]:
        from . import health

        try:
            return _ok({"text": health.report(self.plugin.store)})
        except Exception as exc:
            logger.exception("[brain] 体检失败")
            return _err(str(exc))

    async def health_fix(self) -> dict[str, Any]:
        from . import health

        try:
            return _ok(health.fix(self.plugin.store, self.plugin.engine))
        except Exception as exc:
            logger.exception("[brain] 体检修复失败")
            return _err(str(exc))

    async def conflicts(self) -> dict[str, Any]:
        items = []
        for g in self.plugin.store.list_conflicts(limit=20):
            a = g.get("a") or {}
            b = g.get("b") or {}
            items.append({
                "id": g["id"],
                "a": {"id": a.get("id"), "text": str(a.get("text") or "")[:120]},
                "b": {"id": b.get("id"), "text": str(b.get("text") or "")[:120]},
            })
        return _ok({"items": items})

    async def resolve_conflict(self) -> dict[str, Any]:
        data = request.json or {}
        try:
            cid = int(data.get("id"))
            keep = str(data.get("keep") or "a")
        except (TypeError, ValueError):
            return _err("参数不对")
        rows = [g for g in self.plugin.store.list_conflicts(limit=50) if int(g["id"]) == cid]
        if not rows:
            return _err("冲突不存在")
        g = rows[0]
        keep_id = int((g.get("a") if keep == "a" else g.get("b"))["id"])
        drop_id = int((g.get("b") if keep == "a" else g.get("a"))["id"])
        self.plugin.store.delete_memory(drop_id)
        self.plugin.store.resolve_conflict(cid)
        return _ok({"kept": keep_id, "dropped": drop_id})

    async def forget(self) -> dict[str, Any]:
        data = request.json or {}
        text = str(data.get("text") or "").strip()
        if len(text) < 2:
            return _err("要忘什么？给我一个关键词")
        deleted = 0
        for row in self.plugin.store.list_memories(limit=200):
            if text in str(row.get("text") or ""):
                self.plugin.store.delete_memory(int(row["id"]))
                deleted += 1
                if deleted >= 3:
                    break
        return _ok({"deleted": deleted})

    async def report(self) -> dict[str, Any]:
        from .report import build

        try:
            text = build(self.plugin.store)
            return _ok({"text": text})
        except Exception as exc:
            logger.exception("[brain] 生成报表失败")
            return _err(str(exc))

    async def make_lessons(self) -> dict[str, Any]:
        try:
            n = await self.plugin._generate_lessons()
            rows = self.plugin.store.list_memories(source="lesson", limit=8)
            return _ok({"count": n, "items": [r["text"] for r in rows]})
        except Exception as exc:
            logger.exception("[brain] 提炼道理失败")
            return _err(str(exc))

    async def import_media(self) -> dict[str, Any]:
        try:
            n = await self.plugin._import_media()
            rows = self.plugin.store.list_memories(source="media", limit=10)
            return _ok({"count": n, "total": len(rows), "items": [r["text"][:60] for r in rows]})
        except Exception as exc:
            logger.exception("[brain] 媒体导入失败")
            return _err(str(exc))

    async def make_facts(self) -> dict[str, Any]:
        try:
            done = await self.plugin._generate_facts()
            rows = self.plugin.store.list_memories(kind="fact", limit=8)
            return _ok({"ok": bool(done), "count": done, "facts": [r["text"] for r in rows]})
        except Exception as exc:
            logger.exception("[brain] 沉淀事实失败")
            return _err(str(exc))

    async def maintain(self) -> dict[str, Any]:
        try:
            affected = self.plugin.engine.decay(float(self.plugin.config.get("decay_rate", 0.01) or 0.01))
            archived = self.plugin.engine.archive(
                importance_below=float(self.plugin.config.get("archive_below", 0.3) or 0.3),
                older_than_days=float(self.plugin.config.get("archive_days", 30) or 30),
            )
            return _ok({"decayed": affected, "archived": archived})
        except Exception as exc:
            logger.exception("[brain] 维护失败")
            return _err(str(exc))
