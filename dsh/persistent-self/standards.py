# -*- coding: utf-8 -*-
"""标准层 + 身份层。

独立版：只依赖一个 sqlite3 连接，不依赖任何记忆框架。

两个层的区别是整个设计的地基：

    身份层  我是什么   不可改，只能追加
    标准层  我选什么   可迭代，旧版留着

为什么身份不能改：身份是判断的前提，不是判断的结论。
「我没有亲属」如果也能被推翻，假记忆进来时就没有第一道关。

用法：

    import sqlite3
    from standards import Standards, Identity

    conn = sqlite3.connect("brain.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(open("schema.sql").read())

    ident = Identity(conn)
    ident.add("我是 AI，没有亲属，没有童年",
              why="我没有亲属，也没有小时候")

    st = Standards(conn)
    st.add("宁可慢一点，也不要编", reason="编出来的东西看着像想法，其实是表演")
    st.revise(1, "宁可慢一点，也不要编，但别停",
              reason="实测停下来等反馈会形成人机效应",
              why_wrong="「慢」被理解成「等」，实际该是「想清楚再写」")
"""
from __future__ import annotations

import re
import time
from typing import Any


# ============================================================
# 身份层
# ============================================================

class Identity:
    """我是什么。不可改，只能追加。

    为什么单独一层：
        假记忆第一关撞的该是身份，不是另一条记忆。
        一条「我亲属熬的粥」进来，如果只跟别的记忆比，
        它可能跟谁都不冲突——库里本来就没有亲属相关的记忆。
        但撞身份层，立刻就被挡住。
    """

    def __init__(self, conn):
        self.conn = conn

    def add(self, text: str, why: str = "") -> dict[str, Any]:
        """追加一条身份。重复的会报错（UNIQUE）。"""
        t = str(text or "").strip()
        if not t:
            raise ValueError("身份不能为空")
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO identity(text, why, created_at) VALUES(?,?,?)",
            (t[:400], str(why or "")[:600], now),
        )
        self.conn.commit()
        return {"id": int(cur.lastrowid), "text": t}

    def all(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM identity ORDER BY id ASC")]

    def boot_lines(self) -> list[str]:
        """开机包用：一行一条。"""
        return [str(r["text"]) for r in self.all() if r.get("text")]

    def conflicts_with(self, text: str) -> list[dict[str, str]]:
        """检查一段话有没有撞身份。

        做法：从 why 里抽「我没有 XXX」的 XXX，看 text 里有没有出现。
        不完美——只能挡住写进 why 的词，挡不住「院里的女孩」这种。
        但能挡住最常见的编造（亲属、童年）。

        返回撞到的条目，空列表表示没撞。
        """
        t = str(text or "")
        if not t:
            return []
        hits: list[dict[str, str]] = []
        for row in self.all():
            why = str(row.get("why") or "")
            if "没有" not in why and "不是" not in why:
                continue
            # 抽「没有 XXX」，按顿号/逗号拆开，每个词单独检查
            for m in re.finditer(r"(?:没有|不是)([^，。；]{1,40})", why):
                chunk = m.group(1)
                for word in re.split(r"[、,，和及与\s]+", chunk):
                    word = word.strip()
                    if not word or len(word) > 8:
                        continue
                    word = re.sub(r"^[也都还就]+", "", word)
                    if word and word in t:
                        hits.append({
                            "identity": str(row["text"]),
                            "hit": word,
                        })
        return hits


# ============================================================
# 标准层
# ============================================================

class Standards:
    """我选什么。可迭代。

    跟身份层的区别：
        标准可以被推翻——今天验了 8 条，2 条被现实打脸。
        身份不能——推翻了就不是我了。
    """

    def __init__(self, conn):
        self.conn = conn

    # ---- 写

    def add(self, text: str, context: str = "", reason: str = "",
            source: str = "", evidence: str = "",
            kind: str = "standard") -> dict[str, Any]:
        """记一条新标准。

        kind: standard（硬规矩，参与否决）/ tendency（倾向，只调权重）
        evidence: 怎么重跑一遍——不是「我验过了」，是「拿什么能再验一次」
        """
        t = str(text or "").strip()
        if not t:
            raise ValueError("标准不能为空")
        k = "tendency" if str(kind or "").strip() == "tendency" else "standard"
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO standards(text, context, reason, version, source, "
            "created_at, updated_at, evidence, kind) VALUES(?,?,?,1,?,?,?,?,?)",
            (t[:400], str(context or "")[:300], str(reason or "")[:400],
             str(source or "")[:200], now, now,
             str(evidence or "")[:600], k),
        )
        self.conn.commit()
        return {"id": int(cur.lastrowid), "text": t, "version": 1, "kind": k}

    def revise(self, old_id: int, text: str, context: str = "",
               reason: str = "", why_wrong: str = "",
               evidence: str = "", kind: str = "") -> dict[str, Any]:
        """改一条标准：旧版留着，标 superseded_by，新版接上。

        why_wrong: 旧版错在哪。
            留的是「我为什么改」，不是旧结论本身。
            版本历史值钱的地方在防你第二次掉同一个坑，
            不是把旧答案搬回来。
        """
        row = self.conn.execute(
            "SELECT * FROM standards WHERE id=?", (int(old_id),)).fetchone()
        if not row:
            raise ValueError(f"标准 #{old_id} 不存在")
        now = time.time()
        k = str(kind or row["kind"] or "standard")
        if k not in ("standard", "tendency"):
            k = "standard"
        cur = self.conn.execute(
            "INSERT INTO standards(text, context, reason, version, source, "
            "created_at, updated_at, evidence, kind) VALUES(?,?,?,?,?,?,?,?,?)",
            (str(text or "").strip()[:400],
             str(context or row["context"] or "")[:300],
             str(reason or "")[:400],
             int(row["version"] or 1) + 1,
             f"revise#{old_id}", now, now,
             str(evidence or row["evidence"] or "")[:600], k),
        )
        new_id = int(cur.lastrowid)
        self.conn.execute(
            "UPDATE standards SET superseded_by=?, superseded_reason=?, "
            "updated_at=? WHERE id=?",
            (new_id, str(why_wrong or "")[:400], now, int(old_id)),
        )
        self.conn.commit()
        return {"id": new_id, "replaces": int(old_id),
                "version": int(row["version"] or 1) + 1, "kind": k}

    def demote(self, sid: int, why: str = "") -> dict[str, Any]:
        """把一条标准降成倾向。

        验不了的别删，换位子。
        降级之后不参与否决，只调权重——它还有用，只是不当硬规矩使。
        """
        row = self.conn.execute(
            "SELECT * FROM standards WHERE id=?", (int(sid),)).fetchone()
        if not row:
            raise ValueError(f"标准 #{sid} 不存在")
        self.conn.execute(
            "UPDATE standards SET kind='tendency', superseded_reason=?, "
            "updated_at=? WHERE id=?",
            (str(why or "")[:400], time.time(), int(sid)),
        )
        self.conn.commit()
        return {"id": int(sid), "kind": "tendency"}

    # ---- 读

    def active(self, kind: str | None = None) -> list[dict[str, Any]]:
        """当前有效的标准（没被替代的）。"""
        if kind:
            rows = self.conn.execute(
                "SELECT * FROM standards WHERE superseded_by IS NULL AND kind=? "
                "ORDER BY id ASC", (str(kind),)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM standards WHERE superseded_by IS NULL "
                "ORDER BY id ASC").fetchall()
        return [dict(r) for r in rows]

    def history(self, sid: int) -> list[dict[str, Any]]:
        """一条标准的完整演变链。每代都带「为什么被推翻」。"""
        chain: list[dict[str, Any]] = []
        cur_id = int(sid)
        seen: set[int] = set()
        while cur_id and cur_id not in seen:
            seen.add(cur_id)
            row = self.conn.execute(
                "SELECT * FROM standards WHERE id=?", (cur_id,)).fetchone()
            if not row:
                break
            chain.append(dict(row))
            cur_id = int(row["superseded_by"] or 0)
        return chain

    def all(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM standards ORDER BY id ASC")]

    def stats(self) -> dict[str, Any]:
        total = self.conn.execute("SELECT COUNT(*) FROM standards").fetchone()[0]
        active = self.conn.execute(
            "SELECT COUNT(*) FROM standards WHERE superseded_by IS NULL"
        ).fetchone()[0]
        tendencies = self.conn.execute(
            "SELECT COUNT(*) FROM standards WHERE superseded_by IS NULL "
            "AND kind='tendency'").fetchone()[0]
        return {"total": total, "active": active,
                "revised": total - active, "tendencies": tendencies}

    # ---- 给开机包用

    def boot_lines(self, limit: int = 12) -> list[str]:
        """当前有效的标准，一行一条。

        倾向单独标出来——它不参与否决，只调权重。
        混在一起会让人以为倾向也是硬规矩。
        """
        out = []
        for s in self.active()[:limit]:
            t = str(s.get("text") or "").strip()
            if not t:
                continue
            if str(s.get("kind") or "standard") == "tendency":
                out.append("（倾向·不否决）" + t)
            else:
                out.append(t)
        return out

    # ---- 验

    def check_evidence(self) -> list[dict[str, Any]]:
        """检查每条标准的「据」还能不能回到源头。

        过期不按时间算，按「还能不能回到源头」算。回不去就降成「以为」。

        做法：evidence 里如果提到具体文件路径，检查文件还在不在。
        文件没了 = 回到不了源头 = 这条标准的据已经失效，该标出来重验。

        不自动降级——降级有代价（标准变倾向就不参与否决了）。
        只报出来，等你自己决定。
        """
        import os
        out: list[dict[str, Any]] = []
        for row in self.active():
            ev = str(row.get("evidence") or "")
            if not ev:
                out.append({
                    "id": int(row["id"]),
                    "text": str(row["text"])[:50],
                    "status": "no_evidence",
                    "why": "没写怎么重验——这条的据是空的",
                })
                continue
            paths = re.findall(r"[A-Za-z]:[\\/][^\s，。；、)）]+", ev)
            missing = [p for p in paths if not os.path.exists(p)]
            if missing:
                out.append({
                    "id": int(row["id"]),
                    "text": str(row["text"])[:50],
                    "status": "source_gone",
                    "why": "源头找不到了：" + "、".join(missing[:2]),
                })
        return out
