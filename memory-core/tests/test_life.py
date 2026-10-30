# -*- coding: utf-8 -*-
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory_core.life import LifeService
from memory_core.server import MemoryService


class TestLife(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "books").mkdir()
        (root / "books" / "demo.txt").write_text("第一段。\n第二段。\n", encoding="utf-8")
        self.mem = MemoryService(str(root / "brain.db"), embed=False)
        cfg = root / "life.json"
        cfg.write_text(json.dumps({"books_dir": str(root / "books")}), encoding="utf-8")
        self.life = LifeService(self.mem, str(cfg))

    def tearDown(self):
        self.mem.store.close()
        self.tmp.cleanup()

    def test_book_and_event(self):
        out = self.life.step("book")
        self.assertEqual(out["action"], "book")
        self.assertIn("demo", out["text"])
        self.assertIsNotNone(out["event_id"])
        events = self.life.events(0)
        self.assertEqual(len(events), 1)
        self.assertTrue(self.life.ack(events[0]["id"]))
        self.assertEqual(self.life.events(0)[0]["delivered"], True)

    def test_rest(self):
        out = self.life.step("rest")
        self.assertEqual(out["action"], "rest")
        self.assertGreater(out["state"]["energy"], 0.7)


if __name__ == "__main__":
    unittest.main()
