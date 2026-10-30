# -*- coding: utf-8 -*-
"""插件管理页面的后端接口。

页面通过 AstrBot 的 `AstrBotPluginView.apiGet/apiPost` 调用这些路由，
路由前缀固定为 `/{插件名}/page`。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger
from astrbot.api.web import request

from .core_state import affinity_level, clamp, mood_labels, mood_view
from .emotional import MARKER as GRUDGE_MARKER  # noqa: F401

PLUGIN_NAME = "astrbot_plugin_persona_state"
PREFIX = f"/{PLUGIN_NAME}/page"


def _ok(data: Any) -> dict[str, Any]:
    return {"status": "ok", "data": data}


def _err(message: str) -> dict[str, Any]:
    return {"status": "error", "message": message}


class PageApi:
    def __init__(self, plugin) -> None:
        self.plugin = plugin

    # ---------------------------------------------------------------- 路由

    def register_routes(self) -> None:
        register = self.plugin.context.register_web_api
        routes = [
            ("/state", self.get_state, ["GET"], "状态总览"),
            ("/interests", self.get_interests, ["GET"], "兴趣列表"),
            ("/life", self.get_life, ["GET"], "生活动态"),
            ("/energy", self.get_energy, ["GET"], "精力曲线"),
            ("/mood", self.set_mood, ["POST"], "设置情绪"),
            ("/affinity/set", self.set_affinity, ["POST"], "设置好感"),
            ("/affinity/delete", self.delete_affinity, ["POST"], "删除好感记录"),
            ("/grudges", self.get_grudges, ["GET"], "旧账列表"),
            ("/grudge/resolve", self.resolve_grudge, ["POST"], "标记放下"),
            ("/grudge/delete", self.delete_grudge, ["POST"], "删除旧账"),
            ("/core", self.get_core, ["GET"], "识海列表"),
            ("/core/add", self.add_core, ["POST"], "写入识海"),
            ("/core/delete", self.delete_core, ["POST"], "删除识海条目"),
        ]
        for path, handler, methods, desc in routes:
            register(f"{PREFIX}{path}", handler, methods, f"[persona_state] {desc}")

    # ---------------------------------------------------------------- 读取

    async def get_state(self) -> dict[str, Any]:
        try:
            p = self.plugin
            now = time.time()
            view = mood_view(p.mood.data, now)
            labels = mood_labels(view)
            users = []
            for uid, rec in (p.affinity.data.get("users") or {}).items():
                if not isinstance(rec, dict):
                    continue
                score = float(rec.get("score", 0))
                users.append(
                    {
                        "id": str(uid),
                        "name": rec.get("name") or str(uid),
                        "score": round(score, 1),
                        "level": affinity_level(score),
                        "msgs": int(rec.get("msgs", 0)),
                        "last_seen": float(rec.get("last_seen", 0)),
                    }
                )
            users.sort(key=lambda r: r["score"], reverse=True)
            return _ok(
                {
                    "mood": {
                        **{k: round(float(view[k]), 1) for k in ("valence", "energy", "clinginess")},
                        "labels": labels,
                        "emotion": self._emotion_labels(),
                        "day_msgs": int(view["day_msgs"]),
                        "silent_hours": round(float(view["silent_hours"]), 1),
                    },
                    "affinity": users,
                    "counts": {
                        "users": len(users),
                        "grudges": len([e for e in p.emotional.data.get("events", []) if not e.get("resolved")]),
                        "core": len(p.core.active()),
                    },
                    "config": {"grudge": p.grudge, "floors": p.floors},
                }
            )
        except Exception as exc:
            logger.exception("[state] 页面读取失败")
            return _err(str(exc))

    def _emotion_labels(self) -> list[dict[str, Any]]:
        try:
            labels = ((self.plugin.mood.data or {}).get("pad") or {}).get("labels") or {}
            rows = sorted(labels.items(), key=lambda x: -float(x[1]))[:6]
            return [{"name": k, "score": round(float(v), 2)} for k, v in rows if float(v) >= 0.01]
        except Exception:
            return []

    async def get_energy(self) -> dict[str, Any]:
        import json

        path = self.plugin.base / "energy_history.jsonl"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-72:]
        except OSError:
            lines = []
        points = []
        for line in lines:
            try:
                row = json.loads(line)
                points.append({"ts": float(row.get("ts", 0)),
                               "energy": float(row.get("energy", 0)),
                               "valence": float(row.get("valence", 0))})
            except Exception:
                continue
        return _ok({"points": points})

    @staticmethod
    def _life_dir():
        import os
        from pathlib import Path

        env = os.environ.get("ASTRBOT_LIFE_DIR")
        if env:
            return Path(env)
        root = Path(__file__).resolve()
        for p in (root.parents[4] / "life", root.parents[2] / "life"):
            if p.exists():
                return p
        return root.parents[4] / "life"

    @staticmethod
    def _read_jsonl(path, limit: int = 8) -> list[dict]:
        import json

        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
        except OSError:
            return []
        rows = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    async def get_life(self) -> dict[str, Any]:
        base = self._life_dir()
        actions = [
            {
                "date": r.get("date", ""),
                "action": r.get("action", ""),
                "reason": str(r.get("reason") or "")[:80],
                "message": str(r.get("message") or "")[:80],
            }
            for r in self._read_jsonl(base / "actions.jsonl", 12)
        ]
        notes = [
            {
                "date": r.get("date", ""),
                "kind": r.get("kind", ""),
                "keep": r.get("keep", ""),
                "text": str(r.get("text") or "")[:120],
            }
            for r in self._read_jsonl(base / "notes.jsonl", 12)
        ]
        watched = []
        for r in self._read_jsonl(base / "watched.jsonl", 5):
            items = [str(i.get("desc") or "")[:100] for i in (r.get("items") or [])]
            watched.append({"date": r.get("date", ""), "items": items})
        return _ok({"actions": actions, "notes": notes, "watched": watched})

    async def get_interests(self) -> dict[str, Any]:
        import json

        from pathlib import Path

        import os

        env_dir = os.environ.get("ASTRBOT_LIFE_DIR")
        if env_dir:
            data = Path(env_dir) / "interests.json"
        else:
            root = Path(__file__).resolve()
            candidates = [
                root.parents[4] / "life" / "interests.json",  # AstrBot 同级
                root.parents[2] / "life" / "interests.json",  # data/life
            ]
            data = next((p for p in candidates if p.exists()), candidates[0])
        try:
            rows = json.loads(data.read_text(encoding="utf-8"))
        except Exception:
            rows = []
        items = [
            {
                "name": r.get("name"),
                "weight": round(float(r.get("weight", 0)), 2),
                "tags": r.get("tags") or [],
            }
            for r in rows
            if isinstance(r, dict)
        ]
        items.sort(key=lambda x: -x["weight"])
        return _ok({"items": items})

    async def get_grudges(self) -> dict[str, Any]:
        try:
            events = []
            for e in self.plugin.emotional.data.get("events", []):
                events.append(
                    {
                        "id": int(e.get("id", 0)),
                        "user_id": str(e.get("user_id", "")),
                        "user_name": e.get("user_name") or "",
                        "text": e.get("trigger_text") or e.get("reason") or "",
                        "emotion": e.get("emotion") or "",
                        "delta": round(float(e.get("delta", 0)), 1),
                        "intensity": round(float(e.get("intensity", 0)), 2),
                        "date": e.get("date") or "",
                        "source": e.get("source") or "",
                        "recalls": int(e.get("recalls", 0)),
                        "resolved": bool(e.get("resolved")),
                    }
                )
            events.sort(key=lambda x: (x["resolved"], -x["id"]))
            return _ok({"events": events})
        except Exception as exc:
            logger.exception("[state] 旧账读取失败")
            return _err(str(exc))

    async def get_core(self) -> dict[str, Any]:
        try:
            entries = [
                {
                    "id": int(e.get("id", 0)),
                    "text": e.get("text", ""),
                    "by": e.get("by", ""),
                    "date": e.get("created_date", ""),
                }
                for e in self.plugin.core.active()
            ]
            return _ok({"entries": entries, "max_chars": self.plugin.core_max_chars})
        except Exception as exc:
            logger.exception("[state] 识海读取失败")
            return _err(str(exc))

    # ---------------------------------------------------------------- 写入

    async def set_mood(self) -> dict[str, Any]:
        try:
            payload = await request.json(default={}) or {}
            p = self.plugin
            now = time.time()
            data = p.mood.data
            if "valence" in payload:
                data["valence"] = clamp(float(payload["valence"]), -100, 100)
            if "energy" in payload:
                data["energy"] = clamp(float(payload["energy"]))
            if "clinginess" in payload:
                # 面板给的是展示值，换算成内部偏移（基线 35 + 沉默加成由时间决定）
                target = clamp(float(payload["clinginess"]))
                current = mood_view(data, now)["clinginess"]
                data["cling_adj"] = clamp(
                    float(data.get("cling_adj", 0)) + (target - current), -20, 60
                )
            data["updated_at"] = now
            p._save()
            return _ok({"ok": True})
        except Exception as exc:
            logger.exception("[state] 设置情绪失败")
            return _err(str(exc))

    async def set_affinity(self) -> dict[str, Any]:
        try:
            payload = await request.json(default={}) or {}
            uid = str(payload.get("user_id") or "").strip()
            if not uid:
                return _err("缺少 user_id")
            score = clamp(float(payload.get("score", 30)))
            users = self.plugin.affinity.data.setdefault("users", {})
            rec = users.get(uid)
            if not isinstance(rec, dict):
                rec = {
                    "name": payload.get("name") or uid,
                    "score": score,
                    "first_seen": time.time(),
                    "last_seen": time.time(),
                    "msgs": 0,
                    "gain_day": "",
                    "gain_today": 0.0,
                }
                users[uid] = rec
            rec["score"] = round(score, 2)
            self.plugin._save()
            return _ok({"ok": True, "score": rec["score"]})
        except Exception as exc:
            logger.exception("[state] 设置好感失败")
            return _err(str(exc))

    async def delete_affinity(self) -> dict[str, Any]:
        try:
            payload = await request.json(default={}) or {}
            uid = str(payload.get("user_id") or "").strip()
            users = self.plugin.affinity.data.setdefault("users", {})
            if uid in users:
                del users[uid]
                self.plugin._save()
                return _ok({"ok": True})
            return _err("没有这条记录")
        except Exception as exc:
            logger.exception("[state] 删除好感失败")
            return _err(str(exc))

    async def resolve_grudge(self) -> dict[str, Any]:
        try:
            payload = await request.json(default={}) or {}
            target = int(payload.get("id") or 0)
            for e in self.plugin.emotional.data.get("events", []):
                if int(e.get("id", -1)) == target:
                    e["resolved"] = True
                    # 放下不是只从列表消失：也把当前这口气松一点
                    labels = ((self.plugin.mood.data or {}).get("pad") or {}).get("labels") or {}
                    for k in ("委屈", "生气", "难过"):
                        if k in labels:
                            labels[k] = round(float(labels[k]) * 0.75, 3)
                    self.plugin._save()
                    return _ok({"ok": True})
            return _err("没有这条旧账")
        except Exception as exc:
            logger.exception("[state] 标记旧账失败")
            return _err(str(exc))

    async def delete_grudge(self) -> dict[str, Any]:
        try:
            payload = await request.json(default={}) or {}
            target = int(payload.get("id") or 0)
            events = self.plugin.emotional.data.get("events", [])
            keep = [e for e in events if int(e.get("id", -1)) != target]
            if len(keep) == len(events):
                return _err("没有这条旧账")
            self.plugin.emotional.data["events"] = keep
            self.plugin._save()
            return _ok({"ok": True})
        except Exception as exc:
            logger.exception("[state] 删除旧账失败")
            return _err(str(exc))

    async def add_core(self) -> dict[str, Any]:
        try:
            payload = await request.json(default={}) or {}
            text = str(payload.get("text") or "").strip()
            if len(text) < 2:
                return _err("内容太短")
            entry, created = self.plugin.core.add(text, "面板", "", "panel")
            if not created:
                return _err("这条已经在识海里了")
            self.plugin._save()
            return _ok({"id": entry["id"]})
        except Exception as exc:
            logger.exception("[state] 写入识海失败")
            return _err(str(exc))

    async def delete_core(self) -> dict[str, Any]:
        try:
            payload = await request.json(default={}) or {}
            target = int(payload.get("id") or 0)
            if not self.plugin.core.delete(target):
                return _err("没有这条")
            self.plugin._save()
            return _ok({"ok": True})
        except Exception as exc:
            logger.exception("[state] 删除识海失败")
            return _err(str(exc))
