#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记忆检索评估：跑一批人工标注的 query，输出 recall@k / MRR / 延迟。

用法：python tools/eval_memory.py [--brain path] [--queries eval/queries.jsonl] [--k 5]
标注格式（JSONL）：{"query": "...", "expect": [记忆id, ...]}
embedding 用 AstrBot 配置里的 Ollama；拿不到就退化为纯关键词。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))

from astrbot_plugin_memory_brain.engine import MemoryEngine  # noqa: E402
from astrbot_plugin_memory_brain.store import Store  # noqa: E402

ASTRBOT_DATA = Path("AstrBot/data")
DEFAULT_BRAIN = ASTRBOT_DATA / "plugin_data/astrbot_plugin_memory_brain/brain.db"
DEFAULT_QUERIES = ROOT / "eval" / "queries.jsonl"


def embed_fn():
    try:
        cfg = json.loads((ASTRBOT_DATA / "cmd_config.json").read_text(encoding="utf-8-sig"))
        want = ""
        try:
            plug = json.loads((ASTRBOT_DATA / "config/astrbot_plugin_memory_brain_config.json").read_text(encoding="utf-8-sig"))
            want = str(plug.get("embedding_provider_id") or "")
        except Exception:
            pass
        for p in cfg.get("provider", []):
            kind = str(p.get("provider_type") or p.get("type") or "")
            if want and p.get("id") != want:
                continue
            if "embedding" in kind and p.get("enable", True):
                base = str(p.get("embedding_api_base") or "").rstrip("/")
                model = str(p.get("embedding_model") or "")
                if not base or not model:
                    continue

                def _embed(text: str) -> np.ndarray | None:
                    body = json.dumps({"model": model, "input": text}).encode("utf-8")
                    req = urllib.request.Request(
                        f"{base}/api/embed",
                        data=body,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                    vec = data.get("embeddings") or [data.get("embedding")]
                    return np.asarray(vec[0], dtype=np.float32)

                return _embed
    except Exception:
        pass
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brain", default=str(DEFAULT_BRAIN))
    ap.add_argument("--queries", default=str(DEFAULT_QUERIES))
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--no-rerank", action="store_true")
    args = ap.parse_args()

    store = Store(Path(args.brain))
    engine = MemoryEngine(store)
    embed = embed_fn()
    rows = []
    with open(args.queries, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    hits_at = {1: 0, 3: 0, 5: 0}
    mrr = 0.0
    lat = []
    misses = []

    for item in rows:
        q = item["query"]
        expect = set(item.get("expect") or [])
        vec = embed(q) if embed else None
        t0 = time.perf_counter()
        hits = engine.recall(q, k=args.k, query_vec=vec, touch=False, rerank=not args.no_rerank)
        lat.append((time.perf_counter() - t0) * 1000)

        ids = [h["id"] for h in hits]
        rank = next((i + 1 for i, mid in enumerate(ids) if mid in expect), None)
        if rank:
            mrr += 1.0 / rank
            for k in hits_at:
                if rank <= k:
                    hits_at[k] += 1
        else:
            misses.append((q, ids[:3], sorted(expect)))

    n = len(rows)
    print(f"query {n} 条 | embedding {'开' if embed else '关（仅关键词）'} | k={args.k} | 重排 {'关' if args.no_rerank else '开'}")
    for k in (1, 3, 5):
        print(f"  recall@{k}: {hits_at[k]}/{n} = {hits_at[k] / n:.0%}")
    print(f"  MRR: {mrr / n:.3f}")
    print(f"  延迟 p50 {statistics.median(lat):.1f}ms / p95 {sorted(lat)[int(n * 0.95) - 1]:.1f}ms")
    if misses:
        print("  未命中：")
        for q, got, want in misses:
            print(f"    - {q} | 期望 {want} | 实得 {got}")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
