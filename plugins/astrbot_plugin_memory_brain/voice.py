# -*- coding: utf-8 -*-
"""语音转文字：用本地 faster-whisper，把收到的语音变成可记的文字。

不依赖 AstrBot 的 STT 提供方（本机没配），模型第一次用时才下载。
"""

from __future__ import annotations

import asyncio
import base64
import re
import subprocess
import tempfile
import urllib.request
from pathlib import Path

MODEL_DIR = str(Path(__file__).resolve().parents[2] / "models" / "whisper")
AUDIO_EXT = (".mp3", ".m4a", ".wav", ".amr", ".silk", ".flac", ".ogg", ".aac", ".opus")
# 给 whisper 一点人名/常用词偏置，短语音里“用户”不会被听成“洪到”
DEFAULT_PROMPT = "这是角色和用户的聊天。常提到的名字：用户、角色、宠物、朋友、管理员、粥。"
_model = None
_model_size = ""


def _patch_av() -> None:
    """PyAV 19 去掉了 metadata_errors 参数，而 faster-whisper 还在传；这里兼容一下。"""
    try:
        import av
        import faster_whisper.audio as fwa

        if getattr(fwa, "_patched_av", False):
            return
        original = av.open

        def open_compat(*args, **kwargs):
            kwargs.pop("metadata_errors", None)
            return original(*args, **kwargs)

        fwa.av.open = open_compat
        fwa._patched_av = True
    except Exception:
        pass


def _get_model(size: str):
    global _model, _model_size
    if _model is None or _model_size != size:
        from faster_whisper import WhisperModel

        _patch_av()
        Path(MODEL_DIR).mkdir(parents=True, exist_ok=True)
        _model = WhisperModel(size, device="cpu", compute_type="int8", download_root=MODEL_DIR)
        _model_size = size
    return _model


def _to_wav(src: Path) -> Path | None:
    out = Path(tempfile.mkdtemp()) / "voice.wav"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
             "-ar", "16000", "-ac", "1", str(out)],
            check=True, capture_output=True, timeout=60,
        )
        return out if out.exists() else None
    except Exception:
        return None


async def save_audio(comp) -> Path | None:
    """把 Record 组件落成本地文件：支持 file://、http(s)、base64、本地路径。"""
    raw = str(
        getattr(comp, "path", "")
        or getattr(comp, "file_", "")
        or getattr(comp, "file", "")
        or getattr(comp, "url", "")
        or ""
    ).strip()
    if not raw:
        return None
    try:
        if raw.startswith("file://"):
            path = Path(raw[7:])
            return path if path.exists() else None
        if raw.startswith(("http://", "https://")):
            data = await asyncio.to_thread(_download, raw)
            if not data:
                return None
            path = Path(tempfile.mkdtemp()) / "voice.bin"
            path.write_bytes(data)
            return path
        if raw.startswith("base64://"):
            data = base64.b64decode(raw[9:])
            path = Path(tempfile.mkdtemp()) / "voice.bin"
            path.write_bytes(data)
            return path
        path = Path(raw)
        return path if path.exists() else None
    except Exception:
        return None


def _download(url: str) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "astrbot"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception:
        return None


def transcribe_sync(path: Path, size: str = "small") -> str:
    model = _get_model(size)
    segments, _info = model.transcribe(
        str(path), language="zh", vad_filter=True, beam_size=1,
        initial_prompt=DEFAULT_PROMPT,
    )
    return clean("".join(s.text for s in segments))


def is_audio_name(name: str) -> bool:
    return str(name or "").lower().split("?")[0].endswith(AUDIO_EXT)


def clean(text: str, limit: int = 200) -> str:
    t = re.sub(r"\s+", "", str(text or "")).strip()
    t = t.replace(",", "，").replace("?", "？").replace("!", "！").replace(".", "。")
    t = re.sub(r"[，。！？、；：]{2,}", "，", t)
    return t[:limit]


def _silk_to_wav(src: Path) -> Path | None:
    """QQ 语音常见是 silk；PyAV 解不了时用 pilk 转 wav。"""
    try:
        import pilk

        out = Path(tempfile.mkdtemp()) / "voice.wav"
        pilk.silk_to_wav(str(src), str(out), rate=24000)
        return out if out.exists() else None
    except Exception:
        return None


async def transcribe(path: Path, size: str = "small") -> str:
    # 1) PyAV 直接解；2) ffmpeg 转 wav；3) silk 解码
    try:
        return await asyncio.to_thread(transcribe_sync, path, size)
    except Exception:
        pass
    wav = await asyncio.to_thread(_to_wav, path)
    if wav is not None:
        try:
            return await asyncio.to_thread(transcribe_sync, wav, size)
        except Exception:
            pass
    silk = await asyncio.to_thread(_silk_to_wav, path)
    if silk is not None:
        return await asyncio.to_thread(transcribe_sync, silk, size)
    return ""


def to_memory_text(sender: str, text: str) -> str:
    who = f"{sender}发来一条语音" if sender else "对方发来一条语音"
    return f"{who}：{text}"[:200]


def build_block(text: str) -> str:
    return f"【对方这条语音说的是】{text[:160]}"
