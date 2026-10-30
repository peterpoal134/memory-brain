# -*- coding: utf-8 -*-
"""手动改记忆的审计日志：谁、何时、改前、改后。"""
from __future__ import annotations

import json
import time
from pathlib import Path


def log(
    data_dir: Path,
    action: str,
    memory_id: int | None = None,
    *,
    before: dict | None = None,
    after: dict | None = None,
    actor: str = "manual",
    note: str = "",
) -> None:
    path = Path(data_dir) / "audit.jsonl"
    rec = {
        "ts": time.time(),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "action": action,
        "memory_id": memory_id,
        "actor": actor,
        "note": note,
        "before": before,
        "after": after,
    }
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def recent(data_dir: Path, limit: int = 20) -> list[dict]:
    path = Path(data_dir) / "audit.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
