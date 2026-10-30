# -*- coding: utf-8 -*-
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory_core.server import MemoryService


class TestCore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = MemoryService(str(Path(self.tmp.name) / "brain.db"), embed=False)

    def tearDown(self):
        self.svc.store.close()
        self.tmp.cleanup()

    def test_remember_recall(self):
        self.svc.remember({"text": "用户喜欢蓝灰色的毛绒鲸鱼", "kind": "fact", "importance": 0.8, "visibility": "public"})
        r = self.svc.recall({"query": "鲸鱼", "k": 3})
        self.assertTrue(r["hits"])
        self.assertIn("鲸鱼", r["block"])

    def test_visibility(self):
        self.svc.remember({"text": "私聊里说的秘密暗号是青柠", "visibility": "private"})
        pub = self.svc.recall({"query": "秘密暗号", "k": 3, "visibility": ["public"]})
        priv = self.svc.recall({"query": "秘密暗号", "k": 3, "visibility": ["private"]})
        self.assertEqual(pub["hits"], [])
        self.assertTrue(priv["hits"])

    def test_stats_and_decay(self):
        self.svc.remember({"text": "一条普通记忆", "importance": 0.6})
        self.assertGreaterEqual(self.svc.stats()["active"], 1)
        n = self.svc.engine.decay(0.01)
        self.assertGreaterEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
