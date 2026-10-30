# -*- coding: utf-8 -*-
"""把已有记忆导入 memory-core。

支持：
    python -m memory_core.import_data --to data/brain.db --from-brain old/brain.db
    python -m memory_core.import_data --to data/brain.db --from-jsonl chat.jsonl

只读源库，不修改原数据。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

import numpy as np

from .store import Store


def _vector(src: sqlite3.Connection, mid: int):
    try:
        row = src.execute("SELECT vec FROM vectors WHERE memory_id=?", (int(mid),)).fetchone()
        if row and row[0]:
            return np.frombuffer(row[0], dtype=np.float32)
    except Exception:
        pass
    return None


def import_brain(src_path: str, dst_path: str, limit: int = 0) -> dict:
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    dst = Store(Path(dst_path))
    existing = {str(m.get("text") or "") for m in dst.list_memories(limit=100000)}
    rows = src.execute(
        "SELECT * FROM memories WHERE status='active' ORDER BY created_at ASC"
    ).fetchall()
    if limit:
        rows = rows[:limit]
    id_map = {}
    added = skipped = 0
    src_entities = {}
    try:
        for r in src.execute("SELECT id,name,type FROM entities").fetchall():
            src_entities[int(r["id"])] = (str(r["name"]), str(r["type"] or "thing"))
    except Exception:
        pass
    for r in rows:
        text = str(r["text"] or "").strip()
        if not text or text in existing:
            skipped += 1
            continue
        try:
            meta = json.loads(r["meta"] or "{}")
        except Exception:
            meta = {}
        vec = _vector(src, int(r["id"]))
        mid = dst.insert_memory(
            text=text,
            kind=str(r["kind"] or "episode"),
            importance=float(r["importance"] or 0.5),
            valence=float(r["valence"] or 0),
            arousal=float(r["arousal"] or 0),
            source=str(r["source"] or "import"),
            meta=meta,
            vector=vec,
            created_at=float(r["created_at"] or time.time()),
            visibility=str(r["visibility"] or "public"),
        )
        id_map[int(r["id"])] = mid
        existing.add(text)
        added += 1
    linked = 0
    try:
        for r in src.execute("SELECT memory_id,entity_id FROM memory_entities").fetchall():
            mid = id_map.get(int(r["memory_id"]))
            ent = src_entities.get(int(r["entity_id"]))
            if not mid or not ent:
                continue
            eid = dst.upsert_entity(ent[0], ent[1])
            dst.link_entity(mid, eid)
            linked += 1
    except Exception:
        pass
    rel = 0
    try:
        for r in src.execute("SELECT src_id,dst_id,relation,memory_id FROM relations").fetchall():
            a = src_entities.get(int(r["src_id"]))
            b = src_entities.get(int(r["dst_id"]))
            if not a or not b:
                continue
            src_id = dst.upsert_entity(a[0], a[1])
            dst_id = dst.upsert_entity(b[0], b[1])
            mid = id_map.get(int(r["memory_id"])) if r["memory_id"] is not None else None
            dst.link_relation(src_id, dst_id, str(r["relation"] or "related"), memory_id=mid)
            rel += 1
    except Exception:
        pass
    src.close()
    dst.close()
    return {"added": added, "skipped": skipped, "entities_linked": linked, "relations": rel}


def import_jsonl(path: str, dst_path: str) -> dict:
    dst = Store(Path(dst_path))
    existing = {str(m.get("text") or "") for m in dst.list_memories(limit=100000)}
    added = skipped = 0
    for line in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        text = str(row.get("text") or row.get("content") or row.get("message") or "").strip()
        if not text or text in existing:
            skipped += 1
            continue
        dst.insert_memory(
            text=text,
            kind=str(row.get("kind") or "episode"),
            importance=float(row.get("importance") or 0.5),
            valence=float(row.get("valence") or 0),
            arousal=float(row.get("arousal") or 0),
            source=str(row.get("source") or "import"),
            meta=row.get("meta") if isinstance(row.get("meta"), dict) else {},
            created_at=float(row.get("created_at") or row.get("ts") or time.time()),
            visibility=str(row.get("visibility") or "public"),
        )
        existing.add(text)
        added += 1
    dst.close()
    return {"added": added, "skipped": skipped}


def main():
    ap = argparse.ArgumentParser(description="把已有记忆导入 memory-core（只读源库）")
    ap.add_argument("--to", required=True, help="目标 memory-core 数据库")
    ap.add_argument("--from-brain", help="AstrBot brain.db 路径")
    ap.add_argument("--from-jsonl", help="JSONL 聊天/记忆文件")
    ap.add_argument("--limit", type=int, default=0, help="最多导入多少条")
    args = ap.parse_args()
    Path(args.to).parent.mkdir(parents=True, exist_ok=True)
    if args.from_brain:
        print(json.dumps(import_brain(args.from_brain, args.to, args.limit), ensure_ascii=False))
    if args.from_jsonl:
        print(json.dumps(import_jsonl(args.from_jsonl, args.to), ensure_ascii=False))
    if not args.from_brain and not args.from_jsonl:
        ap.error("至少给 --from-brain 或 --from-jsonl")


if __name__ == "__main__":
    main()
