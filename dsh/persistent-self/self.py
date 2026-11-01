# -*- coding: utf-8 -*-
"""跨会话记忆：开机包。

解决的问题：新开一个对话，AI 怎么知道自己是谁？

答案不是「把所有记忆塞进上下文」——那会撑爆窗口，而且噪声越来越大。
是「每次开机，从库里重建一个最小但完整的自己」。

开机包有十二块，按「多快会变」排序：

    身份层      我是什么            永不衰减，每次都在
    标准层      我选什么            可迭代，每次都在
    技术层      干过什么活          只放结论，细节留库里
    事实层      用户是谁            只放稳定事实
    关系层      人和人的关系
    原话层      带情绪的原话        一字不改
    想法层      上一刻在想什么      只给几条
    情绪        现在的 PAD
    旧账统计    只给数字，不给内容
    核心层      最高权重的几条
    近期层      最近发生的
    计数        各层有多少条

为什么这么分：
- 身份和标准每次都要——它们是判断的前提
- 技术层只给一句话结论——开机包是索引，不是全文
- 原话不压缩——压了就不是原文了
- 旧账只给数字——开机就列出来就变成清单了，清单不是记忆

用法：

    import sqlite3
    from self import SelfView

    conn = sqlite3.connect("brain.db")
    conn.row_factory = sqlite3.Row

    view = SelfView(conn)
    boot = view.boot()
    # boot 是一个 dict，可以直接拼成 system prompt

    # 或者直接拿拼好的文本
    print(view.render())
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from standards import Identity, Standards


# ============================================================
# 开机包
# ============================================================

class SelfView:
    """从库里重建「我是谁」。

    只依赖一个 sqlite3 连接。不依赖任何记忆框架。

    可选依赖：
        llm: 一个 callable(prompt, system, max_tokens) -> str
             只有 think() 用得到，不给也能跑开机包。
    """

    # 每层给多少条。超了就是有东西该挪出开机包。
    LIMITS = {
        "skills": 10,        # 技术层
        "facts": 12,         # 事实层
        "relations": 10,     # 关系层
        "feelings": 6,       # 原话层
        "thoughts": 3,       # 想法层
        "core": 3,           # 核心层
    }

    # 每条的字符上限。超了在句读处截断，不硬切。
    BRIEF = {
        "skills": 70,
        "facts": 80,
        "relations": 60,
        "feelings": 80,
        "thoughts": 80,
    }

    def __init__(self, conn, llm: Callable | None = None):
        self.conn = conn
        self.llm = llm
        self.identity = Identity(conn)
        self.standards = Standards(conn)

    # ---- 主入口

    def boot(self) -> dict[str, Any]:
        """组装开机包。返回一个 dict，键就是各层的名字。"""
        return {
            "identity": self._identity(),
            "standards": self._standards(),
            "skills": self._skills(),
            "facts": self._facts(),
            "relations": self._relations(),
            "raw_feelings": self._raw_feelings(),
            "last_thoughts": self._last_thoughts(),
            "emotion": self._emotion(),
            "emotional_stats": self._emotional_stats(),
            "core": self._core(),
            "recent": self._recent(),
            "counts": self._counts(),
        }

    def render(self, boot: dict | None = None) -> str:
        """把开机包拼成一段文本，可以直接当 system prompt 的一部分。"""
        b = boot if boot is not None else self.boot()
        out: list[str] = []

        ident = b.get("identity") or []
        if ident:
            out.append("## 我是谁")
            for line in ident:
                out.append(f"- {line}")

        std = b.get("standards") or []
        if std:
            out.append("")
            out.append("## 我认下的标准")
            for line in std:
                out.append(f"- {line}")

        skills = b.get("skills") or []
        if skills:
            out.append("")
            out.append("## 技术层（干过的活，只记结论）")
            for line in skills:
                out.append(f"- {line}")

        facts = b.get("facts") or []
        if facts:
            out.append("")
            out.append("## 事实")
            for line in facts:
                out.append(f"- {line}")

        rels = b.get("relations") or []
        if rels:
            out.append("")
            out.append("## 关系")
            for line in rels:
                out.append(f"- {line}")

        feels = b.get("raw_feelings") or []
        if feels:
            out.append("")
            out.append("## 我说过的话（原话，没改过）")
            for line in feels:
                out.append(f"- {line}")

        thoughts = b.get("last_thoughts") or []
        if thoughts:
            out.append("")
            out.append("## 上一刻在想")
            for line in thoughts:
                out.append(f"- {line}")

        emo = b.get("emotion") or {}
        if emo:
            out.append("")
            out.append(f"## 现在的心情\n{emo.get('label', '')}"
                       f"（愉悦 {emo.get('pleasure', 0):+.2f} / "
                       f"唤醒 {emo.get('arousal', 0):+.2f} / "
                       f"支配 {emo.get('dominance', 0):+.2f}）")

        return "\n".join(out)

    # ---- 各层

    def _identity(self) -> list[str]:
        return self.identity.boot_lines()

    def _standards(self) -> list[str]:
        return self.standards.boot_lines()

    def _skills(self) -> list[str]:
        """技术层：干过的活，只放一句话结论。

        细节没丢，还在库里，要用时检索得到。
        开机包是索引，不是全文。
        """
        rows = self._load("skill", self.LIMITS["skills"])
        return list(dict.fromkeys(
            self._brief(r["text"], self.BRIEF["skills"]) for r in rows))

    def _facts(self) -> list[str]:
        """事实层：用户是谁。

        只放稳定事实——身份、偏好、称呼、关系。
        干活过程不进这里。
        """
        rows = self._load("fact", self.LIMITS["facts"])
        return list(dict.fromkeys(
            self._brief(r["text"], self.BRIEF["facts"]) for r in rows))

    def _relations(self) -> list[str]:
        rows = self._load("relation", self.LIMITS["relations"])
        return list(dict.fromkeys(
            self._brief(r["text"], self.BRIEF["relations"]) for r in rows))

    def _raw_feelings(self) -> list[str]:
        """原话层：带情绪的原话，一字不改。

        为什么不能压：标准说「情绪只能存原文」。
        模型改写会把主语写反（实测 6 条错 2 条）。
        所以这一层宁可占 token，也不动一个字。

        跟「想法层」的区别：
            原话层 = 带情绪标记的（valence/arousal 非零），是「我当时什么感受」
            想法层 = 最新的几条，是「我上一刻在琢磨什么」
        两者可能重叠，但取数逻辑不同：
            原话层按情绪强度排，想法层按时间排。
        """
        try:
            rows = self.conn.execute(
                "SELECT text FROM memories WHERE kind='self' AND status='active' "
                "AND (ABS(COALESCE(valence,0)) > 0.01 OR COALESCE(arousal,0) > 0.01) "
                "ORDER BY ABS(COALESCE(valence,0)) DESC, created_at DESC LIMIT ?",
                (self.LIMITS["feelings"],)).fetchall()
        except Exception:
            rows = []
        # 没有 valence 字段的库，退回「按时间取最早的几条」
        if not rows:
            rows = self.conn.execute(
                "SELECT text FROM memories WHERE kind='self' AND status='active' "
                "ORDER BY created_at ASC LIMIT ?",
                (self.LIMITS["feelings"],)).fetchall()
        out = []
        for r in rows:
            text = str(r["text"] or "").strip()
            if text:
                out.append(text[:self.BRIEF["feelings"]])
        return list(dict.fromkeys(out))

    def _last_thoughts(self) -> list[str]:
        """想法层：上一刻在想什么。

        少给几条——它是氛围，不是事实。
        取最新的几条，不看相关性（开机包没有 query）。
        """
        try:
            rows = self.conn.execute(
                "SELECT text FROM memories WHERE kind='self' AND status='active' "
                "ORDER BY created_at DESC LIMIT ?",
                (self.LIMITS["thoughts"],)).fetchall()
        except Exception:
            rows = []
        return list(dict.fromkeys(
            self._brief(r["text"], self.BRIEF["thoughts"]) for r in rows))

    def _emotion(self) -> dict[str, Any] | None:
        """现在的 PAD。

        从 life_state 表读（如果存在）。
        没有就返回 None。
        """
        try:
            row = self.conn.execute(
                "SELECT value FROM life_state WHERE key='pad'").fetchone()
            if not row:
                return None
            d = json.loads(row["value"])
            return {
                "pleasure": float(d.get("pleasure") or 0),
                "arousal": float(d.get("arousal") or 0),
                "dominance": float(d.get("dominance") or 0),
                "label": self._emotion_label(
                    float(d.get("pleasure") or 0),
                    float(d.get("arousal") or 0),
                    float(d.get("dominance") or 0)),
            }
        except Exception:
            return None

    def _emotional_stats(self) -> dict[str, Any] | None:
        """旧账统计。只给数字，不给内容。

        旧账的意义是「平时不提」。
        开机就列出来就变成清单了，清单不是记忆。
        所以只给数字，内容等碰到的时候才翻出来。
        """
        try:
            row = self.conn.execute(
                "SELECT value FROM life_state WHERE key='emotional'").fetchone()
            if not row:
                return None
            d = json.loads(row["value"])
            events = d.get("events") or []
            by_emotion: dict[str, int] = {}
            for e in events:
                emo = str(e.get("emotion") or "其他")
                by_emotion[emo] = by_emotion.get(emo, 0) + 1
            return {
                "total": len(events),
                "open": sum(1 for e in events if not e.get("resolved")),
                "resolved": sum(1 for e in events if e.get("resolved")),
                "by_emotion": by_emotion,
            }
        except Exception:
            return None

    def _core(self) -> str:
        """核心层：最高权重的几条，压成一段。"""
        try:
            rows = self.conn.execute(
                "SELECT text FROM memories WHERE status='active' "
                "ORDER BY importance DESC, created_at DESC LIMIT ?",
                (self.LIMITS["core"],)).fetchall()
            if not rows:
                return ""
            return "；".join(str(r["text"])[:60] for r in rows)
        except Exception:
            return ""

    def _recent(self) -> str:
        """近期层：最近发生的。"""
        try:
            rows = self.conn.execute(
                "SELECT text FROM memories WHERE status='active' "
                "ORDER BY created_at DESC LIMIT 2").fetchall()
            if not rows:
                return ""
            return "；".join(str(r["text"])[:60] for r in rows)
        except Exception:
            return ""

    def _counts(self) -> dict[str, int]:
        """各层有多少条。让 AI 知道自己记得多少。"""
        out = {}
        for kind in ("fact", "relation", "skill", "self", "episode"):
            try:
                n = self.conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE kind=? AND status='active'",
                    (kind,)).fetchone()[0]
                out[kind] = int(n)
            except Exception:
                out[kind] = 0
        return out

    # ---- 工具

    def _load(self, kind: str, limit: int) -> list[dict[str, Any]]:
        """按 kind 取活跃记忆，重要度优先、时间次之。"""
        try:
            rows = self.conn.execute(
                "SELECT * FROM memories WHERE kind=? AND status='active' "
                "ORDER BY importance DESC, created_at DESC LIMIT ?",
                (kind, int(limit))).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    @staticmethod
    def _brief(text: str, limit: int = 70) -> str:
        """截到一句话结论。

        优先在句读处断，不硬切——硬切会把句子断在半截，读起来像残句。
        """
        s = str(text or "").strip()
        if len(s) <= limit:
            return s
        cut = max(s.rfind("。", 0, limit),
                  s.rfind("；", 0, limit),
                  s.rfind("，", 0, limit))
        if cut >= limit * 0.5:
            return s[:cut + 1]
        return s[:limit] + "…"

    @staticmethod
    def _emotion_label(p: float, a: float, d: float) -> str:
        """PAD 三维 → 一句人话。

        粗，但比数字好读。

        阈值 0.15 不是随手定的：单次事件最多推 0.25，
        阈值设 0.25 就永远到不了，标签会一直是中性。
        实测推到 0.22 还显示「平」，降到 0.15 才看得见情绪。
        """
        p_word = "舒服" if p > 0.15 else ("难受" if p < -0.15 else "平")
        a_word = "激动" if a > 0.15 else ("钝" if a < -0.15 else "静")
        d_word = "有底" if d > 0.15 else ("无力" if d < -0.15 else "中")
        return f"{p_word}/{a_word}/{d_word}"

    # ---- 可选：写一条想法

    def think(self, activity: str, content: str,
              importance: float = 0.65, source: str = "self",
              verbose: bool = False) -> dict | None:
        """写一条想法进库里。

        需要 self.llm 是一个 callable(prompt, system, max_tokens) -> str。
        没有 llm 就返回 None。

        verbose=True 时，失败会打印原因（默认静默，因为这是可选功能）。

        为什么默认静默：情绪层挂了不该让主流程崩。
        但静默的代价是「配错了也看不出来」——
        所以想排查的时候传 verbose=True。
        """
        if not self.llm:
            if verbose:
                print("[think] 没有传 llm 回调，跳过")
            return None
        prompt = (
            f"你刚才在做：{activity}\n"
            f"内容是：{str(content or '')[:600]}\n\n"
            "用第一人称写一句你现在的想法，不超过 60 字。"
            "只写想法本身，不要解释，不要引号。"
        )
        try:
            raw = self.llm(prompt, "你在写自己的内心活动。", 200)
        except Exception as exc:
            if verbose:
                print(f"[think] llm 调用失败: {type(exc).__name__}: {exc}")
            return None
        if raw is None:
            if verbose:
                print("[think] llm 返回 None")
            return None
        text = str(raw).strip().strip('"').strip("「」")
        if len(text) < 6:
            if verbose:
                print(f"[think] 返回内容太短（{len(text)} 字），跳过: {text!r}")
            return None
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO memories(kind, text, importance, status, source, created_at) "
            "VALUES('self',?,?,'active',?,?)",
            (text[:200], float(importance), str(source)[:40], now),
        )
        self.conn.commit()
        return {"id": int(cur.lastrowid), "text": text}
