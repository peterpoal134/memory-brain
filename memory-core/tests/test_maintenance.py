# -*- coding: utf-8 -*-
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory_core.maintenance import MaintenanceService
from memory_core.server import MemoryService


class TestMaintenance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mem = MemoryService(str(Path(self.tmp.name) / "brain.db"), embed=False)
        self.m = MaintenanceService(self.mem)

    def tearDown(self):
        self.mem.store.close()
        self.tmp.cleanup()

    def test_conflict(self):
        self.mem.remember({"text": "用户喜欢毛绒鲸鱼", "kind": "fact", "entities": [("用户", "person")]})
        self.mem.remember({"text": "用户讨厌毛绒鲸鱼", "kind": "fact", "entities": [("用户", "person")]})
        out = self.m.run()
        self.assertGreaterEqual(out["conflicts"], 1)
        cs = self.m.conflicts()
        self.assertTrue(cs)
        self.assertTrue(self.m.resolve(cs[0]["id"], "a"))

    def test_entity_merge(self):
        self.mem.remember({"text": "用户喜欢蓝色", "entities": [("用户", "person")]})
        self.mem.remember({"text": "用户今天出门", "entities": [("用户 ", "thing")]})
        self.m.run()
        n = self.mem.store.conn.execute("SELECT COUNT(*) FROM entities WHERE name='用户'").fetchone()[0]
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
