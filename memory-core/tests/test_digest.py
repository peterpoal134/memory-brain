# -*- coding: utf-8 -*-
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory_core.digest import DigestService
from memory_core.server import MemoryService


class TestDigest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mem = MemoryService(str(Path(self.tmp.name) / "brain.db"), embed=False)
        self.d = DigestService(self.mem)

    def tearDown(self):
        self.mem.store.close()
        self.tmp.cleanup()

    def test_extract(self):
        self.mem.complete = lambda *a, **k: '{"summary":"用户喜欢毛绒鲸鱼","kind":"fact","importance":0.8,"entities":[{"name":"毛绒鲸鱼","type":"thing"}],"relations":[]}'
        out = self.d.extract("用户说喜欢毛绒鲸鱼")
        self.assertEqual(out["kind"], "fact")
        self.assertIn("鲸鱼", out["summary"])

    def test_profile(self):
        self.mem.remember({"text": "用户喜欢毛绒鲸鱼", "importance": 0.8})
        self.mem.complete = lambda *a, **k: "用户喜欢毛绒玩具，最近常提鲸鱼。"
        out = self.d.profile("用户")
        self.assertIn("鲸鱼", out["text"])


if __name__ == "__main__":
    unittest.main()
