# -*- coding: utf-8 -*-
"""多模态记忆：图片 → 一句话描述 → 存成记忆。

只处理「对方发来的图」，上限受配置控制；语音/视频先不做。
"""

from __future__ import annotations

import os

import base64
import re
import subprocess
import tempfile
from pathlib import Path

FFMPEG = os.environ.get("ASTRBOT_FFMPEG", "ffmpeg")
VIDEO_PROMPT = (
    "这是同一段视频的几帧。用 40–80 字概括：画面里在做什么、讲的是什么主题或观点。"
    "只写你看到的，不要评价。"
)
CAPTION_PROMPT = (
    "用一句简体中文描述这张图片，20–40 字，只写你看到的事实："
    "谁、在做什么、画面里有什么文字或物件。不要评价、不要猜测情绪。"
)
CAPTION_SYSTEM = "你是一个只描述画面内容的视觉助手。只输出一句话。"


def save_media(comp) -> Path | None:
    """把 Video/Image 落成本地文件（file:// / http / base64 / 本地路径）。"""
    import urllib.request

    raw = str(getattr(comp, "path", "") or getattr(comp, "file", "") or getattr(comp, "url", "") or "").strip()
    if not raw:
        return None
    try:
        if raw.startswith("file://"):
            p = Path(raw[7:])
            return p if p.exists() else None
        if raw.startswith(("http://", "https://")):
            req = urllib.request.Request(raw, headers={"User-Agent": "astrbot"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            p = Path(tempfile.mkdtemp()) / "media.bin"
            p.write_bytes(data)
            return p
        if raw.startswith("base64://"):
            p = Path(tempfile.mkdtemp()) / "media.bin"
            p.write_bytes(base64.b64decode(raw[9:]))
            return p
        p = Path(raw)
        return p if p.exists() else None
    except Exception:
        return None


def image_ref(comp) -> str:
    """从 Image 组件里取一个模型能吃的引用：http(s) 或 data URL。"""
    url = str(getattr(comp, "url", "") or "").strip()
    if url.startswith(("http://", "https://", "data:")):
        return url
    file = str(getattr(comp, "file", "") or getattr(comp, "path", "") or "").strip()
    if file and Path(file).exists():
        data = Path(file).read_bytes()
        return "data:image/jpeg;base64," + base64.b64encode(data).decode()
    return url or file


def clean_caption(text: str, limit: int = 80) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    t = re.sub(r"^(这张|这幅|图片|图中)[^，。]{0,6}(是|显示|展示)[，:：]?", "", t)
    t = t.strip(" 　，,。.；;：:")
    return t[:limit]


def video_frames(path: Path, count: int = 3) -> list[Path]:
    """从视频里抽几帧（按时间百分比），失败就从 0.5 秒抽一帧。"""
    out_dir = Path(tempfile.mkdtemp())
    frames: list[Path] = []
    for i in range(count):
        pct = 0.2 + 0.6 * (i / max(1, count - 1))
        out = out_dir / f"f{i}.jpg"
        for ts in (pct, 0.5):
            try:
                subprocess.run(
                    [FFMPEG, "-y", "-loglevel", "error", "-ss", f"{ts:.2f}",
                     "-i", str(path), "-frames:v", "1", "-vf", "scale=640:-2", str(out)],
                    check=True, capture_output=True, timeout=30,
                )
            except Exception:
                continue
            if out.exists() and out.stat().st_size > 0:
                frames.append(out)
                break
    return frames


def video_audio(path: Path, seconds: int = 60) -> Path | None:
    out = Path(tempfile.mkdtemp()) / "video.wav"
    try:
        subprocess.run(
            [FFMPEG, "-y", "-loglevel", "error", "-i", str(path),
             "-t", str(seconds), "-ar", "16000", "-ac", "1", str(out)],
            check=True, capture_output=True, timeout=120,
        )
        return out if out.exists() and out.stat().st_size > 1000 else None
    except Exception:
        return None


def to_memory_text(caption: str, sender: str = "") -> str:
    who = f"{sender}发的图" if sender else "对方发的图"
    return f"{who}：{caption}"
