# -*- coding: utf-8 -*-
"""通过 NapCat 把文件发到指定私聊（跨会话）。"""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

NAPCAT = os.environ.get("NAPCAT_API", "http://127.0.0.1:3000")
TOKEN = os.environ.get("NAPCAT_TOKEN", "")


def targets(raw: str) -> dict[str, int]:
    out = {}
    for part in str(raw or "").replace("，", ",").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            try:
                out[k.strip()] = int(v.strip())
            except ValueError:
                pass
    return out


def send_private(user_id: int, path: str, kind: str = "video") -> bool:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(str(p))
    typ = "record" if kind == "voice" else "video"
    payload = {
        "user_id": int(user_id),
        "message": [{"type": typ, "data": {"file": p.resolve().as_uri()}}],
    }
    req = urllib.request.Request(
        NAPCAT.rstrip("/") + "/send_private_msg",
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.loads(r.read().decode())
    return str(out.get("status")) == "ok"
