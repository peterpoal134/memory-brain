# -*- coding: utf-8 -*-
"""通用检索评估：不绑定角色，期望值用关键词匹配。"""
from __future__ import annotations

import json
import statistics
import time
from pathlib import Path
from typing import Any


class EvalService:
    def __init__(self, memory, path: str | None = None):
        self.memory = memory
        self.path = Path(path or Path(memory.store.path).parent / "eval_queries.jsonl")
        self.conn = memory.store.conn
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS eval_runs (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   created_at REAL NOT NULL,
                   metrics TEXT NOT NULL,
                   results TEXT NOT NULL
               )"""
        )
        self.conn.commit()

    def queries(self) -> list[dict]:
        rows = []
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    q = json.loads(line)
                except Exception:
                    continue
                if q.get("query"):
                    rows.append(q)
        except Exception:
            pass
        return rows

    def add_query(self, data: dict) -> dict:
        q = str(data.get("query") or "").strip()
        if not q:
            raise ValueError("query 不能为空")
        expect = data.get("expect_any") or []
        if isinstance(expect, str):
            expect = [expect]
        row = {"query": q, "expect_any": [str(x) for x in expect if str(x).strip()],
               "type": str(data.get("type") or "fact")}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    def run(self) -> dict:
        qs = self.queries()
        results = []
        lat = []
        for q in qs:
            t0 = time.perf_counter()
            out = self.memory.recall({"query": q["query"], "k": 5})
            lat.append((time.perf_counter() - t0) * 1000)
            hits = out.get("hits") or []
            expect = [str(x) for x in (q.get("expect_any") or []) if str(x).strip()]
            rank = 0
            if expect:
                for i, h in enumerate(hits, 1):
                    text = str(h.get("text") or "")
                    if any(e in text for e in expect):
                        rank = i
                        break
            else:
                # 负例：没有命中或最高分很低才算对
                top = float(hits[0].get("score") or 0) if hits else 0
                rank = 1 if top < 0.55 else 0
            results.append({"query": q["query"], "expect_any": expect, "type": q.get("type") or "fact",
                            "rank": rank, "hits": hits[:3]})
        total = len(results)
        m = {
            "total": total,
            "recall@1": round(sum(1 for r in results if r["rank"] == 1) / total, 3) if total else 0,
            "recall@3": round(sum(1 for r in results if 0 < r["rank"] <= 3) / total, 3) if total else 0,
            "recall@5": round(sum(1 for r in results if 0 < r["rank"] <= 5) / total, 3) if total else 0,
            "mrr": round(sum(1.0 / r["rank"] for r in results if r["rank"]) / total, 3) if total else 0,
            "p50_ms": round(statistics.median(lat), 1) if lat else 0,
            "negatives": sum(1 for r in results if not r["expect_any"]),
        }
        cur = self.conn.execute(
            "INSERT INTO eval_runs(created_at,metrics,results) VALUES(?,?,?)",
            (time.time(), json.dumps(m, ensure_ascii=False), json.dumps(results, ensure_ascii=False)),
        )
        self.conn.commit()
        return {"id": int(cur.lastrowid), "created_at": time.time(), "metrics": m, "results": results}

    def latest(self) -> dict:
        row = self.conn.execute("SELECT * FROM eval_runs ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return {"empty": True, "total_queries": len(self.queries()),
                    "metrics": {"total": 0, "recall@1": 0, "recall@3": 0, "recall@5": 0, "mrr": 0, "p50_ms": 0}}
        return {"empty": False, "id": int(row["id"]), "created_at": float(row["created_at"]),
                "metrics": json.loads(row["metrics"]), "results": json.loads(row["results"]),
                "total_queries": len(self.queries())}
