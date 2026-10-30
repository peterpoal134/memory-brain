# -*- coding: utf-8 -*-
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory_core.server import MemoryService


class TestBatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mem = MemoryService(str(Path(self.tmp.name) / "brain.db"), embed=False)

    def tearDown(self):
        self.mem.store.close()
        self.tmp.cleanup()

    def test_batch(self):
        for i in range(11):
            self.mem.remember_turn("u1", "private", "user", f"消息{i}")
        self.assertEqual(self.mem.store.stats()["active"], 0)
        self.mem.remember_turn("u1", "private", "assistant", "第十二条")
        rows = [m for m in self.mem.store.list_memories(limit=10) if m.get("source") == "chat_batch"]
        self.assertEqual(len(rows), 1)
        self.assertIn("消息", rows[0]["text"])

    def test_user_scoped(self):
        for i in range(6):
            self.mem.remember_turn("u1", "private", "user", f"a{i}")
            self.mem.remember_turn("u2", "private", "user", f"b{i}")
        self.assertEqual(self.mem.store.stats()["active"], 0)


if __name__ == "__main__":
    unittest.main()
