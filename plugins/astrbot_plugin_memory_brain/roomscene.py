# -*- coding: utf-8 -*-
"""把助手的透明人物图合到房间背景里。"""
from __future__ import annotations

from pathlib import Path


def compose(character_png: str, room_jpg: str, out_path: str = "/tmp/xiaoba-room.jpg") -> Path:
    from PIL import Image

    char = Image.open(character_png).convert("RGBA")
    room = Image.open(room_jpg).convert("RGBA")
    h = min(800, room.height - 40)
    char = char.resize((int(char.width * h / char.height), h), Image.LANCZOS)
    x = (room.width - char.width) // 2
    y = room.height - h - 10
    room.alpha_composite(char, (x, y))
    # 1024 宽，QQ 发送更轻
    if room.width > 1024:
        nh = int(room.height * 1024 / room.width)
        room = room.resize((1024, nh), Image.LANCZOS)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    room.convert("RGB").save(out, quality=92)
    return out
