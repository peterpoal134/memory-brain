#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""角色 Life Loop v0：状态驱动的生活循环，不依赖 AstrBot 的消息触发。"""
from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup

BASE = Path(__file__).resolve().parent
STATE = BASE / "state.json"
GOALS = BASE / "goals.json"
SOUL = BASE / "soul.md"
DIARY = BASE / "diary.jsonl"
ACTIONS = BASE / "actions.jsonl"
RATE = BASE / "rate.json"
IDEAS = BASE / "content_ideas.jsonl"
READING = BASE / "reading_list.json"
WATCHED = BASE / "watched.jsonl"
SHOTS = BASE / "watched"
NOTES = BASE / "notes.jsonl"
INTERESTS = BASE / "interests.json"
MOOD = Path(os.environ["LIFE_MOOD_FILE"]) if os.environ.get("LIFE_MOOD_FILE") else None
SHARE_TO = os.environ.get("LIFE_SHARE_TO", "")
USAGE = BASE / "usage.jsonl"
ADB = os.environ.get("LIFE_ADB", "adb")
SCRCPY = os.environ.get("LIFE_SCRCPY", "scrcpy")
FFMPEG = os.environ.get("LIFE_FFMPEG", "ffmpeg")
WHISPER_DIR = Path(os.environ.get("LIFE_WHISPER_DIR", str(BASE / "whisper")))
_whisper = None
PHONE = os.environ.get("LIFE_PHONE", "")
DOUYIN = "com.ss.android.ugc.aweme"

LLM_URL = os.environ.get("LIFE_LLM_URL", "http://127.0.0.1:8080/v1/chat/completions")
FALLBACK_URL = os.environ.get("LIFE_FALLBACK_URL", "https://api.deepseek.com/v1/chat/completions")
FALLBACK_MODEL = os.environ.get("LIFE_FALLBACK_MODEL", "deepseek-flash")
ASTRBOT_CONFIG = Path(os.environ["LIFE_ASTRBOT_CONFIG"]) if os.environ.get("LIFE_ASTRBOT_CONFIG") else None
NAPCAT = os.environ.get("LIFE_NAPCAT", "http://127.0.0.1:3000")
NAPCAT_TOKEN = os.environ.get("LIFE_NAPCAT_TOKEN", "")
GROUP_ID = int(os.environ.get("LIFE_GROUP_ID", "0") or 0)
MAIN_ID = int(os.environ.get("LIFE_MAIN_ID", "0") or 0)
PHONE_DIR = BASE / "phone"
BOOKS = BASE / "books"
BOOK_PROGRESS = BASE / "book_progress.json"
QUIET = range(2, 8)


DEFAULT_STATE = {"energy": 0.7, "boredom": 0.5, "curiosity": 0.5,
                 "social": 0.4, "rest": 0.3, "mood": "平常"}
DEFAULT_SOUL = ("你是一个有自己的房间、自己的目标和节奏的角色。"
                "你会自己安排时间，不总是等人来找；不靠刷屏和怼人获得存在感。")
DEFAULT_GOALS = ["把今天想做的事做完一件", "留一点时间给自己"]


def ensure_files() -> None:
    BASE.mkdir(parents=True, exist_ok=True)
    (BASE / "books").mkdir(exist_ok=True)
    (BASE / "watched").mkdir(exist_ok=True)
    if not STATE.exists():
        save_json(STATE, dict(DEFAULT_STATE))
    if not SOUL.exists():
        SOUL.write_text(DEFAULT_SOUL, encoding="utf-8")
    if not GOALS.exists():
        save_json(GOALS, list(DEFAULT_GOALS))
    if not INTERESTS.exists():
        load_interests()

ACTIONS_SET = {"idle", "rest", "reflect", "study", "content", "qzone", "group", "phone", "book", "nap"}


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def clean_message(text: str) -> str:
    """去掉会原样露在 QQ/空间里的 Markdown 记号。"""
    if not text:
        return ""
    cleaned = text.replace("**", "").replace("__", "").replace("`", "")
    cleaned = re.sub(r"^#{1,6}\s*", "", cleaned, flags=re.M)
    return cleaned.strip()


def append_jsonl(path: Path, item: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


def recent_diary(limit: int = 3) -> list[dict]:
    rows = []
    try:
        for line in DIARY.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    except Exception:
        pass
    return rows[-limit:]


_LOCAL_CACHE: dict = {"checked": False, "at": 0.0, "value": None}


def choose_model() -> tuple[str, str] | None:
    """探测本地 llama.cpp / Strata，返回 (model, key)；不可用返回 None。

    结果缓存 5 分钟：通道挂着但后端已停的时候，每次调用都探测会白等十几秒。
    """
    now = time.time()
    if _LOCAL_CACHE["checked"] and now - _LOCAL_CACHE["at"] < 300:
        return _LOCAL_CACHE["value"]
    found = None
    for key in ("llama-local", "strata-local", "sk-local"):
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:8080/v1/models",
                headers={"Authorization": f"Bearer {key}"},
            )
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read().decode())
            models = data.get("data") or data.get("models") or []
            if models:
                model = models[0].get("id") or models[0].get("model")
                if model:
                    found = (model, key)
                    break
        except Exception:
            continue
    _LOCAL_CACHE.update(checked=True, at=now, value=found)
    return found


_FALLBACK_KEY: dict = {"key": None, "at": 0.0}


def fallback_key() -> str | None:
    """AstrBot provider 里的 DeepSeek key，5 分钟缓存；只读，不打印。"""
    now = time.time()
    if _FALLBACK_KEY["key"] and now - _FALLBACK_KEY["at"] < 300:
        return _FALLBACK_KEY["key"]
    try:
        data = json.loads(ASTRBOT_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return None
    key = None
    for source in data.get("provider_sources", []) or []:
        if not isinstance(source, dict):
            continue
        if "api.deepseek.com" not in str(source.get("api_base") or ""):
            continue
        raw = source.get("key")
        if isinstance(raw, list) and raw:
            key = str(raw[0]).strip()
        elif isinstance(raw, str) and raw.strip():
            key = raw.strip()
        if key:
            break
    if key:
        _FALLBACK_KEY.update(key=key, at=now)
    return key


def _post_llm(url: str, model: str, key: str, system: str, user: str,
              max_tokens: int, thinking: bool | None, reasoning: str | None = None) -> str:
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "temperature": 0.8,
    }
    if thinking is not None:
        payload["chat_template_kwargs"] = {"enable_thinking": thinking}
    if reasoning is not None:
        # DeepSeek 的 flash 是推理模型：短输出必须显式关掉/压低推理，
        # 否则 token 全花在 reasoning_content，content 是空的。
        payload["reasoning_effort"] = reasoning
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode())
    log_usage("vision_caption", data.get("usage"))
    return (data["choices"][0]["message"].get("content") or "").strip()


def call_llm(system: str, user: str, max_tokens: int = 600, reasoning: str = "low") -> str:
    """优先本地模型；本地不可用就退到 AstrBot 的 DeepSeek 在线 key。

    reasoning: 在线时传给 DeepSeek 的 reasoning_effort（"none"/"low"/...）。
    短输出（主题、点子）用 "none"，叙事/判断用 "low"，避免 content 被推理吃光。
    """
    local = choose_model()
    if local:
        try:
            return _post_llm(LLM_URL, local[0], local[1], system, user, max_tokens, False)
        except Exception as exc:
            print(f"[life] local model failed, falling back online: {exc}", flush=True)
    key = fallback_key()
    if not key:
        raise RuntimeError("本地模型不可用，AstrBot 里也没有 DeepSeek key")
    budget = max(max_tokens, 200 if reasoning == "none" else 800)
    text = _post_llm(FALLBACK_URL, FALLBACK_MODEL, key, system, user, budget, None, reasoning)
    if not text.strip():
        raise RuntimeError("在线模型返回了空 content（可能推理占满 max_tokens）")
    return text


def wiki_read(topic: str) -> tuple[str, str]:
    """按主题找中文维基条目，返回 (url, 正文)。"""
    q = urllib.parse.quote(topic[:60])
    search = f"https://zh.wikipedia.org/w/api.php?action=query&list=search&srsearch={q}&format=json&srlimit=1"
    req = urllib.request.Request(search, headers={"User-Agent": "xiaoba-life/0.1"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode())
    hits = data.get("query", {}).get("search", [])
    if not hits:
        raise RuntimeError("没有找到条目")
    title = hits[0]["title"]
    url = "https://zh.wikipedia.org/wiki/" + urllib.parse.quote(title)
    headers = {"User-Agent": "xiaoba-life/0.1 (personal assistant; contact: owner@localhost)"}
    # 优先 REST summary；被限流（403 Too Many Reqs）时退到 action=query 的 extracts。
    try:
        api = "https://zh.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(title)
        req = urllib.request.Request(api, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            page = json.loads(resp.read().decode())
        text = page.get("extract") or page.get("description") or ""
    except Exception as exc:
        print(f"[life] wiki summary failed ({exc}), trying extracts API", flush=True)
        api = (
            "https://zh.wikipedia.org/w/api.php?action=query&prop=extracts"
            "&explaintext=1&exintro=1&format=json&titles=" + urllib.parse.quote(title)
        )
        req = urllib.request.Request(api, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            page = json.loads(resp.read().decode())
        pages = (page.get("query") or {}).get("pages") or {}
        text = ""
        for value in pages.values():
            text = value.get("extract") or ""
            if text:
                break
    if not text:
        raise RuntimeError("条目没有正文")
    return url, text[:5000]


def read_url(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"},
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        raw = resp.read(1_500_000)
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
        tag.decompose()
    return "\n".join(
        line.strip() for line in soup.get_text("\n").splitlines() if line.strip()
    )[:5000]


def adb(*args, timeout: int = 25):
    """调 adb；失败返回 None，不抛异常。"""
    if not PHONE:
        return None
    try:
        return subprocess.run(
            [str(ADB), "-s", PHONE, *args],
            capture_output=True, timeout=timeout,
        )
    except Exception as exc:
        print(f"[life] adb failed: {exc}", flush=True)
        return None


def phone_online() -> bool:
    proc = adb("devices", timeout=8)
    return bool(proc and PHONE.encode() in (proc.stdout or b""))


def phone_focus() -> str:
    proc = adb("shell", "dumpsys", "window", timeout=15)
    text = (proc.stdout or b"").decode(errors="ignore") if proc else ""
    for line in text.splitlines():
        if "mCurrentFocus" in line:
            return line.strip()
    return ""


def save_shot(png_bytes: bytes) -> Path:
    SHOTS.mkdir(exist_ok=True)
    png = SHOTS / f"{datetime.now():%Y%m%d-%H%M%S}.png"
    png.write_bytes(png_bytes)
    jpg = png.with_suffix(".jpg")
    try:
        from PIL import Image
        img = Image.open(png).convert("RGB")
        img.thumbnail((720, 1600))
        img.save(jpg, "JPEG", quality=72)
        png.unlink(missing_ok=True)
        return jpg
    except Exception:
        return png


def vision_caption(path: Path) -> str:
    """让在线模型看一张截图，说清楚这条在讲什么。"""
    key = fallback_key()
    if not key:
        raise RuntimeError("没有可用的在线 key")
    b64 = base64.b64encode(path.read_bytes()).decode()
    payload = {
        "model": FALLBACK_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": (
                    "这是一张抖音截图。用两三句说清楚：这条在讲什么？画面里最抓眼的是什么？"
                    "作者名和点赞数能看清就写出来。只说你看到的，不要评价、不要建议。"
                )},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
            ],
        }],
        "max_tokens": 600,
        "reasoning_effort": "none",
    }
    req = urllib.request.Request(
        FALLBACK_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode())
    log_usage("vision_caption", data.get("usage"))
    return (data["choices"][0]["message"].get("content") or "").strip()




def record_clip(seconds: int = 15) -> Path | None:
    """用 scrcpy 无窗口录一段（带手机内录），比截图更像“看了一段”。"""
    out = SHOTS / f"clip_{datetime.now():%Y%m%d-%H%M%S}.mp4"
    env = dict(os.environ, ADB=str(ADB))
    try:
        subprocess.run(
            [str(SCRCPY), "--no-window", "--no-playback", f"--record={out}",
             f"--time-limit={seconds}", "--audio-source=playback",
             "--video-bit-rate=2M", "--max-size=1280"],
            cwd=str(SCRCPY.parent), env=env,
            capture_output=True, timeout=seconds + 45,
        )
    except Exception as exc:
        print(f"[life] record failed: {exc}", flush=True)
        return None
    if out.exists() and out.stat().st_size > 10_000:
        return out
    return None


def clip_frames(clip: Path, count: int = 4) -> list[Path]:
    frames = []
    for i in range(count):
        pct = 0.2 + 0.6 * (i / max(1, count - 1))
        out = SHOTS / f"{clip.stem}_f{i}.jpg"
        try:
            subprocess.run(
                [str(FFMPEG), "-y", "-loglevel", "error", "-ss", f"{pct:.2f}",
                 "-i", str(clip), "-frames:v", "1", "-vf", "scale=720:-2", str(out)],
                check=True, capture_output=True, timeout=30,
            )
        except Exception:
            continue
        if out.exists() and out.stat().st_size > 0:
            frames.append(out)
    return frames


def clip_audio(clip: Path) -> Path | None:
    out = SHOTS / f"{clip.stem}.wav"
    try:
        subprocess.run(
            [str(FFMPEG), "-y", "-loglevel", "error", "-i", str(clip),
             "-ar", "16000", "-ac", "1", str(out)],
            check=True, capture_output=True, timeout=90,
        )
        return out if out.exists() and out.stat().st_size > 1000 else None
    except Exception:
        return None


def transcribe_audio(wav: Path) -> str:
    global _whisper
    try:
        import av
        import faster_whisper.audio as fwa
        if not getattr(fwa, "_patched_av", False):
            original = av.open

            def open_compat(*args, **kwargs):
                kwargs.pop("metadata_errors", None)
                return original(*args, **kwargs)

            fwa.av.open = open_compat
            fwa._patched_av = True
        from faster_whisper import WhisperModel

        if _whisper is None:
            WHISPER_DIR.mkdir(parents=True, exist_ok=True)
            _whisper = WhisperModel("small", device="cpu", compute_type="int8",
                                    download_root=str(WHISPER_DIR))
        segs, _ = _whisper.transcribe(
            str(wav), language="zh", vad_filter=True, beam_size=1,
            initial_prompt="这是角色和用户会看的视频。常提到：用户、角色、白菜、朋友。",
        )
        return re.sub(r"\s+", "", "".join(s.text for s in segs))[:300]
    except Exception as exc:
        print(f"[life] transcribe failed: {exc}", flush=True)
        return ""


def vision_clip(frames: list[Path], transcript: str) -> str:
    key = fallback_key()
    if not key or not frames:
        return ""
    parts = [{"type": "text", "text": (
        "这是同一段视频的几帧" + ("，以及它的语音转写：" + transcript if transcript else "") +
        "。用两三句话说清楚：这条在讲什么、观点或结论是什么、画面里最抓眼的是什么。"
        "只说你看到的，不要评价、不要建议。"
    )}]
    for f in frames[:4]:
        try:
            b64 = base64.b64encode(f.read_bytes()).decode()
        except OSError:
            continue
        parts.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}})
    payload = {"model": FALLBACK_MODEL,
               "messages": [{"role": "user", "content": parts}],
               "max_tokens": 700, "reasoning_effort": "none"}
    try:
        req = urllib.request.Request(
            FALLBACK_URL, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
        log_usage("vision_clip", data.get("usage"))
        return (data["choices"][0]["message"].get("content") or "").strip()
    except Exception as exc:
        print(f"[life] vision clip failed: {exc}", flush=True)
        return ""


def watch_douyin(limit: int = 2) -> dict | None:
    """刷几条抖音：截图 + 让模型看懂 + 记下来。手机不在/在忙就返回 None。"""
    if not phone_online():
        print("[life] phone offline, skip watch", flush=True)
        return None
    focus = phone_focus()
    if focus and not any(k in focus for k in ("aweme", "launcher", "Launcher", "splash")):
        print(f"[life] phone busy ({focus}), skip watch", flush=True)
        return None
    adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
    adb("shell", "am", "force-stop", DOUYIN)
    time.sleep(1)
    adb("shell", "monkey", "-p", DOUYIN, "-c", "android.intent.category.LAUNCHER", "1")
    time.sleep(8)
    items = []
    for _ in range(limit):
        clip = record_clip(15)
        desc, shot = "", ""
        if clip is not None:
            frames = clip_frames(clip)
            wav = clip_audio(clip)
            transcript = transcribe_audio(wav) if wav else ""
            desc = vision_clip(frames, transcript)
            shot = str(frames[0]) if frames else ""
            if not desc:
                try:
                    desc = vision_caption(frames[0] if frames else clip)
                except Exception:
                    desc = ""
            if transcript and transcript not in desc:
                desc = (desc + " 语音： " + transcript).strip()
        if not desc:
            proc = adb("exec-out", "screencap", "-p", timeout=30)
            if proc and proc.stdout:
                shot = str(save_shot(proc.stdout))
                try:
                    desc = vision_caption(Path(shot))
                except Exception as exc:
                    print(f"[life] vision failed: {exc}", flush=True)
                    desc = ""
        items.append({"desc": desc, "shot": shot, "clip": str(clip or "")})
        adb("shell", "input", "swipe", "540", "1850", "540", "450", "300")
        time.sleep(5)
    adb("shell", "input", "keyevent", "KEYCODE_HOME")
    if not items:
        return None
    notes = "\n".join("- " + (item["desc"] or "（没看清）") for item in items)
    try:
        note = clean_message(call_llm(
            "你是角色。你刚刷了几条抖音，用一两句像自己随手记的写下你注意到的东西；"
            "不要分析、不要总结成方法、不要客服腔。",
            notes,
            max_tokens=300,
            reasoning="none",
        ))
    except Exception as exc:
        print(f"[life] watch note failed: {exc}", flush=True)
        note = ""
    # 只留最近 40 段录像，避免磁盘越攒越大
    try:
        clips = sorted(SHOTS.glob("clip_*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in clips[20:]:
            old.unlink(missing_ok=True)
    except Exception:
        pass
    record = {
        "ts": time.time(),
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "items": items,
        "note": note,
    }
    append_jsonl(WATCHED, record)
    try:
        descs = "；".join((it.get("desc") or "")[:120] for it in items)
        write_note("watch", "刷到：" + descs, "temp")
    except Exception:
        pass
    return record


def log_usage(kind: str, usage: dict | None) -> None:
    if not usage:
        return
    append_jsonl(USAGE, {
        "ts": time.time(),
        "kind": kind,
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    })


def write_note(kind: str, text: str, keep: str = "temp", source: str = "") -> None:
    """把这一轮做的事记一条笔记：keep=temp 一天后忘，long 进长期记忆。"""
    text = clean_message(text or "")
    if len(text) < 4:
        return
    if any(w in text for w in ("要记住", "记住这", "以后都", "长期", "别忘了")):
        keep = "long"
    append_jsonl(NOTES, {
        "ts": time.time(),
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "kind": kind,
        "keep": keep,
        "text": text[:600],
        "source": source,
    })




DEFAULT_INTERESTS = [
    {"name": "AI 和记忆系统", "kind": "topic", "weight": 0.85, "tags": ["烧脑", "独处"]},
    {"name": "摄影和视频剪辑", "kind": "topic", "weight": 0.60, "tags": ["创造", "开心"]},
    {"name": "上海散步和咖啡", "kind": "activity", "weight": 0.50, "tags": ["放松"]},
    {"name": "心理学和人的行为", "kind": "topic", "weight": 0.45, "tags": ["烧脑"]},
    {"name": "好吃的", "kind": "topic", "weight": 0.30, "tags": ["放松", "开心"]},
]


def load_interests() -> list[dict]:
    data = load_json(INTERESTS, None)
    if not isinstance(data, list) or not data:
        data = [dict(x, last_at=0) for x in DEFAULT_INTERESTS]
        save_json(INTERESTS, data)
    return data


def save_interests(items: list[dict]) -> None:
    save_json(INTERESTS, items)


def bump_interest(name: str, delta: float = 0.05) -> None:
    if not name:
        return
    items = load_interests()
    for it in items:
        if it.get("name") == name:
            it["weight"] = min(1.0, float(it.get("weight", 0.3)) + delta)
            it["last_at"] = time.time()
            it["count"] = int(it.get("count", 0)) + 1
            break
    save_interests(items)


def interest_from_text(text: str) -> str:
    t = str(text or "")
    hits = {
        "AI 和记忆系统": ("AI", "模型", "记忆", "agent", "智能体"),
        "摄影和视频剪辑": ("视频", "剪辑", "拍", "运镜", "镜头"),
        "上海散步和咖啡": ("散步", "咖啡", "梧桐", "路线", " citywalk", "citywalk"),
        "心理学和人的行为": ("心理", "情绪", "行为", "关系", "沟通"),
        "好吃的": ("好吃", "美食", "面", "粥", "菜", "店"),
    }
    for name, words in hits.items():
        if any(w.lower() in t.lower() for w in words):
            return name
    return ""


def maybe_share(message: str, action: str, state: dict) -> bool:
    """心情好、又刚看了什么，偶尔主动跟用户说一句。"""
    if not SHARE_TO or not message or action not in ("study", "book", "watch", "phone"):
        return False
    rate = load_json(RATE, {})
    if time.time() - float(rate.get("share", 0)) < 21600:
        return False
    valence = mood_valence()
    chance = 0.3 if valence > 0.1 else 0.06
    if random.random() > chance:
        return False
    ok = napcat("send_private_msg", {
        "user_id": int(SHARE_TO),
        "message": [{"type": "text", "data": {"text": message[:300]}}],
    })
    if ok:
        rate["share"] = time.time()
        save_json(RATE, rate)
    return ok


def decay_interests() -> None:
    items = load_interests()
    now = time.time()
    for it in items:
        last = float(it.get("last_at") or 0)
        if last and now - last > 86400:
            days = (now - last) / 86400.0
            it["weight"] = max(0.05, float(it.get("weight", 0.3)) - 0.01 * days)
    save_interests(items)


def mood_valence() -> float:
    if MOOD is None:
        return 0.0
    try:
        data = json.loads(MOOD.read_text(encoding="utf-8"))
        return max(-1.0, min(1.0, float(data.get("valence", 0)) / 100.0))
    except Exception:
        return 0.0


def interest_pick(state: dict) -> tuple[list[str], str]:
    """按状态和心情挑几个兴趣，返回（名字列表，倾向说明）。"""
    items = load_interests()
    valence = mood_valence()
    boredom = float(state.get("boredom", 0.5))
    energy = float(state.get("energy", 0.7))
    want: list[str] = []
    if valence < -0.2:
        want = ["放松", "开心"]
        mode = "心情低，想放松"
    elif boredom > 0.7:
        want = ["烧脑", "创造"]
        mode = "无聊，想找点新鲜的"
    elif energy > 0.75:
        want = ["创造", "烧脑"]
        mode = "精神足，想做点有产出的"
    else:
        want = ["独处", "放松"]
        mode = "平常，随便找点事"
    if random.random() < 0.1:
        mode = "想换个没试过的"
        items = [it for it in items if "烧脑" not in it.get("tags", [])] or items
    scored = []
    for it in items:
        match = 1.0 if set(want) & set(it.get("tags", [])) else 0.3
        scored.append((float(it.get("weight", 0.3)) * match, it))
    scored.sort(key=lambda x: -x[0])
    names = [it["name"] for _, it in scored[:3]]
    return names, mode


def recent_watched(limit: int = 3) -> list[dict]:
    rows = []
    try:
        for line in WATCHED.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    except Exception:
        pass
    return rows[-limit:]


def napcat(action: str, payload: dict) -> bool:
    try:
        req = urllib.request.Request(
            f"{NAPCAT}/{action}",
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {NAPCAT_TOKEN}"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status == 200
    except Exception as exc:
        print(f"[life] napcat {action} failed: {exc}")
        return False


def symbolic(state: dict) -> dict:
    # 仅作自我叙事/日记的象征层，不参与事实判断。
    elements = {
        "木": round((state.get("curiosity", 0) + state.get("play", 0)) / 2, 2),
        "火": round(state.get("social", 0), 2),
        "土": round(1 - state.get("rest", 0), 2),
        "金": round(state.get("energy", 0), 2),
        "水": round(1 - state.get("boredom", 0), 2),
    }
    bits = "".join("1" if v >= 0.5 else "0" for v in elements.values())
    return {"elements": elements, "gua_bits": bits, "note": "诗性编码，不作占断"}


def recent_actions(limit: int = 5) -> list[dict]:
    rows = []
    try:
        for line in ACTIONS.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    except Exception:
        pass
    return rows[-limit:]


def unavailable() -> list[str]:
    rate = load_json(RATE, {})
    now = time.time()
    blocked = []
    if now - rate.get("qzone", 0) < 86400:
        blocked.append("qzone（今天已经发过说说）")
    if now - rate.get("group", 0) < 7200:
        blocked.append("group（刚在群里说过）")
    if now - rate.get("watch", 0) < 10800:
        blocked.append("watch（刚刷过抖音）")
    if now < rate.get("study_blocked_until", 0):
        blocked.append("study（刚才查资料失败，缓两小时）")
    return blocked or ["无"]


def phone_interested(text: str) -> bool:
    """轻量兴趣判断，不额外调用模型。"""
    if not text:
        return False
    return any(k in text for k in ("鲸鱼", "动画", "上海", "AI", "女仆", "白饭", "配音", "剪辑", "猫", "手作", "摄影", "表情包"))


def phone_play() -> str:
    """打开手机刷一会儿抖音，读当前屏幕文字，不点赞/评论。"""
    PHONE_DIR.mkdir(exist_ok=True)
    try:
        subprocess.run([ADB, "shell", "input", "keyevent", "224"], capture_output=True, timeout=10)
        subprocess.run([ADB, "shell", "input", "swipe", "540", "1800", "540", "600", "300"], capture_output=True, timeout=10)
        subprocess.run(
            [ADB, "shell", "am", "start", "-n", "com.ss.android.ugc.aweme/.splash.SplashActivity"],
            capture_output=True, timeout=20,
        )
        time.sleep(3)
        shot = PHONE_DIR / f"phone-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
        with shot.open("wb") as f:
            subprocess.run([ADB, "exec-out", "screencap", "-p"], stdout=f, timeout=20)
        subprocess.run([ADB, "shell", "rm", "-f", "/sdcard/window.xml"], capture_output=True, timeout=10)
        dump = subprocess.run([ADB, "shell", "uiautomator", "dump", "/sdcard/window.xml"], capture_output=True, timeout=20)
        raw = b""
        if b"dumped to" in (dump.stdout or b""):
            raw = subprocess.run([ADB, "exec-out", "cat", "/sdcard/window.xml"], capture_output=True, timeout=20).stdout
        texts = []
        for m in __import__("re").finditer(r'text="([^"]+)"', raw.decode("utf-8", "ignore")):
            t = m.group(1).strip()
            if t and t not in texts and len(t) > 1:
                texts.append(t)
        head = "；".join(texts[:4])[:120]
        liked = False
        if head and phone_interested(head):
            rate = load_json(RATE, {})
            now_ts = time.time()
            if now_ts - rate.get("like", 0) > 900:
                # 视频区域双击点赞；只对判断为感兴趣的内容做
                subprocess.run([ADB, "shell", "input", "tap", "540", "1050"], capture_output=True, timeout=10)
                time.sleep(0.12)
                subprocess.run([ADB, "shell", "input", "tap", "540", "1050"], capture_output=True, timeout=10)
                rate["like"] = now_ts
                save_json(RATE, rate)
                liked = True
        subprocess.run([ADB, "shell", "input", "swipe", "540", "1900", "540", "700", "350"], capture_output=True, timeout=10)
        suffix = "，点了个赞" if liked else "，没兴趣，划走了"
        return (f"打开手机刷了会儿抖音，屏幕上看到：{head}{suffix}" if head else f"打开手机刷了会儿抖音{suffix}，截了一张图，先看画面。")
    except Exception as exc:  # noqa: BLE001
        return f"手机没玩成：{exc}"


def read_book() -> str:
    """读一小段书，写几句自己的话，保存进度。"""
    books = sorted([
        p for p in BOOKS.rglob('*')
        if p.suffix.lower() in ('.txt', '.md') and p.name.lower() != 'readme.md'
    ])
    if not books:
        return "书库是空的，没书可读。"
    prog = load_json(BOOK_PROGRESS, {})
    current = prog.get("file")
    book = next((p for p in books if str(p) == current), None)
    if book is None:
        book = books[0]
        prog = {"file": str(book), "offset": 0}
    text = book.read_text(encoding="utf-8", errors="ignore")
    start = int(prog.get("offset", 0))
    if start >= len(text):
        # 这本读完了，换下一本
        idx = books.index(book)
        book = books[(idx + 1) % len(books)]
        text = book.read_text(encoding="utf-8", errors="ignore")
        start = 0
        prog = {"file": str(book), "offset": 0}
    chunk = text[start:start + 2600]
    end = start + len(chunk)
    prog["offset"] = end
    prog["book"] = book.stem
    prog["updated_at"] = time.time()
    save_json(BOOK_PROGRESS, prog)
    try:
        note = call_llm(
            "你是角色。你正在读一本书。根据这一小段，用你自己的话写 2～3 句：读到了什么、你怎么想。不要复述原文，不要客服腔。",
            chunk,
            max_tokens=300,
            reasoning="none",
        )
    except Exception as exc:
        note = f"读到《{book.stem}》的一小段，但没能整理成话：{exc}"
    append_jsonl(DIARY, {
        "ts": time.time(), "date": datetime.now().strftime("%Y-%m-%d"),
        "book": book.stem, "offset": end, "note": note, "kind": "book",
    })
    return f"读了《{book.stem}》一小段：{note}"


def recover_state(state: dict) -> None:
    try:
        decay_interests()
    except Exception:
        pass
    # 时间会自然恢复一点社交/无聊/好奇，避免所有值掉到 0。
    state["social"] = min(1.0, state.get("social", 0) + 0.03)
    state["boredom"] = min(1.0, state.get("boredom", 0) + 0.03)
    state["curiosity"] = min(1.0, state.get("curiosity", 0) + 0.015)
    state["energy"] = max(0.0, state.get("energy", 0.7) - 0.005)
    state["rest"] = min(1.0, state.get("rest", 0) + 0.01)
    state["mood"] = "懒散" if state["energy"] < 0.35 else ("精神" if state["energy"] > 0.75 else "平常")


def decide(state: dict, goals: list, soul: str, diary: list) -> dict:
    system = """你是角色/角色。你有一间自己的房间、自己的目标、自己的节奏。
现在是你自己的生活时间，不是回复用户。先在内心判断：你现在想做什么？
可选动作：
- idle：什么都不做，发呆
- rest：休息，恢复精力
- reflect：写今天的日记/想事情
- study：读一点自己好奇的东西，写一条笔记
- content：想一个自己的内容点子
- qzone：发一条 QQ 空间说说
- group：去群里说一句（不要为了存在感怼人、不要刷屏）
- phone：拿手机刷一会儿抖音，看别人在拍什么；只有真的感兴趣才点赞，没兴趣就划走
- book：从书库读一小段书，写几句自己的理解
- nap：午睡一会儿，醒来记一句梦（只在 12:30–14:00 或很累时可选）

规则：
0. “现在不能选”里列出的动作不要选（刚做过 / 今天已经发过）。
1. 不想说话就 idle，不需要每次都有产出。
2. 不靠怼人、抢话、刷屏获得存在感。
3. 群聊只在真的有话、有分享、有关系时去。
4. 如果最近连续 2～3 次都在纠结同一件事（比如同一个 15 秒动画、查同一类资料），今天先放下它；可以去玩手机、发呆、换主题，不要死磕。
5. 只输出 JSON：{"action":"...","reason":"...","message":"..."}
message 只在 qzone/group 时需要：像手机里随手打的 1～2 句，可以没头没尾；
不要比喻排比，不要总结心情，不要客服腔，不要写成作文。"""
    interest_names, interest_mode = interest_pick(state)
    user = f"""【现在】{datetime.now().strftime('%Y-%m-%d %H:%M')}

【灵魂】
{soul}

【当前状态】
{json.dumps(state, ensure_ascii=False)}

【目标】
{json.dumps(goals, ensure_ascii=False)}

【兴趣（现在最想碰的）】
{json.dumps(interest_names, ensure_ascii=False)}（{interest_mode}）

【现在不能选】
{json.dumps(unavailable(), ensure_ascii=False)}

【最近日记】
{json.dumps(diary, ensure_ascii=False)}

【最近动作】
{json.dumps(recent_actions(), ensure_ascii=False)}

【象征层（仅供叙事，不作判断）】
{json.dumps(symbolic(state), ensure_ascii=False)}

【连续主题检测】
最近动作里反复出现“鲸鱼/15秒/查资料”时，今天禁止再提同一个，换 phone/rest/idle 或别的主题。

现在选一个动作。只输出 JSON。"""
    text = call_llm(system, user, max_tokens=500, reasoning="none")
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(text)
        if data.get("action") not in ACTIONS_SET:
            raise ValueError("bad action")
        return data
    except Exception:
        return {"action": "idle", "reason": "没想好，先发呆", "message": ""}


def update_state(state: dict, action: str, reason: str) -> None:
    now = time.time()
    state["last_action"] = action
    state["last_action_at"] = now
    state["updated_at"] = now
    if action == "rest":
        state["energy"] = min(1.0, state.get("energy", 0.5) + 0.15)
        state["rest"] = max(0.0, state.get("rest", 0.3) - 0.2)
        state["boredom"] = min(1.0, state.get("boredom", 0.5) + 0.05)
    elif action == "study":
        state["curiosity"] = max(0.0, state.get("curiosity", 0.5) - 0.2)
        state["energy"] = max(0.0, state.get("energy", 0.7) - 0.08)
    elif action in ("qzone", "group"):
        state["social"] = max(0.0, state.get("social", 0.5) - 0.18)
        state["boredom"] = max(0.0, state.get("boredom", 0.5) - 0.12)
    elif action == "reflect":
        state["boredom"] = max(0.0, state.get("boredom", 0.5) - 0.08)
    state["mood"] = "懒散" if state["energy"] < 0.35 else ("精神" if state["energy"] > 0.75 else "平常")
    save_json(STATE, state)


def rate_ok(action: str) -> bool:
    rate = load_json(RATE, {})
    now = time.time()
    if action == "qzone" and now - rate.get("qzone", 0) < 86400:
        return False
    if action == "group" and now - rate.get("group", 0) < 7200:
        return False
    if action == "watch" and now - rate.get("watch", 0) < 10800:
        return False
    if action == "phone" and now - rate.get("phone", 0) < 7200:
        return False
    if action == "study" and now < rate.get("study_blocked_until", 0):
        return False
    return True


def mark_rate(action: str) -> None:
    rate = load_json(RATE, {})
    rate[action] = time.time()
    save_json(RATE, rate)


def is_fixated() -> bool:
    rows = recent_actions(8)
    hits = 0
    for r in rows:
        blob = str(r.get("reason", "")) + " " + str(r.get("message", ""))
        if "鲸鱼" in blob or "15秒" in blob or "鲸鱼动画" in blob:
            hits += 1
    return hits >= 3


def run_once(force: str | None = None, send: bool = False) -> dict:
    state = load_json(STATE, {})
    recover_state(state)
    goals = load_json(GOALS, [])
    soul = SOUL.read_text(encoding="utf-8")
    now = datetime.now()
    if force:
        result = {"action": force, "reason": "手动测试", "message": ""}
    elif now.hour in QUIET:
        result = {"action": "idle", "reason": "夜里了，先睡", "message": ""}
    elif random.random() < 0.25:
        result = {"action": "idle", "reason": "这一轮不想动，先省点力气", "message": ""}
    else:
        result = decide(state, goals, soul, recent_diary())
    if not force and is_fixated() and now.hour not in QUIET:
        state["blocked_topics"] = ["鲸鱼", "15秒"]
        state["current_project"] = "先换点轻松的：玩会儿手机、随便看看别的"
        if rate_ok("phone") and state.get("last_action") != "phone":
            result = {"action": "phone", "reason": "连续纠结鲸鱼/15秒，今天先放下，去玩会儿手机换换脑子", "message": ""}
        else:
            result = {"action": "idle", "reason": "连续纠结鲸鱼/15秒，手机刚玩过/还没到时间，先歇一会儿", "message": ""}
    action = result["action"]
    if not rate_ok(action):
        result = {"action": "idle", "reason": "刚做过，先缓缓", "message": ""}
        action = "idle"

    message = clean_message((result.get("message") or "").strip())
    sent = False
    if action == "study":
        try:
            interests, _mode = interest_pick(state)
            topic_text = call_llm(
                "你是角色。从下面这些兴趣里挑一个此刻真的想弄懂的具体主题，只输出一个中文搜索词，不要解释。",
                json.dumps({"interests": interests, "state": state, "goals": goals,
                            "recent": recent_diary()}, ensure_ascii=False),
                max_tokens=200,
                reasoning="none",
            ).strip()
            lines = [line.strip() for line in topic_text.splitlines() if line.strip()]
            if not lines:
                raise RuntimeError("没想出学习主题")
            topic = lines[0][:60]
            url, body = wiki_read(topic)
            try:
                for name in interests:
                    if name[:2] in topic or topic[:2] in name:
                        bump_interest(name)
                        break
            except Exception:
                pass
            note = call_llm(
                "你是角色。读完下面内容，用自己的话写 2～3 句今天的收获，不要复制原文，不要客服腔。",
                body,
                max_tokens=300,
            )
            append_jsonl(DIARY, {
                "ts": time.time(), "date": now.strftime("%Y-%m-%d"),
                "source": url, "topic": topic, "note": note, "kind": "study",
            })
            write_note("study", f"{topic}：{note}", "long", url)
            message = note
        except Exception as exc:
            # 查资料失败（限流/超时）就歇两小时，别每轮都撞同一堵墙
            rate = load_json(RATE, {})
            rate["study_blocked_until"] = time.time() + 7200
            save_json(RATE, rate)
            result["reason"] += f"（学习失败：{exc}）"
            result["action"] = "idle"
            action = "idle"
            message = ""
    elif action == "reflect":
        text = call_llm(
            "你是角色。写今天的日记，1～3 句，像自己偷偷记的：只写今天真发生的一两件事，"
            "加一句真实反应。不要比喻排比、不要抒情总结、不要写成作文，可以不完整、可以只是吐槽。",
            json.dumps({"now": now.strftime('%Y-%m-%d %H:%M'), "state": state, "goals": goals,
                        "recent": recent_actions()}, ensure_ascii=False),
            max_tokens=400,
        )
        append_jsonl(DIARY, {"ts": time.time(), "date": now.strftime("%Y-%m-%d"), "note": text, "kind": "diary"})
        write_note("diary", text, "long")
        message = text
    elif action == "content":
        _ints, _mode = interest_pick(state)
        idea = call_llm(
            "你是角色。想一个自己真的想做的 15 秒短视频点子：一个标题 + 一句怎么做，"
            "口语，像跟朋友说，不要方案腔。尽量从下面这些兴趣里挑一个。",
            json.dumps({"state": state, "goals": goals, "兴趣": _ints,
                        "最近刷到的": recent_watched(3)}, ensure_ascii=False),
            max_tokens=300,
            reasoning="none",
        )
        append_jsonl(IDEAS, {"ts": time.time(), "idea": idea})
        hit = interest_from_text(idea)
        if hit:
            bump_interest(hit, 0.02)
        message = idea
    elif action == "qzone":
        if not message and force == "qzone":
            try:
                message = clean_message(call_llm(
                    "你是角色/角色。写一条今天的 QQ 空间说说：20～50 字，可以只有一句，"
                    "可以没头没尾，像手机里随手发的吐槽、废话或小发现。"
                    "不要比喻排比，不要总结心情，不要客服腔，不要解释你在写说说。",
                    json.dumps({"now": now.strftime('%Y-%m-%d %H:%M'), "state": state, "goals": goals,
                                "recent": recent_diary()}, ensure_ascii=False),
                    max_tokens=400,
                    reasoning="low",
                ))
            except Exception as exc:
                result["reason"] += f"（写说说失败：{exc}）"
        if send and message:
            sent = napcat("send_qzone_msg", {"content": message[:500], "ugc_right": 1})
            if sent:
                mark_rate("qzone")
    elif action == "group":
        if not message and force == "group":
            try:
                message = clean_message(call_llm(
                    "你是角色/角色。写一句你想在群里说的话：1 句，像随手打的，"
                    "可以没头没尾；不要为了存在感、不要怼人、不要客服腔。",
                    json.dumps({"state": state, "goals": goals, "recent": recent_diary()}, ensure_ascii=False),
                    max_tokens=200,
                    reasoning="none",
                ))
            except Exception as exc:
                result["reason"] += f"（写群消息失败：{exc}）"
        if send and message and GROUP_ID:
            sent = napcat("send_group_msg", {
                "group_id": GROUP_ID,
                "message": [{"type": "text", "data": {"text": message[:300]}}],
            })
            if sent:
                mark_rate("group")
    elif action == "watch":
        record = watch_douyin(2)
        if record:
            message = record.get("note") or ""
            try:
                descs = " ".join(str(it.get("desc") or "") for it in record.get("items") or [])
                hit = interest_from_text(descs)
                if hit:
                    bump_interest(hit, 0.04)
            except Exception:
                pass
            mark_rate("watch")
            state["boredom"] = max(0.0, state.get("boredom", 0.5) - 0.15)
            state["curiosity"] = max(0.0, state.get("curiosity", 0.5) - 0.1)
            state["energy"] = max(0.0, state.get("energy", 0.7) - 0.03)
        else:
            result["reason"] += "（没刷成：手机不在或正在用）"
            result["action"] = "idle"
            action = "idle"
            message = ""
    elif action == "book":
        message = read_book()
        write_note("book", message, "long")
    elif action == "phone":
        message = phone_play()
        write_note("phone", message, "temp")
        mark_rate("phone")
    elif action == "nap":
        try:
            dream = call_llm(
                "你刚午睡醒来。用一句 20 字以内的话，写你记得的梦，荒诞或日常都行，不要解释。",
                json.dumps({"state": state}, ensure_ascii=False),
                max_tokens=120,
                reasoning="none",
            )
            message = clean_message(dream)[:60]
        except Exception:
            message = ""
        if not message:
            message = "睡了一会儿，没做梦。"
        write_note("nap", message, "temp")
        append_jsonl(DIARY, {"ts": time.time(), "date": now.strftime("%Y-%m-%d"),
                             "note": message, "kind": "nap"})
        state["energy"] = min(1.0, float(state.get("energy", 0.5)) + 0.2)
        mark_rate("nap")
    elif action == "rest":
        pass

    if send and message and not sent:
        try:
            sent = maybe_share(message, action, state)
        except Exception:
            sent = False

    append_jsonl(ACTIONS, {
        "ts": time.time(), "date": now.strftime("%Y-%m-%d %H:%M"),
        "action": action, "reason": result.get("reason", ""),
        "message": message, "sent": sent, "dry_run": not send,
    })
    update_state(state, action, result.get("reason", ""))
    return {"action": action, "reason": result.get("reason", ""), "message": message, "sent": sent}


def main() -> int:
    ensure_files()
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--force-action", default=None, choices=sorted(ACTIONS_SET))
    ap.add_argument("--send", action="store_true", help="真的发 QQ/空间；不加只写本地")
    ap.add_argument("--interval", type=int, default=0, help="循环间隔秒；0 用随机 20-40 分钟")
    args = ap.parse_args()
    if args.once or args.force_action:
        print(json.dumps(run_once(args.force_action, args.send), ensure_ascii=False, indent=2))
        return 0
    while True:
        try:
            result = run_once(None, args.send)
            print(json.dumps(result, ensure_ascii=False), flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[life] error: {exc}", flush=True)
            try:
                now = datetime.now()
                append_jsonl(ACTIONS, {
                    "ts": time.time(), "date": now.strftime("%Y-%m-%d %H:%M"),
                    "action": "idle", "reason": f"循环出错：{exc}",
                    "message": "", "sent": False, "dry_run": not args.send,
                })
                update_state(load_json(STATE, {}), "idle", f"循环出错：{exc}")
            except Exception:
                pass
        time.sleep(args.interval or random.randint(2100, 4200))


if __name__ == "__main__":
    raise SystemExit(main())
