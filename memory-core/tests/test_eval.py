# -*- coding: utf-8 -*-
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory_core.eval import EvalService
from memory_core.server import MemoryService


class TestEval(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mem = MemoryService(str(Path(self.tmp.name) / "brain.db"), embed=False)
        self.eval = EvalService(self.mem, str(Path(self.tmp.name) / "q.jsonl"))

    def tearDown(self):
        self.mem.store.close()
        self.tmp.cleanup()

    def test_hit(self):
        self.mem.remember({"text": "用户喜欢蓝色的毛绒鲸鱼", "importance": 0.8})
        self.eval.add_query({"query": "用户喜欢什么玩具", "expect_any": ["鲸鱼"]})
        out = self.eval.run()
        self.assertEqual(out["metrics"]["recall@1"], 1.0)
        self.assertEqual(out["results"][0]["rank"], 1)

    def test_negative(self):
        self.eval.add_query({"query": "完全没提过的话题", "expect_any": []})
        out = self.eval.run()
        self.assertEqual(out["metrics"]["negatives"], 1)


if __name__ == "__main__":
    unittest.main()
