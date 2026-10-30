# -*- coding: utf-8 -*-
"""记忆大脑的单元测试：只依赖标准库 + numpy，不需要 AstrBot。"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

from astrbot_plugin_memory_brain import (  # noqa: E402
    abstract, conflict, consolidate, extractor, lessons, media, multimodal, procedural,
    profile, reflection, style, working,
)

try:  # 插件接线依赖 AstrBot，没有就跳过相关用例
    import astrbot  # noqa: F401

    HAS_ASTRBOT = True
except Exception:
    HAS_ASTRBOT = False
from astrbot_plugin_memory_brain.engine import MemoryEngine  # noqa: E402
from astrbot_plugin_memory_brain.store import Store, bigrams  # noqa: E402


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(Path(self.tmp) / "brain.db")

    def tearDown(self):
        self.store.close()

    def test_bigrams_chinese(self):
        self.assertEqual(bigrams("用户吃粥"), ["用户", "户吃", "吃粥"])
        self.assertEqual(bigrams(""), [])

    def test_insert_and_get(self):
        mid = self.store.insert_memory(text="今天和用户吃粥", importance=0.7)
        mem = self.store.get_memory(mid)
        self.assertEqual(mem["text"], "今天和用户吃粥")
        self.assertAlmostEqual(mem["importance"], 0.7)
        self.assertEqual(self.store.stats()["active"], 1)

    def test_fts_chinese(self):
        self.store.insert_memory(text="今天和用户吃粥，她说没胃口")
        self.store.insert_memory(text="管理员让我整理开票明细表格")
        hits = self.store.fts_search("用户胃口")
        self.assertTrue(hits)
        top_id = hits[0][0]
        self.assertIn("用户", self.store.get_memory(top_id)["text"])

    def test_entities_and_graph(self):
        a = self.store.insert_memory(text="和用户吃粥")
        b = self.store.insert_memory(text="用户说她想吃甜的")
        c = self.store.insert_memory(text="管理员在改表格")
        e_hong = self.store.upsert_entity("用户", "person")
        e_master = self.store.upsert_entity("管理员", "person")
        self.store.link_entity(a, e_hong)
        self.store.link_entity(b, e_hong)
        self.store.link_entity(c, e_master)

        self.assertEqual(self.store.memory_entities(a), [e_hong])
        self.assertIn(b, self.store.neighbor_memories([a]))
        self.assertNotIn(c, self.store.neighbor_memories([a]))

        self.store.link_relation(e_master, e_hong, "管理员")
        graph = self.store.graph()
        self.assertEqual(len(graph["nodes"]), 2)
        self.assertEqual(len(graph["edges"]), 1)

    def test_decay_and_archive(self):
        now = time.time()
        old = self.store.insert_memory(text="很久以前的小事", importance=0.32, created_at=now - 40 * 86400)
        self.store.insert_memory(text="重要的事", importance=0.9, created_at=now - 40 * 86400)
        self.store.decay(0.2, now=now)
        archived = self.store.archive(0.3, 30, now=now)
        self.assertEqual(archived, 1)
        self.assertEqual(self.store.get_memory(old)["status"], "archived")

    def test_delete(self):
        mid = self.store.insert_memory(text="会被删掉")
        self.assertTrue(self.store.delete_memory(mid))
        self.assertIsNone(self.store.get_memory(mid))
        self.assertEqual(self.store.fts_search("删掉"), [])


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(Path(self.tmp) / "brain.db")
        self.now = 1_800_000_000.0
        # 简单可解释的假向量：三个主题各占一个维度
        self.topic = {"粥": 0, "表格": 1, "鲸鱼": 2}

        def fake_embed(text):
            v = np.zeros(3, dtype=np.float32)
            for key, idx in self.topic.items():
                if key in text:
                    v[idx] = 1.0
            if not v.any():
                v[0] = 0.1
            return v

        self.engine = MemoryEngine(self.store, embed=fake_embed)

    def tearDown(self):
        self.store.close()

    def test_semantic_recall(self):
        self.engine.remember("和用户吃粥", importance=0.5, now=self.now)
        self.engine.remember("整理开票表格", importance=0.5, now=self.now)
        hits = self.engine.recall("粥", k=2, now=self.now)
        self.assertTrue(hits)
        self.assertIn("粥", hits[0]["text"])

    def test_keyword_only_without_embedding(self):
        engine = MemoryEngine(self.store, embed=None)
        engine.remember("晚上刷到抹香鲸竖着睡觉", now=self.now)
        engine.remember("管理员在改表格", now=self.now)
        hits = engine.recall("抹香鲸怎么睡觉", k=2, now=self.now)
        self.assertTrue(hits)
        self.assertIn("抹香鲸", hits[0]["text"])
        self.assertTrue(hits[0]["why"]["keyword"] > 0) if "why" in hits[0] else None

    def test_explain_breakdown(self):
        self.engine.remember("和用户吃粥", importance=0.9, now=self.now)
        hits = self.engine.recall("粥", k=1, now=self.now, explain=True)
        why = hits[0]["why"]
        for key in ("semantic", "keyword", "importance", "recency", "graph"):
            self.assertIn(key, why)

    def test_recency_boost(self):
        old = self.engine.remember("和用户吃粥", importance=0.5, now=self.now - 90 * 86400)
        new = self.engine.remember("和用户吃粥", importance=0.5, now=self.now)
        # 关掉去重重排，单独验证「新鲜度」这一路
        hits = self.engine.recall("用户粥", k=2, now=self.now, explain=True, rerank=False)
        ids = [h["id"] for h in hits]
        self.assertEqual(ids[0], new, "新的应该排在旧的前面")
        self.assertIn(old, ids)


    def test_mood_congruent_boost(self):
        pos = self.engine.remember("今天的事", importance=0.50, valence=0.9)
        neg = self.engine.remember("今天的事", importance=0.48, valence=-0.9)
        hits = self.engine.recall("今天的事", k=2, mood_valence=-1.0, rerank=False)
        self.assertEqual(hits[0]["id"], neg, "心情差时，负面记忆应该排前面")
        hits2 = self.engine.recall("今天的事", k=2, mood_valence=1.0, rerank=False)
        self.assertEqual(hits2[0]["id"], pos)

    def test_rerank_dedupes_near_duplicates(self):
        self.engine.remember("和用户吃粥", importance=0.5, now=self.now)
        self.engine.remember("和用户吃粥", importance=0.5, now=self.now)
        hits = self.engine.recall("用户粥", k=2, now=self.now)
        self.assertEqual(len(hits), 1)

    def test_graph_expansion(self):
        # 两条记忆共享实体但没有共同关键词
        a = self.engine.remember("和用户吃粥", entities=[("用户", "person")], now=self.now)
        b = self.engine.remember("用户最近在学剪辑", entities=[("用户", "person")], now=self.now)
        # 查询只命中 a 的关键词，b 靠图谱被带出来
        hits = self.engine.recall("粥", k=5, now=self.now, explain=True)
        ids = [h["id"] for h in hits]
        self.assertIn(a, ids)
        self.assertIn(b, ids)
        self.assertTrue(any(h.get("why", {}).get("graph", 0) > 0 for h in hits))

    def test_touch_updates_access(self):
        mid = self.engine.remember("和用户吃粥", now=self.now)
        self.engine.recall("粥", k=1, now=self.now)
        mem = self.store.get_memory(mid)
        self.assertEqual(mem["access_count"], 1)
        self.assertGreater(mem["last_access"], 0)

    def test_empty_query(self):
        self.assertEqual(self.engine.recall(""), [])

    def test_stats(self):
        self.engine.remember("和用户吃粥", now=self.now)
        self.engine.remember("管理员在改表格", now=self.now)
        s = self.engine.stats()
        self.assertEqual(s["active"], 2)
        self.assertIn("episode", s["kinds"])


class ExtractorTest(unittest.TestCase):
    def test_parse_plain_json(self):
        data = extractor.parse_json('{"summary": "和用户吃粥"}')
        self.assertEqual(data["summary"], "和用户吃粥")

    def test_parse_fenced_json(self):
        text = '```json\n{"summary": "在改表格", "kind": "fact"}\n```'
        self.assertEqual(extractor.parse_json(text)["kind"], "fact")

    def test_parse_garbage(self):
        self.assertIsNone(extractor.parse_json("今天天气不错"))

    def test_normalize_clamps(self):
        data = extractor.normalize(
            {
                "summary": "  和用户吃粥  ",
                "kind": "weird",
                "importance": 5,
                "valence": -9,
                "entities": [{"name": "用户", "type": "person"}, {"name": "", "type": "thing"}],
                "relations": [{"src": "用户", "dst": "粥", "relation": "吃"}],
            }
        )
        self.assertEqual(data["summary"], "和用户吃粥")
        self.assertEqual(data["kind"], "episode")
        self.assertEqual(data["importance"], 1.0)
        self.assertEqual(data["valence"], -1.0)
        names = [e["name"] for e in data["entities"]]
        self.assertEqual(names, ["用户", "粥"])

    def test_normalize_rejects_empty(self):
        self.assertIsNone(extractor.normalize({"summary": "嗯"}))
        self.assertIsNone(extractor.normalize(None))

    def test_fallback_uses_speakers(self):
        data = extractor.fallback([("用户", "在吗"), ("管理员", "在")])
        self.assertTrue(data["fallback"])
        self.assertEqual(sorted(e["name"] for e in data["entities"]), ["用户", "管理员"])


@unittest.skipUnless(HAS_ASTRBOT, "需要 AstrBot 环境（把 AstrBot 根目录加到 PYTHONPATH）")
class PluginFlushTest(unittest.TestCase):
    """用假的 LLM 验证「摘要 → 写入实体/关系」这条链。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cwd = Path.cwd()
        os.chdir(self.tmp)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

    def tearDown(self):
        os.chdir(self.cwd)

    def _plugin(self, provider):
        import importlib
        main = importlib.import_module("astrbot_plugin_memory_brain.main")
        importlib.reload(main)

        class FakeContext:
            def get_using_provider(self, umo=None):
                return provider

        plugin = main.MemoryBrainPlugin(FakeContext(), {"llm_summary": True})
        return plugin, main

    def test_flush_uses_llm_summary_and_graph(self):
        class Provider:
            async def text_chat(self, prompt=None, system_prompt=None):
                class R:
                    completion_text = (
                        '{"summary":"和用户吃粥，她说没胃口","kind":"episode",'
                        '"importance":0.6,"valence":-0.2,'
                        '"entities":[{"name":"用户","type":"person"},{"name":"粥","type":"thing"}],'
                        '"relations":[{"src":"用户","dst":"粥","relation":"吃"}]}'
                    )
                return R()

        plugin, _ = self._plugin(Provider())
        import asyncio
        asyncio.run(plugin._flush_rows("qq:test", [("用户", "今天没胃口"), ("管理员", "那喝点粥")]))

        stats = plugin.engine.stats()
        self.assertEqual(stats["active"], 1)
        self.assertEqual(stats["entities"], 2)
        self.assertEqual(stats["relations"], 1)

        rows = plugin.engine.store.list_memories()
        self.assertEqual(rows[0]["text"], "和用户吃粥，她说没胃口")
        self.assertEqual(rows[0]["kind"], "episode")
        plugin.store.close()


    def test_generate_profile_with_fake_llm(self):
        import asyncio

        class Provider:
            async def text_chat(self, prompt=None, system_prompt=None):
                class R:
                    completion_text = "画像：她喜欢甜的，最近没什么胃口，做开票表很细。"
                return R()

        plugin, _ = self._plugin(Provider())
        eid = plugin.store.upsert_entity("用户", "person")
        for i in range(2):
            mid = plugin.store.insert_memory(text=f"用户喜欢甜的 {i}")
            plugin.store.link_entity(mid, eid)
        done = asyncio.run(plugin._generate_profiles(force_id=eid))
        self.assertEqual(done, 1)
        self.assertIn("喜欢甜", plugin.store.get_entity(eid)["profile"])
        plugin.store.close()


    def test_generate_reflection_once_per_day(self):
        import asyncio

        class Provider:
            async def text_chat(self, prompt=None, system_prompt=None):
                class R:
                    completion_text = "今天陪用户喝粥。\n她说没胃口，我有点担心。\n晚上把鲸鱼视频剪完了。"
                return R()

        plugin, _ = self._plugin(Provider())
        now = time.time()
        for i in range(3):
            plugin.store.insert_memory(text=f"今天的事 {i}", created_at=now - i * 60)
        done = asyncio.run(plugin._generate_reflection())
        self.assertEqual(done, 1)
        rows = plugin.store.list_memories(kind="reflection")
        self.assertEqual(len(rows), 1)
        self.assertIn("用户", rows[0]["text"])
        self.assertTrue(rows[0]["meta"].get("reflection_date"))
        # 同一天再跑不该重复写
        self.assertEqual(asyncio.run(plugin._generate_reflection()), 0)
        plugin.store.close()


    def test_generate_facts_with_fake_llm(self):
        import asyncio

        class Provider:
            async def text_chat(self, prompt=None, system_prompt=None):
                class R:
                    completion_text = '[{"fact": "用户喜欢甜食", "importance": 0.7, "entities": [{"name": "用户", "type": "person"}]}]'
                return R()

        plugin, _ = self._plugin(Provider())
        now = time.time()
        for i in range(3):
            plugin.store.insert_memory(text=f"今天和用户吃了甜食 {i}", created_at=now - i * 60)
        done = asyncio.run(plugin._generate_facts())
        self.assertEqual(done, 1)
        facts = plugin.store.list_memories(kind="fact")
        self.assertEqual(len(facts), 1)
        self.assertIn("甜食", facts[0]["text"])
        self.assertTrue(facts[0]["meta"].get("source_ids"))
        # 同一天再跑一次应该去重
        self.assertEqual(asyncio.run(plugin._generate_facts()), 0)
        plugin.store.close()


    def test_learn_skill_with_fake_llm(self):
        import asyncio

        class Provider:
            async def text_chat(self, prompt=None, system_prompt=None):
                class R:
                    completion_text = ('{"task": "核对采购表", "steps": ["逐行核对金额", '
                                       '"标红异常", "标黄状态", "确认图片"], "entities": '
                                       '[{"name": "表格", "type": "thing"}]}')
                return R()

        plugin, _ = self._plugin(Provider())
        for i in range(4):
            plugin.store.add_raw("s1", "user", f"帮我核对第{i}张表", "用户")
        done = asyncio.run(plugin._learn_skill("s1"))
        self.assertEqual(done, 1)
        skills = plugin.store.list_memories(kind="skill")
        self.assertEqual(len(skills), 1)
        self.assertIn("核对", skills[0]["text"])
        # 相同做法不重复写
        self.assertEqual(asyncio.run(plugin._learn_skill("s1")), 0)
        plugin.store.close()


    def test_caption_image_stores_memory(self):
        import asyncio

        class Provider:
            async def text_chat(self, prompt=None, system_prompt=None, image_urls=None):
                class R:
                    completion_text = "一张桌子，上面有一碗粥和一碟酱黄瓜"
                return R()

        plugin, _ = self._plugin(Provider())
        done = asyncio.run(plugin._caption_image(None, "http://example.com/a.jpg", "用户"))
        rows = plugin.store.list_memories(source="vision")
        self.assertEqual(len(rows), 1)
        self.assertIn("酱黄瓜", rows[0]["text"])
        self.assertEqual(plugin.store.count_memories(source="vision"), 1)
        plugin.store.close()


    def test_import_media_dedupes(self):
        import asyncio
        import json
        import tempfile as tf

        plugin, _ = self._plugin(None)
        path = Path(tf.mkdtemp()) / "j.jsonl"
        rows = [
            {"title": "抹香鲸竖着睡", "url": "https://a/1", "note": "靠吐泡泡控制浮力"},
            {"title": "Gemma 4 开源", "url": "https://a/2", "note": "31B 数学 89.2%"},
        ]
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
        self.assertEqual(asyncio.run(plugin._import_media(paths=[path])), 2)
        self.assertEqual(asyncio.run(plugin._import_media(paths=[path])), 0)
        self.assertEqual(plugin.store.count_memories(source="media"), 2)
        plugin.store.close()


    def test_speaker_block_lists_others(self):
        plugin, _ = self._plugin(None)
        plugin.store.add_raw("qq_main:GroupMessage:1", "user", "在吗", "用户")
        plugin.store.add_raw("qq_main:GroupMessage:1", "user", "嗯", "管理员")

        class E:
            def get_group_id(self): return "1"
            def get_sender_name(self): return "用户"
            @property
            def unified_msg_origin(self): return "qq_main:GroupMessage:1"

        block = plugin._speaker_block(E())
        self.assertIn("【当前说话人】用户", block)
        self.assertIn("管理员", block)
        plugin.store.close()


    def test_import_life_notes_split(self):
        import asyncio
        import json
        import tempfile as tf

        plugin, _ = self._plugin(None)
        path = Path(tf.mkdtemp()) / "notes.jsonl"
        rows = [
            {"ts": 1, "kind": "study", "keep": "long", "text": "学到一个道理：先做完再完美"},
            {"ts": 2, "kind": "phone", "keep": "temp", "text": "刷到一个搞笑视频"},
        ]
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
        self.assertEqual(asyncio.run(plugin._import_life_notes(path=path)), 2)
        self.assertEqual(len(plugin.store.list_memories(source="life")), 1)
        self.assertEqual(len(plugin.store.list_working("life")), 1)
        self.assertEqual(asyncio.run(plugin._import_life_notes(path=path)), 0)
        plugin.store.close()


    def test_generate_lessons_with_fake_llm(self):
        import asyncio

        class Provider:
            async def text_chat(self, prompt=None, system_prompt=None):
                class R:
                    completion_text = '[{"lesson": "先做完再完美，别一开始就抠细节", "why": "拖太久"}]'
                return R()

        plugin, _ = self._plugin(Provider())
        for i in range(4):
            plugin.store.insert_memory(text=f"看到一条关于效率的内容 {i}", source="media", importance=0.5)
        done = asyncio.run(plugin._generate_lessons())
        self.assertEqual(done, 1)
        self.assertEqual(len(plugin.store.list_memories(source="lesson")), 1)
        self.assertEqual(asyncio.run(plugin._generate_lessons()), 0)
        plugin.store.close()


    def test_correction_marks_and_records(self):
        plugin, _ = self._plugin(None)
        mid = plugin.store.insert_memory(text="用户喜欢吃甜的", importance=0.6)

        class E:
            def get_message_str(self): return "不是，你记错了"
            def get_sender_name(self): return "管理员"
            def get_group_id(self): return None
            @property
            def unified_msg_origin(self): return "s1"

        plugin._last_hits["s1"] = [mid]
        plugin._maybe_correction(E())
        self.assertTrue((plugin.store.get_memory(mid).get("meta") or {}).get("corrected"))
        self.assertEqual(len(plugin.store.list_memories(source="correction")), 1)
        plugin.store.close()

    def test_self_review_writes_reflection(self):
        import asyncio

        class Provider:
            async def text_chat(self, prompt=None, system_prompt=None):
                class R:
                    completion_text = "这周‘先做完再完美’用上了，但早睡这条还是没做到。"
                return R()

        plugin, _ = self._plugin(Provider())
        plugin.store.insert_memory(text="学到：先做完再完美", source="lesson")
        out = []
        async def run():
            class E:
                message_str = "/大脑 自省"
                def get_message_str(self): return self.message_str
                def plain_result(self, x): return x
            async for r in plugin.cmd_brain(E()):
                out.append(r)
        asyncio.run(run())
        self.assertTrue(any("先做完" in str(x) for x in out))
        self.assertEqual(len(plugin.store.list_memories(source="review")), 1)
        plugin.store.close()


    def test_skill_trigger_requires_task_words(self):
        import asyncio

        class Provider:
            async def text_chat(self, prompt=None, system_prompt=None):
                class R:
                    completion_text = '{"task": "改表格", "steps": ["看原表", "改格式"], "entities": []}'
                return R()

        plugin, _ = self._plugin(Provider())

        class E:
            def __init__(self, text): self.t = text
            def get_message_str(self): return self.t
            def get_sender_name(self): return "用户"
            def get_group_id(self): return None
            @property
            def unified_msg_origin(self): return "s1"

        # 只有寒暄，没有任务动词 → 不触发
        for i in range(4):
            plugin.store.add_raw("s1", "user", f"今天天气不错 {i}", "用户")
        async def call(text):
            plugin._maybe_learn_skill(E(text))
            await asyncio.sleep(0.05)

        asyncio.run(call("谢谢"))
        self.assertNotIn("s1", plugin._skill_cooldown)

        # 有任务动词 → 触发
        for i in range(4):
            plugin.store.add_raw("s1", "user", f"帮我改一下表格 {i}", "用户")
        asyncio.run(call("好了"))
        self.assertIn("s1", plugin._skill_cooldown)
        plugin.store.close()

    def test_flush_falls_back_when_llm_broken(self):
        class Broken:
            async def text_chat(self, prompt=None, system_prompt=None):
                raise RuntimeError("boom")

        plugin, _ = self._plugin(Broken())
        import asyncio
        asyncio.run(plugin._flush_rows("qq:test", [("用户", "今天没胃口")]))
        rows = plugin.engine.store.list_memories()
        self.assertEqual(len(rows), 1)
        self.assertIn("用户", rows[0]["text"])
        plugin.store.close()


class ConsolidateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(Path(self.tmp) / "brain.db")
        self.engine = MemoryEngine(self.store)

    def tearDown(self):
        self.store.close()

    def test_merge_entities_same_name_after_strip(self):
        a = self.store.upsert_entity("用户", "person")
        b = self.store.upsert_entity("用户🌸️", "person")
        m = self.store.insert_memory(text="用户吃粥")
        self.store.link_entity(m, a)
        self.store.link_entity(m, b)

        result = consolidate.merge_duplicate_entities(self.store)
        self.assertEqual(result["merged"], 1)
        self.assertEqual(self.store.stats()["entities"], 1)

    def test_alias_map_merges_me_and_name(self):
        me = self.store.upsert_entity("我", "person")
        name = self.store.upsert_entity("角色", "person")
        self.store.upsert_entity("管理员", "person")
        m = self.store.insert_memory(text="我说了句话")
        self.store.link_entity(m, me)
        self.store.link_entity(m, name)

        result = consolidate.merge_duplicate_entities(self.store, alias_map={"我": "角色"})
        self.assertEqual(result["merged"], 1)
        self.assertEqual(self.store.stats()["entities"], 2)
        names = {r["name"] for r in self.store.conn.execute("SELECT name FROM entities")}
        self.assertIn("角色", names)
        self.assertNotIn("我", names)

    def test_merge_similar_memories(self):
        v = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        a = self.engine.remember("用户没胃口", importance=0.4, vector=v, now=1000.0)
        b = self.engine.remember("用户最近吃不下", importance=0.7, vector=v, now=2000.0)
        result = consolidate.merge_similar_memories(self.store, threshold=0.9)
        self.assertEqual(result["merged"], 1)
        self.assertEqual(self.store.stats()["active"], 1)
        kept = self.store.get_memory(b)
        self.assertAlmostEqual(kept["importance"], 0.7)

    def test_dissimilar_not_merged(self):
        a = np.array([1.0, 0.0], dtype=np.float32)
        b = np.array([0.0, 1.0], dtype=np.float32)
        self.engine.remember("用户吃粥", vector=a, now=1000.0)
        self.engine.remember("管理员在改表格", vector=b, now=1000.0)
        result = consolidate.merge_similar_memories(self.store, threshold=0.9)
        self.assertEqual(result["merged"], 0)
        self.assertEqual(self.store.stats()["active"], 2)

    def test_sleep_returns_counts(self):
        self.store.upsert_entity("用户", "person")
        self.store.upsert_entity("用户🌸️", "person")
        result = consolidate.sleep(self.store)
        self.assertEqual(result["entities_merged"], 1)


class ProfileTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(tempfile.mkdtemp()) / "brain.db")
        self.engine = MemoryEngine(self.store)

    def tearDown(self):
        self.store.close()

    def test_prompt_contains_facts(self):
        text = profile.build_prompt(
            "用户", "person", [{"text": "用户喜欢甜的", "date": "2026-10-27"}], []
        )
        self.assertIn("用户", text)
        self.assertIn("喜欢甜的", text)

    def test_clean_strips_prefix(self):
        self.assertEqual(profile.clean("画像：她喜欢甜的"), "她喜欢甜的")
        self.assertEqual(profile.clean("  她 最近 没胃口 "), "她 最近 没胃口")

    def test_store_profile_roundtrip(self):
        eid = self.store.upsert_entity("用户", "person")
        m = self.store.insert_memory(text="用户喜欢甜的")
        self.store.link_entity(m, eid)
        self.store.set_profile(eid, "她喜欢甜的，最近没胃口。")
        row = self.store.get_entity(eid)
        self.assertEqual(row["profile"], "她喜欢甜的，最近没胃口。")
        self.assertEqual(row["degree"], 1)

    def test_entities_needing_profile_filters_by_degree(self):
        hot = self.store.upsert_entity("用户", "person")
        cold = self.store.upsert_entity("路人", "person")
        for i in range(3):
            m = self.store.insert_memory(text=f"用户的事 {i}")
            self.store.link_entity(m, hot)
        m = self.store.insert_memory(text="路人的事")
        self.store.link_entity(m, cold)
        rows = self.store.entities_needing_profile(min_degree=2)
        self.assertEqual([r["name"] for r in rows], ["用户"])

    def test_set_profile_marks_time(self):
        eid = self.store.upsert_entity("用户", "person")
        self.store.set_profile(eid, "印象", now=12345.0)
        self.assertEqual(self.store.get_entity(eid)["profile_at"], 12345.0)


class ProfileInjectionTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(tempfile.mkdtemp()) / "brain.db")
        e = self.store.upsert_entity("用户", "person")
        self.store.set_profile(e, "她喜欢甜的，最近没胃口。")
        b = self.store.upsert_entity("宠物", "person")
        self.store.set_profile(b, "宠物爱越狱，胆子大。")
        self.store.upsert_entity("路人", "person")

    def tearDown(self):
        self.store.close()

    def test_find_entity(self):
        self.assertEqual(self.store.find_entity_by_name("用户")["name"], "用户")
        self.assertIsNone(self.store.find_entity_by_name("不存在的人"))

    def test_profiled_entities_only_with_profile(self):
        names = [r["name"] for r in self.store.profiled_entities()]
        self.assertEqual(sorted(names), sorted(["用户", "宠物"]))

    def test_profile_block(self):
        import importlib
        import os

        cwd = os.getcwd()
        tmp = tempfile.mkdtemp()
        os.chdir(tmp)
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
            try:
                main = importlib.import_module("astrbot_plugin_memory_brain.main")
            except ModuleNotFoundError:
                self.skipTest("需要安装 AstrBot")
            importlib.reload(main)
            plugin = main.MemoryBrainPlugin(None, {"inject_profile": True})
            plugin.store = self.store

            class E:
                def __init__(self, name, text, group=None):
                    self._n, self._t, self._g = name, text, group
                def get_sender_name(self): return self._n
                def get_sender_id(self): return "10001"
                def get_message_str(self): return self._t
                def get_group_id(self): return self._g

            block = plugin._profile_block(E("用户", "宠物今天又越狱了"))
            self.assertIn("【对他们的印象】", block)
            self.assertIn("用户", block)
            self.assertIn("宠物", block)

            empty = plugin._profile_block(E("路人", "在吗"))
            self.assertEqual(empty, "")

            group_block = plugin._profile_block(E("用户", "在吗", group="123"))
            self.assertIn("群里不要", group_block)
        finally:
            os.chdir(cwd)


class ReflectionTest(unittest.TestCase):
    def test_clean_strips_numbering(self):
        raw = "1. 今天陪用户喝粥\n2、听到她说没胃口有点担心\n第三件：晚上把视频剪完了"
        cleaned = reflection.clean(raw)
        self.assertEqual(len(cleaned.splitlines()), 3)
        self.assertTrue(cleaned.startswith("今天陪用户喝粥"))
        self.assertIn("视频剪完了", cleaned)

    def test_clean_limits_three_lines(self):
        raw = "今天很累\n陪用户喝粥\n剪完了视频\n还想再玩一会"
        self.assertEqual(len(reflection.clean(raw).splitlines()), 3)

    def test_prompt_has_date_and_memories(self):
        p = reflection.build_prompt("2026-10-27", [{"text": "陪用户喝粥", "kind": "episode"}])
        self.assertIn("2026-10-27", p)
        self.assertIn("陪用户喝粥", p)

    def test_memories_between(self):
        store = Store(Path(tempfile.mkdtemp()) / "b.db")
        now = time.time()
        store.insert_memory(text="今天的", created_at=now)
        store.insert_memory(text="前天的", created_at=now - 2 * 86400)
        rows = store.memories_between(now - 3600, now + 60)
        self.assertEqual([r["text"] for r in rows], ["今天的"])
        store.close()


class ForgettingTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(tempfile.mkdtemp()) / "brain.db")
        self.now = time.time()

    def tearDown(self):
        self.store.close()

    def test_accessed_memory_decays_slower(self):
        hot = self.store.insert_memory(text="用户的粥", importance=0.5, created_at=self.now - 86400)
        cold = self.store.insert_memory(text="路人的事", importance=0.5, created_at=self.now - 86400)
        for _ in range(10):
            self.store.touch([hot], now=self.now)
        self.store.decay(0.01, now=self.now)
        hot_imp = self.store.get_memory(hot)["importance"]
        cold_imp = self.store.get_memory(cold)["importance"]
        self.assertGreater(hot_imp, cold_imp)
        self.assertAlmostEqual(cold_imp, 0.495, places=3)

    def test_archive_protects_recent_access(self):
        old = self.now - 60 * 86400
        keep = self.store.insert_memory(text="常被想起的旧事", importance=0.2, created_at=old)
        drop = self.store.insert_memory(text="没人想起的旧事", importance=0.2, created_at=old)
        self.store.touch([keep], now=self.now - 3 * 86400)
        archived = self.store.archive(0.3, 30, now=self.now, protect_days=30)
        self.assertEqual(archived, 1)
        self.assertEqual(self.store.get_memory(keep)["status"], "active")
        self.assertEqual(self.store.get_memory(drop)["status"], "archived")

    def test_purge_removes_old_archived(self):
        mid = self.store.insert_memory(text="很久以前的", importance=0.1, created_at=self.now - 400 * 86400)
        self.store.archive(0.3, 30, now=self.now, protect_days=0)
        # 模拟“归档已经过了 200 天”
        self.store.conn.execute(
            "UPDATE memories SET archived_at=? WHERE id=?", (self.now - 200 * 86400, mid)
        )
        self.store.conn.commit()
        self.assertEqual(self.store.purge_archived(180, now=self.now), 1)
        self.assertIsNone(self.store.get_memory(mid))

    def test_orphan_entities_removed(self):
        eid = self.store.upsert_entity("临时", "thing")
        self.assertEqual(self.store.drop_orphan_entities(), 1)
        self.assertIsNone(self.store.get_entity(eid))


class AbstractTest(unittest.TestCase):
    def test_parse_array(self):
        raw = '```json\n[{"fact": "用户喜欢甜的", "importance": 0.7}]\n```'
        items = abstract.parse_array(raw)
        self.assertEqual(items[0]["fact"], "用户喜欢甜的")
        self.assertEqual(abstract.parse_array("没有 JSON"), [])

    def test_normalize_clamps_and_dedupes(self):
        items = abstract.normalize([
            {"fact": "用户喜欢甜食", "importance": 9, "entities": [{"name": "用户", "type": "person"}]},
            {"fact": "用户喜欢甜食", "importance": 0.5},
            {"fact": "短"},
        ])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["importance"], 1.0)
        self.assertEqual(items[0]["entities"][0]["name"], "用户")


class StyleTest(unittest.TestCase):
    def test_analyze_basic(self):
        rows = [
            {"text": "好的"}, {"text": "还没吃"}, {"text": "吃吧"},
            {"text": "角色，棒棒的"}, {"text": "在吗？"}, {"text": "哈哈哈"},
        ]
        st = style.analyze(rows)
        self.assertEqual(st["n"], 6)
        self.assertLessEqual(st["median_len"], 5)
        self.assertAlmostEqual(st["no_punct_ratio"], 5 / 6, places=2)
        self.assertIn("吧", st["particles"])

    def test_build_block_requires_enough_samples(self):
        self.assertEqual(style.build_block({"n": 5}), "")
        block = style.build_block({"n": 50, "median_len": 11, "short_ratio": 0.2,
                                  "no_punct_ratio": 0.9, "particles": ["吧", "哦"],
                                  "examples": ["好的", "还没吃"]})
        self.assertIn("【最近的聊天习惯】", block)
        self.assertIn("中位 11 字", block)
        self.assertIn("还没吃", block)

    def test_raw_messages_roundtrip(self):
        store = Store(Path(tempfile.mkdtemp()) / "b.db")
        store.add_raw("s1", "user", "吃吧", "用户")
        store.add_raw("s1", "bot", "好呀", "bot")
        self.assertEqual(len(store.recent_raw("user", 10)), 1)
        self.assertEqual(len(store.recent_raw("bot", 10)), 1)
        for i in range(30):
            store.add_raw("s1", "user", f"消息{i}")
        rows = store.recent_raw("user", limit=10)
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[-1]["text"], "消息29")
        store.close()


class WorkingTest(unittest.TestCase):
    def test_extract_and_done(self):
        items = working.extract("记得明天把开票表交了")
        self.assertTrue(items)
        self.assertTrue(working.is_done("好了，弄完了"))
        self.assertFalse(working.is_done("还没弄完"))
        block = working.build_block([{"text": "明天交表", "kind": "todo"}])
        self.assertIn("还没做完", block)
        self.assertEqual(working.build_block([]), "")

    def test_store_working_lifecycle(self):
        store = Store(Path(tempfile.mkdtemp()) / "w.db")
        store.add_working("s1", "明天交开票表")
        store.add_working("s1", "明天交开票表")
        self.assertEqual(len(store.list_working("s1")), 1)
        self.assertEqual(store.close_working("s1", "明天交开票表"), 1)
        self.assertEqual(len(store.list_working("s1")), 0)
        store.add_working("s1", "待会改表格", now=time.time() - 10 * 86400)
        self.assertGreaterEqual(store.prune_working(), 1)
        self.assertEqual(store.list_working("s1"), [])
        store.close()


class ProceduralTest(unittest.TestCase):
    def test_parse_and_normalize(self):
        raw = ('```json\n{"task": "导出一条竖屏视频", "steps": ["写脚本", "生成分镜", '
               '"合成语音", "导出"], "entities": [{"name": "ffmpeg", "type": "skill"}]}\n```')
        skill = procedural.normalize(procedural.parse_object(raw))
        self.assertEqual(skill["task"], "导出一条竖屏视频")
        self.assertIn("导出", procedural.to_text(skill))
        self.assertIsNone(procedural.normalize({"task": "x", "steps": ["1"]}))
        self.assertIsNone(procedural.normalize(None))


class ConflictTest(unittest.TestCase):
    def test_detects_opposite_polarity(self):
        a = "用户喜欢吃甜的"
        b = "用户讨厌吃甜的"
        self.assertTrue(conflict.is_contradiction(a, b))
        self.assertFalse(conflict.is_contradiction("用户喜欢吃甜的", "用户住在城里"))

    def test_find_requires_shared_entity(self):
        facts = [
            {"id": 1, "text": "用户喜欢吃甜的", "entity_ids": {1}},
            {"id": 2, "text": "用户讨厌吃甜的", "entity_ids": {1}},
            {"id": 3, "text": "用户讨厌吃甜的", "entity_ids": {2}},
        ]
        pairs = conflict.find(facts)
        self.assertEqual([(a, b) for a, b, _ in pairs], [(1, 2)])

    def test_conflict_rows_dedupe(self):
        store = Store(Path(tempfile.mkdtemp()) / "c.db")
        self.assertTrue(store.add_conflict(1, 2, "x"))
        self.assertFalse(store.add_conflict(2, 1, "x"))
        self.assertEqual(len(store.list_conflicts()), 1)
        store.resolve_conflict(1)
        self.assertEqual(store.list_conflicts(), [])
        store.close()


class MultimodalTest(unittest.TestCase):
    def test_clean_caption(self):
        self.assertEqual(multimodal.clean_caption("这张图片显示：用户在喝粥。"), "用户在喝粥")
        self.assertEqual(multimodal.clean_caption("  一桌菜  "), "一桌菜")
        self.assertEqual(multimodal.clean_caption(""), "")

    def test_memory_text(self):
        self.assertIn("用户", multimodal.to_memory_text("桌上有一碗粥", "用户"))
        self.assertIn("对方", multimodal.to_memory_text("桌上有一碗粥"))


class VisibilityTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(tempfile.mkdtemp()) / "v.db")
        self.engine = MemoryEngine(self.store)

    def tearDown(self):
        self.store.close()

    def test_recall_filters_by_visibility(self):
        self.engine.remember("私聊才说的事", importance=0.6, visibility="private")
        self.engine.remember("群里说的事", importance=0.6, visibility="group")
        group_hits = self.engine.recall("说的事", k=5, visibility_allow={"public", "group"})
        private_hits = self.engine.recall("说的事", k=5, visibility_allow={"public", "private"})
        self.assertFalse(any("私聊" in h["text"] for h in group_hits))
        self.assertFalse(any("群里" in h["text"] for h in private_hits))

    def test_insert_visibility_bad_value_defaults_public(self):
        mid = self.store.insert_memory(text="随便", visibility="whatever")
        self.assertEqual(self.store.get_memory(mid)["visibility"], "public")


class MediaTest(unittest.TestCase):
    def test_load_and_text(self):
        import json
        import tempfile as tf

        path = Path(tf.mkdtemp()) / "journal.jsonl"
        rows = [
            {"title": "抹香鲸竖着睡", "url": "https://a/1", "note": "靠吐泡泡控制浮力", "source": "网页"},
            {"title": "抹香鲸竖着睡", "url": "https://a/1", "note": "重复"},
            {"title": "", "url": "https://a/2"},
        ]
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
        items = media.load_items([path])
        self.assertEqual(len(items), 2)
        text = media.to_text(items[0])
        self.assertIn("抹香鲸", text)
        self.assertIn("泡泡", text)
        self.assertEqual(media.item_key(items[0]), "https://a/1")
        self.assertEqual(media.meta_of(items[0])["url"], "https://a/1")

    def test_lessons_parse_and_normalize(self):
        raw = '```json\n[{"lesson": "先做完再完美，别一开始就抠细节", "why": "拖太久"}, {"lesson": "短", "why": "x"}]\n```'
        items = lessons.normalize(lessons.parse_array(raw))
        self.assertEqual(len(items), 1)
        self.assertIn("先做完", lessons.to_text(items[0]))
        self.assertEqual(lessons.parse_array("没有"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
