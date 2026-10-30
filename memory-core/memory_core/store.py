# -*- coding: utf-8 -*-
"""记忆大脑的存储层：SQLite 单文件，不依赖任何外部服务。

表结构见 DESIGN.md 第 2 节。这里只做「存/取/改」，不做打分和策略，
方便单独测试，也方便以后换存储（Postgres/向量库）而不动引擎。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

import numpy as np

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'episode',
    text TEXT NOT NULL,
    importance REAL NOT NULL DEFAULT 0.5,
    valence REAL NOT NULL DEFAULT 0.0,
    arousal REAL NOT NULL DEFAULT 0.0,
    created_at REAL NOT NULL,
    last_access REAL NOT NULL DEFAULT 0,
    access_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    source TEXT NOT NULL DEFAULT '',
    meta TEXT NOT NULL DEFAULT '{}',
    visibility TEXT NOT NULL DEFAULT 'public',
    archived_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);
CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    tokens,
    memory_id UNINDEXED,
    tokenize='unicode61'
);

CREATE TABLE IF NOT EXISTS vectors (
    memory_id INTEGER PRIMARY KEY,
    dim INTEGER NOT NULL,
    vec BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'thing',
    mention_count INTEGER NOT NULL DEFAULT 1,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    aliases TEXT NOT NULL DEFAULT '[]',
    profile TEXT NOT NULL DEFAULT '',
    profile_at REAL NOT NULL DEFAULT 0,
    UNIQUE(name, type)
);

CREATE TABLE IF NOT EXISTS memory_entities (
    memory_id INTEGER NOT NULL,
    entity_id INTEGER NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (memory_id, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_me_entity ON memory_entities(entity_id);

CREATE TABLE IF NOT EXISTS daily_stats (
    day TEXT NOT NULL,
    key TEXT NOT NULL,
    value REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, key)
);

CREATE TABLE IF NOT EXISTS conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    a_id INTEGER NOT NULL,
    b_id INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS working (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    text TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'todo',
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    done INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_working_session ON working(session_id, done);

CREATE TABLE IF NOT EXISTS raw_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    sender TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_role ON raw_messages(role, id);

CREATE TABLE IF NOT EXISTS relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    src_id INTEGER NOT NULL,
    dst_id INTEGER NOT NULL,
    relation TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    memory_id INTEGER,
    created_at REAL NOT NULL,
    UNIQUE(src_id, dst_id, relation)
);
"""


def _bigram_sim(a: str, b: str) -> float:
    ga, gb = set(bigrams(a)), set(bigrams(b))
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / min(len(ga), len(gb))


def bigrams(text: str) -> list[str]:
    """中文友好的检索词：字符二元组，不依赖分词。"""
    t = re.sub(r"[^\w\u4e00-\u9fff]+", "", str(text or ""))
    if len(t) <= 1:
        return [t] if t else []
    return [t[i: i + 2] for i in range(len(t) - 1)]


def _token_blob(text: str) -> str:
    return " ".join(bigrams(text))


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(_SCHEMA)
        self._migrate_columns()
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    def _migrate_columns(self) -> None:
        cols = {str(r["name"]) for r in self.conn.execute("PRAGMA table_info(memories)")}
        if "archived_at" not in cols:
            self.conn.execute("ALTER TABLE memories ADD COLUMN archived_at REAL NOT NULL DEFAULT 0")
        if "visibility" not in cols:
            self.conn.execute(
                "ALTER TABLE memories ADD COLUMN visibility TEXT NOT NULL DEFAULT 'public'"
            )
            # 旧数据保守处理：事件按私聊算，事实/反思/技能保持公开
            self.conn.execute("UPDATE memories SET visibility='private' WHERE kind='episode'")
        self.conn.commit()
        self._migrate_entity_profile()

    def _migrate_entity_profile(self) -> None:
        cols = {str(r["name"]) for r in self.conn.execute("PRAGMA table_info(entities)")}
        if "profile" not in cols:
            self.conn.execute("ALTER TABLE entities ADD COLUMN profile TEXT NOT NULL DEFAULT ''")
        if "profile_at" not in cols:
            self.conn.execute("ALTER TABLE entities ADD COLUMN profile_at REAL NOT NULL DEFAULT 0")
        self.conn.commit()

    # ---------------------------------------------------------------- 记忆

    def insert_memory(
        self,
        *,
        text: str,
        kind: str = "episode",
        importance: float = 0.5,
        valence: float = 0.0,
        arousal: float = 0.0,
        source: str = "",
        meta: dict | None = None,
        vector: np.ndarray | None = None,
        created_at: float | None = None,
        visibility: str = "public",
    ) -> int:
        now = created_at or time.time()
        cur = self.conn.execute(
            """INSERT INTO memories
               (kind, text, importance, valence, arousal, created_at, last_access, source, meta, visibility)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                kind,
                text,
                float(importance),
                float(valence),
                float(arousal),
                now,
                0.0,
                source,
                json.dumps(meta or {}, ensure_ascii=False),
                visibility if visibility in ("public", "private", "group") else "public",
            ),
        )
        memory_id = int(cur.lastrowid)
        self.conn.execute(
            "INSERT INTO memories_fts(tokens, memory_id) VALUES (?, ?)",
            (_token_blob(text), memory_id),
        )
        if vector is not None:
            self.set_vector(memory_id, vector)
        self.conn.commit()
        return memory_id

    def set_vector(self, memory_id: int, vec: np.ndarray) -> None:
        v = np.asarray(vec, dtype=np.float32).reshape(-1)
        self.conn.execute(
            "INSERT OR REPLACE INTO vectors(memory_id, dim, vec) VALUES (?,?,?)",
            (int(memory_id), int(v.shape[0]), v.tobytes()),
        )
        self.conn.commit()

    def count_memories(
        self,
        *,
        source: str | None = None,
        since: float | None = None,
        status: str = "active",
    ) -> int:
        sql = "SELECT COUNT(*) AS n FROM memories WHERE status=?"
        args: list[Any] = [status]
        if source:
            sql += " AND source=?"
            args.append(source)
        if since:
            sql += " AND created_at>=?"
            args.append(float(since))
        row = self.conn.execute(sql, args).fetchone()
        return int(row["n"] if row else 0)

    def get_memory(self, memory_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM memories WHERE id=?", (int(memory_id),)).fetchone()
        return self._row_to_memory(row) if row else None

    def list_memories(
        self,
        *,
        status: str = "active",
        kind: str | None = None,
        source: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        sql = "SELECT * FROM memories WHERE status=?"
        args: list[Any] = [status]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if source:
            sql += " AND source=?"
            args.append(source)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        args += [int(limit), int(offset)]
        return [self._row_to_memory(r) for r in self.conn.execute(sql, args).fetchall()]

    def raw_between(
        self, start: float, end: float, limit: int = 8, session_like: str | None = None
    ) -> list[tuple[float, str, str]]:
        sql = """SELECT created_at, sender, text FROM raw_messages
                 WHERE role='user' AND created_at >= ? AND created_at < ?"""
        args: list[Any] = [float(start), float(end)]
        if session_like:
            sql += " AND session_id LIKE ?"
            args.append(session_like)
        sql += " ORDER BY created_at ASC LIMIT ?"
        args.append(int(limit))
        rows = self.conn.execute(sql, args).fetchall()
        return [(float(r[0]), str(r[1]), str(r[2])) for r in rows]

    def memories_in_range(
        self, start: float, end: float, visibility: set[str], limit: int = 5,
        kinds: tuple[str, ...] | None = None,
    ) -> list[dict]:
        marks = ",".join("?" * len(visibility))
        sql = f"""SELECT * FROM memories
                WHERE status='active' AND created_at >= ? AND created_at < ?
                  AND visibility IN ({marks})"""
        args: list[Any] = [float(start), float(end), *visibility]
        if kinds:
            sql += " AND kind IN (" + ",".join("?" * len(kinds)) + ")"
            args.extend(kinds)
        sql += " ORDER BY importance DESC, created_at DESC LIMIT ?"
        args.append(int(limit))
        return [self._row_to_memory(r) for r in self.conn.execute(sql, args).fetchall()]

    def delete_memory(self, memory_id: int) -> bool:
        memory_id = int(memory_id)
        self.conn.execute("DELETE FROM memories_fts WHERE memory_id=?", (memory_id,))
        self.conn.execute("DELETE FROM vectors WHERE memory_id=?", (memory_id,))
        self.conn.execute("DELETE FROM memory_entities WHERE memory_id=?", (memory_id,))
        cur = self.conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def update_memory(self, memory_id: int, **fields: Any) -> None:
        allowed = {"text", "kind", "importance", "valence", "arousal", "status", "last_access", "access_count", "meta"}
        sets, args = [], []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "meta":
                value = json.dumps(value, ensure_ascii=False)
            sets.append(f"{key}=?")
            args.append(value)
        if not sets:
            return
        args.append(int(memory_id))
        self.conn.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id=?", args)
        if "text" in fields:
            self.conn.execute("DELETE FROM memories_fts WHERE memory_id=?", (int(memory_id),))
            self.conn.execute(
                "INSERT INTO memories_fts(tokens, memory_id) VALUES (?,?)",
                (_token_blob(str(fields["text"])), int(memory_id)),
            )
        self.conn.commit()

    def touch(self, memory_ids: Iterable[int], now: float | None = None) -> None:
        now = now or time.time()
        self.conn.executemany(
            "UPDATE memories SET last_access=?, access_count=access_count+1 WHERE id=?",
            [(now, int(mid)) for mid in memory_ids],
        )
        self.conn.commit()

    # ---------------------------------------------------------------- 检索

    def fts_search(self, query: str, limit: int = 50) -> list[tuple[int, float]]:
        grams = bigrams(query)
        if not grams:
            return []
        match = " OR ".join(f'"{g}"' for g in grams)
        try:
            rows = self.conn.execute(
                """SELECT memory_id, bm25(memories_fts) AS score
                   FROM memories_fts
                   WHERE memories_fts MATCH ?
                   ORDER BY score
                   LIMIT ?""",
                (match, int(limit)),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [(int(r["memory_id"]), float(r["score"])) for r in rows]

    def all_vectors(self) -> tuple[list[int], np.ndarray | None]:
        rows = self.conn.execute(
            """SELECT v.memory_id, v.vec FROM vectors v
               JOIN memories m ON m.id=v.memory_id
               WHERE m.status='active'"""
        ).fetchall()
        if not rows:
            return [], None
        ids = [int(r["memory_id"]) for r in rows]
        dim = len(np.frombuffer(rows[0]["vec"], dtype=np.float32))
        mat = np.zeros((len(rows), dim), dtype=np.float32)
        for i, r in enumerate(rows):
            mat[i] = np.frombuffer(r["vec"], dtype=np.float32)
        return ids, mat

    # ---------------------------------------------------------------- 实体与关系

    def upsert_entity(
        self,
        name: str,
        type: str = "thing",
        *,
        now: float | None = None,
        aliases: list[str] | None = None,
    ) -> int:
        now = now or time.time()
        row = self.conn.execute(
            "SELECT id, mention_count FROM entities WHERE name=? AND type=?", (name, type)
        ).fetchone()
        if row:
            self.conn.execute(
                "UPDATE entities SET mention_count=mention_count+1, last_seen=? WHERE id=?",
                (now, int(row["id"])),
            )
            self.conn.commit()
            return int(row["id"])
        cur = self.conn.execute(
            """INSERT INTO entities(name, type, mention_count, first_seen, last_seen, aliases)
               VALUES (?,?,?,?,?,?)""",
            (name, type, 1, now, now, json.dumps(aliases or [], ensure_ascii=False)),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def link_entity(self, memory_id: int, entity_id: int, weight: float = 1.0) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO memory_entities(memory_id, entity_id, weight) VALUES (?,?,?)",
            (int(memory_id), int(entity_id), float(weight)),
        )
        self.conn.commit()

    def memory_entities(self, memory_id: int) -> list[int]:
        rows = self.conn.execute(
            "SELECT entity_id FROM memory_entities WHERE memory_id=?", (int(memory_id),)
        ).fetchall()
        return [int(r["entity_id"]) for r in rows]

    def entities_in_text(self, text: str, limit: int = 6) -> list[int]:
        """query 里直接出现的实体（名字长度 >=2，避免单字误命中）。"""
        text = str(text or "")
        if not text:
            return []
        rows = self.conn.execute(
            "SELECT id, name FROM entities WHERE length(name) >= 2 ORDER BY mention_count DESC"
        ).fetchall()
        hits = []
        for r in rows:
            if str(r["name"]) in text:
                hits.append(int(r["id"]))
                if len(hits) >= limit:
                    break
        return hits

    def memory_ids_of_entities(self, entity_ids: Iterable[int], limit: int = 200) -> set[int]:
        ids = [int(e) for e in entity_ids]
        if not ids:
            return set()
        marks = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"SELECT DISTINCT memory_id FROM memory_entities WHERE entity_id IN ({marks}) LIMIT ?",
            (*ids, int(limit)),
        ).fetchall()
        return {int(r["memory_id"]) for r in rows}

    def entities_of(self, memory_ids: Iterable[int]) -> set[int]:
        ids = [int(x) for x in memory_ids]
        if not ids:
            return set()
        marks = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"SELECT DISTINCT entity_id FROM memory_entities WHERE memory_id IN ({marks})",
            ids,
        ).fetchall()
        return {int(r["entity_id"]) for r in rows}

    def memories_by_entities(self, entity_ids: Iterable[int], limit: int = 50) -> list[int]:
        ids = [int(x) for x in entity_ids]
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"""SELECT memory_id, SUM(weight) AS w
                FROM memory_entities
                WHERE entity_id IN ({marks})
                GROUP BY memory_id
                ORDER BY w DESC
                LIMIT ?""",
            ids + [int(limit)],
        ).fetchall()
        return [int(r["memory_id"]) for r in rows]

    def neighbor_memories(self, memory_ids: Iterable[int], limit: int = 30) -> list[int]:
        """1 跳：共享实体的其它记忆。"""
        ents = self.entities_of(memory_ids)
        if not ents:
            return []
        exclude = {int(x) for x in memory_ids}
        return [m for m in self.memories_by_entities(ents, limit=limit) if m not in exclude]

    def find_entity_by_name(self, name: str) -> dict | None:
        name = str(name or "").strip()
        if not name:
            return None
        row = self.conn.execute(
            "SELECT * FROM entities WHERE name=? ORDER BY mention_count DESC LIMIT 1", (name,)
        ).fetchone()
        if row:
            return dict(row)
        row = self.conn.execute(
            "SELECT * FROM entities WHERE name LIKE ? ORDER BY mention_count DESC LIMIT 1",
            (f"%{name}%",),
        ).fetchone()
        if row:
            return dict(row)
        # 退一步：忽略 emoji/标点后比对（带 emoji 的昵称 → 去掉 emoji 后的名字）
        key = re.sub(r"[^\w\u4e00-\u9fff]+", "", name).lower()
        if key:
            for r in self.conn.execute("SELECT * FROM entities"):
                if re.sub(r"[^\w\u4e00-\u9fff]+", "", str(r["name"])).lower() == key:
                    return dict(r)
        return None

    def profiled_entities(self, *, min_profile_len: int = 10) -> list[dict]:
        rows = self.conn.execute(
            """SELECT e.id, e.name, e.type, e.profile,
                      (SELECT COUNT(*) FROM memory_entities me WHERE me.entity_id=e.id) AS degree
               FROM entities e
               WHERE LENGTH(e.profile) >= ?
               ORDER BY degree DESC""",
            (int(min_profile_len),),
        ).fetchall()
        return [dict(r) for r in rows]

    def set_profile(self, entity_id: int, text: str, now: float | None = None) -> None:
        self.conn.execute(
            "UPDATE entities SET profile=?, profile_at=? WHERE id=?",
            (str(text)[:600], now or time.time(), int(entity_id)),
        )
        self.conn.commit()

    def get_entity(self, entity_id: int) -> dict | None:
        row = self.conn.execute(
            """SELECT e.*,
                      (SELECT COUNT(*) FROM memory_entities me WHERE me.entity_id=e.id) AS degree
               FROM entities e WHERE e.id=?""",
            (int(entity_id),),
        ).fetchone()
        return dict(row) if row else None

    def entity_relations(self, entity_id: int, limit: int = 10) -> list[dict]:
        rows = self.conn.execute(
            """SELECT r.relation, e.name AS other, e.type AS other_type,
                      CASE WHEN r.src_id=? THEN 'out' ELSE 'in' END AS direction
               FROM relations r
               JOIN entities e ON e.id = CASE WHEN r.src_id=? THEN r.dst_id ELSE r.src_id END
               WHERE r.src_id=? OR r.dst_id=?
               ORDER BY r.weight DESC LIMIT ?""",
            (int(entity_id), int(entity_id), int(entity_id), int(entity_id), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

    def entities_needing_profile(
        self,
        *,
        min_degree: int = 2,
        older_than: float = 7 * 86400,
        limit: int = 10,
        now: float | None = None,
    ) -> list[dict]:
        now = now or time.time()
        rows = self.conn.execute(
            """SELECT e.id, e.name, e.type, e.mention_count, e.profile_at,
                      (SELECT COUNT(*) FROM memory_entities me WHERE me.entity_id=e.id) AS degree
               FROM entities e
               WHERE e.type IN ('person', 'topic')
                 AND (SELECT COUNT(*) FROM memory_entities me WHERE me.entity_id=e.id) >= ?
                 AND e.profile_at < ?
               ORDER BY degree DESC, e.last_seen DESC
               LIMIT ?""",
            (int(min_degree), float(now - older_than), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

    def absorb_entity(self, old_id: int, keep_id: int) -> int:
        """把 old 实体并进 keep：迁移记忆关联和关系边，然后删掉 old。"""
        old_id, keep_id = int(old_id), int(keep_id)
        if old_id == keep_id:
            return 0
        rows = self.conn.execute(
            "SELECT memory_id, weight FROM memory_entities WHERE entity_id=?", (old_id,)
        ).fetchall()
        moved = 0
        for r in rows:
            self.conn.execute(
                "INSERT OR REPLACE INTO memory_entities(memory_id, entity_id, weight) VALUES (?,?,?)",
                (int(r["memory_id"]), keep_id, float(r["weight"])),
            )
            moved += 1
        self.conn.execute("DELETE FROM memory_entities WHERE entity_id=?", (old_id,))
        self.conn.execute("UPDATE OR REPLACE relations SET src_id=? WHERE src_id=?", (keep_id, old_id))
        self.conn.execute("UPDATE OR REPLACE relations SET dst_id=? WHERE dst_id=?", (keep_id, old_id))
        lost = self.conn.execute(
            "SELECT mention_count FROM entities WHERE id=?", (old_id,)
        ).fetchone()
        if lost:
            self.conn.execute(
                "UPDATE entities SET mention_count = mention_count + ? WHERE id=?",
                (int(lost["mention_count"]), keep_id),
            )
        self.conn.execute("DELETE FROM entities WHERE id=?", (old_id,))
        self.conn.commit()
        return moved

    def rename_entity(self, entity_id: int, name: str) -> None:
        try:
            self.conn.execute("UPDATE entities SET name=? WHERE id=?", (name, int(entity_id)))
            self.conn.commit()
        except sqlite3.IntegrityError:
            pass

    def absorb_memory(self, drop_id: int, keep_id: int, similarity: float = 0.0) -> None:
        """把 drop 并进 keep：实体关联、访问次数、重要性取大，然后删掉 drop。"""
        drop_id, keep_id = int(drop_id), int(keep_id)
        keep = self.get_memory(keep_id)
        drop = self.get_memory(drop_id)
        if not keep or not drop:
            return
        for eid in self.memory_entities(drop_id):
            row = self.conn.execute(
                "SELECT weight FROM memory_entities WHERE memory_id=? AND entity_id=?",
                (drop_id, eid),
            ).fetchone()
            self.conn.execute(
                "INSERT OR REPLACE INTO memory_entities(memory_id, entity_id, weight) VALUES (?,?,?)",
                (keep_id, eid, float(row["weight"]) if row else 1.0),
            )
        self.conn.execute(
            """UPDATE memories SET
                 importance = MAX(importance, ?),
                 access_count = access_count + ?,
                 last_access = MAX(last_access, ?)
               WHERE id=?""",
            (
                float(drop["importance"]),
                int(drop["access_count"]),
                float(drop["last_access"]),
                keep_id,
            ),
        )
        self.conn.execute("UPDATE relations SET memory_id=? WHERE memory_id=?", (keep_id, drop_id))
        self.conn.commit()
        self.delete_memory(drop_id)

    def link_relation(self, src: int, dst: int, relation: str, memory_id: int | None = None, weight: float = 1.0) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO relations(src_id, dst_id, relation, weight, memory_id, created_at)
               VALUES (?,?,?,?,?,?)""",
            (int(src), int(dst), relation, float(weight), memory_id, time.time()),
        )
        self.conn.commit()

    def cooccurrence_edges(self, limit: int = 800) -> list[dict[str, Any]]:
        """共现边：两条记忆里同时出现的两个实体，连一条虚线。"""
        rows = self.conn.execute(
            """SELECT a.entity_id AS src, b.entity_id AS dst, COUNT(*) AS w
               FROM memory_entities a
               JOIN memory_entities b
                 ON a.memory_id = b.memory_id AND a.entity_id < b.entity_id
               GROUP BY a.entity_id, b.entity_id
               ORDER BY w DESC
               LIMIT ?""",
            (int(limit),),
        ).fetchall()
        return [{"src": int(r["src"]), "dst": int(r["dst"]), "weight": int(r["w"])} for r in rows]

    def entity_names(self) -> dict[int, str]:
        rows = self.conn.execute("SELECT id, name FROM entities").fetchall()
        return {int(r["id"]): str(r["name"]) for r in rows}

    def memories_of_entity(self, entity_id: int, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            """SELECT m.* FROM memories m
               JOIN memory_entities me ON me.memory_id = m.id
               WHERE me.entity_id = ? AND m.status = 'active'
               ORDER BY m.created_at DESC LIMIT ?""",
            (int(entity_id), int(limit)),
        ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    def graph(self, limit: int = 500) -> dict[str, list[dict]]:
        nodes = self.conn.execute(
            """SELECT e.id, e.name, e.type, e.mention_count,
                      (SELECT COUNT(*) FROM memory_entities me WHERE me.entity_id=e.id) AS degree
               FROM entities e ORDER BY degree DESC LIMIT ?""",
            (int(limit),),
        ).fetchall()
        edges = self.conn.execute(
            """SELECT src_id, dst_id, relation, weight FROM relations
               ORDER BY weight DESC LIMIT ?""",
            (int(limit * 3),),
        ).fetchall()
        return {
            "nodes": [dict(r) for r in nodes],
            "edges": [dict(r) for r in edges],
        }

    # ---------------------------------------------------------------- 维护

    def decay(
        self,
        rate: float = 0.01,
        now: float | None = None,
        *,
        access_boost: float = 0.5,
        access_window_days: float = 30.0,
        access_max_count: float = 10.0,
        protect_names: list[str] | None = None,
        core_floor: float = 0.45,
        emotion_factor: float = 0.4,
    ) -> int:
        """每日衰减：频率感知 + 情绪保护 + 核心人物地板。"""
        now = now or time.time()
        window = now - max(1.0, access_window_days) * 86400
        protect = {str(n).strip() for n in (protect_names or []) if str(n).strip()}
        core_ids: set[int] = set()
        if protect:
            for row in self.conn.execute(
                """SELECT DISTINCT me.memory_id FROM memory_entities me
                   JOIN entities e ON e.id = me.entity_id
                   WHERE e.name IN (%s)""" % ",".join("?" * len(protect)),
                list(protect),
            ):
                core_ids.add(int(row[0]))
        rows = self.conn.execute(
            "SELECT id, importance, access_count, last_access, valence, arousal FROM memories WHERE status='active'"
        ).fetchall()
        updates = []
        for r in rows:
            factor = 1.0
            if r["last_access"] and float(r["last_access"]) >= window:
                factor = 1.0 - access_boost * min(
                    1.0, float(r["access_count"]) / max(1.0, access_max_count)
                )
            rate_i = float(rate)
            val = abs(float(r["valence"] or 0))
            if val > 1:
                val /= 100.0
            aro = float(r["arousal"] or 0)
            if aro > 1:
                aro /= 100.0
            # 情绪强的记忆，忘得更慢
            if val >= 0.5 or aro >= 0.5:
                rate_i *= float(emotion_factor)
            new_imp = max(0.01, float(r["importance"]) * (1.0 - rate_i * factor))
            # 核心人物相关：不低于地板
            if int(r["id"]) in core_ids:
                new_imp = max(new_imp, float(core_floor))
            updates.append((round(new_imp, 4), int(r["id"])))
        if updates:
            self.conn.executemany("UPDATE memories SET importance=? WHERE id=?", updates)
            self.conn.commit()
        return len(updates)

    def archive(
        self,
        importance_below: float,
        older_than_days: float,
        now: float | None = None,
        *,
        protect_days: float = 30.0,
        drop_vectors: bool = True,
    ) -> int:
        """归档：最近 protect_days 内被访问过的不动；归档后可以丢掉向量省空间。"""
        now = now or time.time()
        cutoff = now - float(older_than_days) * 86400
        protect = now - float(protect_days) * 86400
        rows = self.conn.execute(
            """SELECT id FROM memories
               WHERE status='active' AND importance < ? AND created_at < ?
                 AND (last_access = 0 OR last_access < ?)""",
            (float(importance_below), cutoff, protect),
        ).fetchall()
        ids = [int(r["id"]) for r in rows]
        if not ids:
            return 0
        self.conn.executemany(
            "UPDATE memories SET status='archived', archived_at=? WHERE id=?",
            [(now, mid) for mid in ids],
        )
        if drop_vectors:
            self.conn.executemany("DELETE FROM vectors WHERE memory_id=?", [(m,) for m in ids])
        self.conn.commit()
        return len(ids)

    def trim_kind(self, kind: str, max_count: int, now: float | None = None) -> int:
        """某一类记忆超过上限时，把最不重要、最旧的归档。"""
        now = now or time.time()
        rows = self.conn.execute(
            """SELECT id FROM memories WHERE status='active' AND kind=?
               ORDER BY importance ASC, created_at ASC""",
            (kind,),
        ).fetchall()
        if len(rows) <= int(max_count):
            return 0
        victims = [int(r["id"]) for r in rows[: len(rows) - int(max_count)]]
        self.conn.executemany(
            "UPDATE memories SET status='archived', archived_at=? WHERE id=?",
            [(now, mid) for mid in victims],
        )
        self.conn.executemany("DELETE FROM vectors WHERE memory_id=?", [(mid,) for mid in victims])
        self.conn.commit()
        return len(victims)

    def purge_archived(self, keep_days: float = 180.0, now: float | None = None) -> int:
        """归档超过 keep_days 的彻底删除，控制增长曲线。"""
        now = now or time.time()
        cutoff = now - float(keep_days) * 86400
        rows = self.conn.execute(
            "SELECT id FROM memories WHERE status='archived' AND archived_at > 0 AND archived_at < ?",
            (cutoff,),
        ).fetchall()
        ids = [int(r["id"]) for r in rows]
        for mid in ids:
            self.delete_memory(mid)
        return len(ids)

    def drop_orphan_entities(self) -> int:
        """删掉没有任何记忆引用的实体（合并/删除后留下的空节点）。"""
        cur = self.conn.execute(
            """DELETE FROM entities
               WHERE id NOT IN (SELECT DISTINCT entity_id FROM memory_entities)"""
        )
        self.conn.commit()
        return cur.rowcount

    def memories_between(self, start: float, end: float, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            """SELECT * FROM memories
               WHERE status='active' AND created_at >= ? AND created_at < ?
               ORDER BY importance DESC, created_at DESC LIMIT ?""",
            (float(start), float(end), int(limit)),
        ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    # ---------------------------------------------------------------- 每日计数

    def bump_daily(self, key: str, n: float = 1.0, day: str | None = None) -> None:
        day = day or time.strftime("%Y-%m-%d")
        self.conn.execute(
            """INSERT INTO daily_stats(day, key, value) VALUES (?,?,?)
               ON CONFLICT(day, key) DO UPDATE SET value = value + excluded.value""",
            (day, key, float(n)),
        )
        self.conn.commit()

    def daily_stats(self, day: str | None = None) -> dict[str, float]:
        day = day or time.strftime("%Y-%m-%d")
        rows = self.conn.execute(
            "SELECT key, value FROM daily_stats WHERE day=?", (day,)
        ).fetchall()
        return {str(r["key"]): float(r["value"]) for r in rows}

    # ---------------------------------------------------------------- 冲突

    def add_conflict(self, a_id: int, b_id: int, reason: str, now: float | None = None) -> bool:
        row = self.conn.execute(
            "SELECT id FROM conflicts WHERE ((a_id=? AND b_id=?) OR (a_id=? AND b_id=?)) AND resolved=0",
            (a_id, b_id, b_id, a_id),
        ).fetchone()
        if row:
            return False
        self.conn.execute(
            "INSERT INTO conflicts(a_id, b_id, reason, created_at) VALUES (?,?,?,?)",
            (a_id, b_id, reason, now or time.time()),
        )
        self.conn.commit()
        return True

    def list_conflicts(self, resolved: bool = False, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM conflicts WHERE resolved=? ORDER BY id DESC LIMIT ?",
            (1 if resolved else 0, int(limit)),
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["a"] = self.get_memory(int(r["a_id"]))
            item["b"] = self.get_memory(int(r["b_id"]))
            out.append(item)
        return out

    def resolve_conflict(self, conflict_id: int, keep_id: int | None = None) -> None:
        self.conn.execute("UPDATE conflicts SET resolved=1 WHERE id=?", (int(conflict_id),))
        self.conn.commit()

    def set_meta(self, memory_id: int, meta: dict) -> None:
        self.conn.execute(
            "UPDATE memories SET meta=? WHERE id=?",
            (json.dumps(meta, ensure_ascii=False), int(memory_id)),
        )
        self.conn.commit()

    # ---------------------------------------------------------------- 工作记忆

    def add_working(
        self,
        session_id: str,
        text: str,
        kind: str = "todo",
        now: float | None = None,
        ttl_days: float = 3.0,
        keep: int = 5,
    ) -> int | None:
        text = re.sub(r"\s+", " ", str(text or "")).strip()[:60]
        if len(text) < 2:
            return None
        now = now or time.time()
        for row in self.list_working(session_id, limit=20):
            if _bigram_sim(text, row["text"]) >= 0.5:
                return int(row["id"])
        cur = self.conn.execute(
            "INSERT INTO working(session_id, text, kind, created_at, expires_at) VALUES (?,?,?,?,?)",
            (session_id, text, kind, now, now + float(ttl_days) * 86400),
        )
        self.conn.execute(
            """DELETE FROM working WHERE session_id=? AND done=0 AND id NOT IN (
                   SELECT id FROM working WHERE session_id=? AND done=0
                   ORDER BY id DESC LIMIT ?)""",
            (session_id, session_id, int(keep)),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_working(self, session_id: str, include_done: bool = False, limit: int = 10) -> list[dict]:
        sql = "SELECT * FROM working WHERE session_id=?"
        if not include_done:
            sql += " AND done=0"
        sql += " ORDER BY id DESC LIMIT ?"
        return [dict(r) for r in self.conn.execute(sql, (session_id, int(limit))).fetchall()]

    def close_working(self, session_id: str, text: str, now: float | None = None) -> int:
        closed = 0
        for row in self.list_working(session_id, limit=20):
            if _bigram_sim(text, row["text"]) >= 0.5:
                self.conn.execute("UPDATE working SET done=1 WHERE id=?", (int(row["id"]),))
                closed += 1
        if closed:
            self.conn.commit()
        return closed

    def prune_working(self, now: float | None = None) -> int:
        now = now or time.time()
        cur = self.conn.execute("DELETE FROM working WHERE expires_at < ? OR done=1", (now,))
        self.conn.commit()
        return cur.rowcount

    # ---------------------------------------------------------------- 原始消息（语感分析用）

    def add_raw(
        self,
        session_id: str,
        role: str,
        text: str,
        sender: str = "",
        now: float | None = None,
        keep: int = 2000,
    ) -> None:
        text = str(text or "").strip()
        if not text:
            return
        self.conn.execute(
            "INSERT INTO raw_messages(session_id, role, sender, text, created_at) VALUES (?,?,?,?,?)",
            (str(session_id)[:80], role, str(sender)[:32], text[:400], now or time.time()),
        )
        self.conn.execute(
            "DELETE FROM raw_messages WHERE id NOT IN (SELECT id FROM raw_messages ORDER BY id DESC LIMIT ?)",
            (int(keep),),
        )
        self.conn.commit()

    def recent_raw(self, role: str, limit: int = 300) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM raw_messages WHERE role=? ORDER BY id DESC LIMIT ?",
            (role, int(limit)),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def stats(self) -> dict[str, Any]:
        row = self.conn.execute(
            """SELECT
                 (SELECT COUNT(*) FROM memories WHERE status='active') AS active,
                 (SELECT COUNT(*) FROM memories WHERE status='archived') AS archived,
                 (SELECT COUNT(*) FROM entities) AS entities,
                 (SELECT COUNT(*) FROM relations) AS relations,
                 (SELECT COUNT(*) FROM vectors) AS vectors,
                 (SELECT MIN(created_at) FROM memories) AS oldest"""
        ).fetchone()
        kinds = self.conn.execute(
            "SELECT kind, COUNT(*) AS n FROM memories WHERE status='active' GROUP BY kind"
        ).fetchall()
        return {
            "active": int(row["active"]),
            "archived": int(row["archived"]),
            "entities": int(row["entities"]),
            "relations": int(row["relations"]),
            "vectors": int(row["vectors"]),
            "oldest": float(row["oldest"] or 0),
            "kinds": {r["kind"]: int(r["n"]) for r in kinds},
        }

    def close(self) -> None:
        self.conn.close()

    @staticmethod
    def _row_to_memory(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        try:
            d["meta"] = json.loads(d.get("meta") or "{}")
        except json.JSONDecodeError:
            d["meta"] = {}
        return d
