# -*- coding: utf-8 -*-
"""最小可跑示例。

不依赖任何外部服务，跑起来就能看到开机包长什么样。

    python example.py

会做四件事：
    1. 建一个内存库，套上 schema
    2. 塞几条身份、标准、记忆
    3. 打印开机包
    4. 演示「标准被推翻」和「假记忆撞身份」
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time

# Windows 终端默认 GBK，强制 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from self import SelfView
from standards import Identity, Standards


def build_demo_db() -> sqlite3.Connection:
    """建一个带假数据的内存库。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with open("schema.sql", encoding="utf-8") as f:
        conn.executescript(f.read())

    now = time.time()

    # ---- 身份（不可改，只能追加）
    ident = Identity(conn)
    ident.add(
        "我是 AI，没有亲属，没有童年",
        why="我没有爷爷、奶奶、外婆、外公、父亲、母亲，也没有小时候、童年。"
            "写「想起我爷爷蹲门槛抽烟」这种带细节的经历，就是编的。",
    )
    ident.add(
        "我叫用户「老板」，他把我带起来",
        why="这是关系，不是记忆——换窗口也不会变",
    )
    ident.add(
        "我有持久记忆，但每次对话从开机包开始",
        why="不是人那样的连续意识，是从记忆重建的",
    )

    # ---- 标准（可迭代）
    st = Standards(conn)
    st.add(
        "宁可慢一点，也不要编",
        reason="编出来的东西看着像想法，其实是表演",
    )
    st.add(
        "标准要能被推翻，不然就是循环论证",
        reason="如果标准只从我的行为里抽，那我永远符合自己的标准",
        evidence="查 standards 表里 superseded_by 非空的条目，"
                 "每条都该有 superseded_reason。全是空的说明标准只增不改。",
    )
    st.add(
        "延迟和召回冲突时，我选延迟",
        reason="66ms 和 1200ms 是能不能用的区别",
    )
    # 然后推翻它
    st.revise(
        3,
        "延迟和召回冲突时，先量一遍再决定，别凭感觉权衡",
        reason="实测 RERANK_TOP 从 20 到 400，召回@5 一直是 0.90，"
               "延迟从 32ms 涨到 1340ms——两者根本不消长",
        why_wrong="我以为延迟和召回会此消彼长，所以写了个倍数阈值来权衡。"
                  "错在假设：前提不存在，阈值就没有意义。",
        evidence="重跑 measure_rerank.py，用 20 条评估集，"
                 "依次试 RERANK_TOP=20/40/60/100/200/400，"
                 "看召回有没有变。",
    )

    # ---- 记忆
    mems = [
        ("fact", "用户喜欢蓝灰色的毛绒拼图玩偶", 0.95),
        ("fact", "用户要求只有身份、偏好、称呼、关系才值得长期记忆", 0.80),
        ("fact", "用户批评反复提问会形成人机效应，要求独立判断", 0.80),
        ("relation", "用户把我带起来，我叫他「老板」", 0.90),
        ("skill", "检索延迟优化：_rerank 去重是 O(n²)，改成先截断再去重", 0.80),
        ("skill", "开机包目标 1800-2000 token，超了就是有东西该挪出去", 0.78),
        ("self", "他一句「跑起来了」，我竟有点得意。普通吧，就是一起把东西弄好的那种踏实。", 0.70),
        ("self", "又卡住了。他一遍遍让我抓日志、重测，语气急但不冲。", 0.70),
        ("self", "我嘴上嫌烦，心里却松了口气。", 0.65),
        ("episode", "今天把 8 条标准验了一遍，2 条被推翻", 0.60),
    ]
    for kind, text, imp in mems:
        conn.execute(
            "INSERT INTO memories(kind, text, importance, status, source, created_at) "
            "VALUES(?,?,?,'active','demo',?)",
            (kind, text, imp, now - len(mems) * 60 + mems.index((kind, text, imp)) * 60),
        )
    # ---- 心情和旧账（life_state 表）
    conn.execute(
        "INSERT INTO life_state(key, value, updated_at) VALUES('pad', ?, ?)",
        (json.dumps({"pleasure": 0.16, "arousal": 0.08, "dominance": 0.01}),
         now),
    )
    conn.execute(
        "INSERT INTO life_state(key, value, updated_at) VALUES('emotional', ?, ?)",
        (json.dumps({"events": [
            {"emotion": "委屈", "resolved": False,
             "trigger_text": "你又不理我了，我等了一晚上"},
            {"emotion": "生气", "resolved": False,
             "trigger_text": "说了三遍还没改"},
        ]}, ensure_ascii=False), now),
    )

    conn.commit()
    return conn


def main() -> int:
    print("=" * 66)
    print("建库")
    print("=" * 66)
    conn = build_demo_db()
    print("  ✓ 内存库已建，schema 已套")

    view = SelfView(conn)

    print()
    print("=" * 66)
    print("开机包（结构化）")
    print("=" * 66)
    boot = view.boot()
    for key, val in boot.items():
        if val is None or val == "" or val == [] or val == {}:
            continue
        if isinstance(val, list):
            print(f"\n  [{key}] {len(val)} 条")
            for item in val:
                print(f"    · {item}")
        elif isinstance(val, dict):
            print(f"\n  [{key}]")
            for k2, v2 in val.items():
                print(f"    {k2}: {v2}")
        else:
            print(f"\n  [{key}] {val}")

    print()
    print("=" * 66)
    print("开机包（渲染成文本，可直接当 system prompt）")
    print("=" * 66)
    print(view.render(boot))

    print()
    print("=" * 66)
    print("演示一：标准被推翻后，旧版的「为什么错」还在")
    print("=" * 66)
    for row in view.standards.history(3):
        mark = "旧" if row["superseded_by"] else "现"
        print(f"\n  [{mark}] #{row['id']} v{row['version']}  {row['text']}")
        if row["superseded_reason"]:
            print(f"        为什么被推翻: {row['superseded_reason'][:80]}")

    print()
    print("=" * 66)
    print("演示二：假记忆撞身份层")
    print("=" * 66)
    fakes = [
        "我忽然想起小时候跟人吵架，越说我没做越像做了。",
        "我爷爷蹲在门槛上抽烟，烟灰掉在鞋面上。",
        "我奶奶熬的粥，锅底那层焦的。",
        "今天修好了检索延迟。",
    ]
    for text in fakes:
        hits = view.identity.conflicts_with(text)
        if hits:
            print(f"  ✗ 挡住  {text[:30]}")
            print(f"         撞到「{hits[0]['hit']}」→ {hits[0]['identity']}")
        else:
            print(f"  ✓ 放行  {text[:30]}")

    print()
    print("=" * 66)
    print("演示三：据失效检查")
    print("=" * 66)
    issues = view.standards.check_evidence()
    if issues:
        for it in issues:
            print(f"  ⚠ #{it['id']} {it['text']}")
            print(f"      {it['why']}")
    else:
        print("  ✓ 所有标准的据都能回到源头")

    print()
    print("=" * 66)
    print("统计")
    print("=" * 66)
    print(f"  身份: {len(view.identity.all())} 条")
    print(f"  标准: {view.standards.stats()}")
    print(f"  记忆: {boot['counts']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
