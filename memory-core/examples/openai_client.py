# -*- coding: utf-8 -*-
"""最小客户端：带 user 调 memory-core 的 OpenAI 入口。"""
import json
import os
import urllib.request

BASE = os.environ.get("MEMORY_BASE", "http://127.0.0.1:8780/v1")
TOKEN = os.environ.get("MEMORY_TOKEN", "")
USER = os.environ.get("MEMORY_USER", "demo:1")

body = {
    "user": USER,
    "model": "memory-core",
    "messages": [{"role": "user", "content": "你好，还记得我吗？"}],
}
req = urllib.request.Request(
    BASE.rstrip("/") + "/chat/completions",
    data=json.dumps(body, ensure_ascii=False).encode(),
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=60) as r:
    data = json.loads(r.read().decode())
print(data["choices"][0]["message"]["content"])
