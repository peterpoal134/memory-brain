#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手动改一条记忆，并写审计日志。

用法：
  python tools_brain_edit.py --db brain.db --id 120 --show
  python tools_brain_edit.py --db brain.db --id 120 --text "新文本" --visibility private \
      --importance 0.9 --actor dshell --note "用户纠正：昵称是「个」"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from store import Store  # noqa: E402
import audit  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--id", type=int)
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--audit", action="store_true")
    ap.add_argument("--text")
    ap.add_argument("--importance", type=float)
    ap.add_argument("--visibility", choices=["public", "private", "group"])
    ap.add_argument("--actor", default="manual")
    ap.add_argument("--note", default="")
    args = ap.parse_args()

    db = Path(args.db).expanduser().resolve()
    if args.audit:
        for rec in audit.recent(db.parent, 20):
            print(rec.get("time"), rec.get("action"), rec.get("memory_id"),
                  (rec.get("note") or "")[:50])
        return 0
    if not args.id:
        print("需要 --id（或用 --audit 查看修改记录）")
        return 1
    store = Store(db)
    before = store.get_memory(args.id)
    if not before:
        print("没有这条记忆")
        return 1
    if args.show or not any([args.text, args.importance, args.visibility]):
        print(json.dumps({k: before.get(k) for k in ("id", "kind", "text", "importance", "visibility", "source", "meta")},
                         ensure_ascii=False, indent=2))
        return 0

    sets, vals = [], []
    if args.text is not None:
        sets.append("text=?"); vals.append(args.text)
    if args.importance is not None:
        sets.append("importance=?"); vals.append(float(args.importance))
    if args.visibility is not None:
        sets.append("visibility=?"); vals.append(args.visibility)
    vals.append(args.id)
    store.conn.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id=?", vals)
    store.conn.commit()
    after = store.get_memory(args.id)
    audit.log(db.parent, "update", args.id, before=before, after=after,
              actor=args.actor, note=args.note)
    print(json.dumps({k: after.get(k) for k in ("id", "text", "importance", "visibility")}, ensure_ascii=False, indent=2))
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
