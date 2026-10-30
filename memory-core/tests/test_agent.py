# -*- coding: utf-8 -*-
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory_core.agent import AgentService
from memory_core.server import MemoryService


class TestAgent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mem = MemoryService(str(Path(self.tmp.name) / "brain.db"), embed=False)
        self.agent = AgentService(self.mem)

    def tearDown(self):
        self.mem.store.close()
        self.tmp.cleanup()

    def test_emotion_and_affinity(self):
        old = self.mem.chat
        seen = {}

        def fake(data):
            seen["system"] = data["messages"][0]["content"]
            return {"choices": [{"message": {"content": "好呀"}}]}

        self.mem.chat = fake
        try:
            out = self.agent.chat({"message": "今天好开心", "user_id": "u1"})
        finally:
            self.mem.chat = old
        self.assertIn("开心", out["state"]["label"])
        self.assertGreater(out["state"]["affinity"], 50)
        self.assertIn("主要情绪：开心", seen["system"])

    def test_style_limit(self):
        old = self.mem.chat
        self.mem.chat = lambda data: {"choices": [{"message": {"content": "第一行\n第二行\n第三行\n第四行"}}]}
        try:
            out = self.agent.chat({"message": "在吗", "user_id": "u1"})
        finally:
            self.mem.chat = old
        self.assertLessEqual(len(out["reply"].split("\n")), 3)


if __name__ == "__main__":
    unittest.main()
