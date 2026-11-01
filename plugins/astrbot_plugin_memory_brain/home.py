# -*- coding: utf-8 -*-
"""房间与玩具库：生活层写，面板读，两边共用一个 JSON。"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path

LIFE = Path(__file__).resolve().parents[4] / "life"
HOME = LIFE / "home.json"
PAGES = Path(__file__).resolve().parent / "pages" / "panel"
PERSONA = Path(__file__).resolve().parents[4] / "AstrBot" / "data" / "plugin_data" / "astrbot_plugin_persona_state"
ASSETS = PAGES / "assets" / "home"
TOYS_DIR = ASSETS / "toys"

DEFAULT = {
    "room": "你的房间",
    "theme": "night",
    "toys": [
        {"id": "toy_whale", "name": "毛绒拼图", "type": "plush", "desc": "蓝灰色的拼图，抱着睡刚好", "novelty": 0.82, "from": "你", "added_at": 0, "rarity": "珍藏", "durability": 0.9, "liking": 0.95},
        {"id": "toy_puzzle", "name": "五百片拼图", "type": "puzzle", "desc": "海边小镇，拼到一半总被猫踩乱", "novelty": 0.65, "from": "你", "added_at": 0, "rarity": "喜欢", "durability": 0.5, "liking": 0.75},
        {"id": "toy_ball", "name": "小皮球", "type": "ball", "desc": "拍起来会弹到床底下", "novelty": 0.55, "from": "自己", "added_at": 0, "rarity": "普通", "durability": 0.7, "liking": 0.6},
    ],
}


def _read() -> dict:
    try:
        data = json.loads(HOME.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("bad home")
    except Exception:
        data = json.loads(json.dumps(DEFAULT))
    data.setdefault("toys", [])
    data.setdefault("room", DEFAULT["room"])
    data.setdefault("theme", "night")
    return data


def _write(data: dict) -> None:
    LIFE.mkdir(parents=True, exist_ok=True)
    HOME.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def gift_effect(owner_id: str = "") -> dict:
    """送礼：加好感、哄好负面情绪。返回变化量，失败不影响送礼。"""
    out = {"affinity": 0.0, "soothed": []}
    now = time.time()
    try:
        if owner_id:
            p = PERSONA / "affinity.json"
            data = json.loads(p.read_text(encoding="utf-8"))
            user = (data.get("users") or {}).get(str(owner_id))
            if user is not None:
                before = float(user.get("score") or 0)
                user["score"] = round(min(100.0, before + 3.0), 1)
                day = time.strftime("%Y-%m-%d")
                if user.get("gain_day") != day:
                    user["gain_day"] = day
                    user["gain_today"] = 0.0
                user["gain_today"] = round(float(user.get("gain_today") or 0) + 3.0, 1)
                user["last_seen"] = now
                p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                out["affinity"] = round(user["score"] - before, 1)
    except Exception:
        pass
    try:
        p = PERSONA / "mood.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        pad = data.setdefault("pad", {})
        labels = pad.setdefault("labels", {})
        for name, cut in (("生气", 0.30), ("委屈", 0.25), ("难过", 0.20)):
            if float(labels.get(name) or 0) > 0:
                labels[name] = round(max(0.0, float(labels[name]) - cut), 3)
                out["soothed"].append(name)
        labels["开心"] = round(min(0.6, float(labels.get("开心") or 0) + 0.08), 3)
        labels["感动"] = round(min(0.6, float(labels.get("感动") or 0) + 0.10), 3)
        data["valence"] = min(100.0, float(data.get("valence") or 0) + 4.0)
        pad["p"] = min(1.0, float(pad.get("p") or 0.3) + 0.03)
        pad["a"] = min(1.0, float(pad.get("a") or 0.4) + 0.02)
        pad["at"] = now
        data["updated_at"] = now
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return out


def books() -> dict:
    folder = LIFE / "books"
    prog = {}
    try:
        prog = json.loads((LIFE / "book_progress.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    cur = str(prog.get("book") or "")
    rows = []
    try:
        for p in sorted(folder.glob("*.txt")):
            text = p.read_text(encoding="utf-8", errors="ignore")
            rows.append({
                "title": p.stem,
                "chars": len(text),
                "current": p.stem == cur,
                "offset": int(prog.get("offset") or 0) if p.stem == cur else 0,
                "chapter": str(prog.get("chapter") or "") if p.stem == cur else "",
            })
    except Exception:
        pass
    rows.sort(key=lambda r: (not r["current"], r["title"]))
    return {"books": rows, "current": cur}


def snapshot() -> dict:
    data = _read()
    state = {}
    try:
        state = json.loads((LIFE / "state.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    toys = sorted(data.get("toys", []), key=lambda t: float(t.get("novelty", 0)), reverse=True)
    return {
        "room": data.get("room"),
        "theme": data.get("theme"),
        "toys": toys,
        "status": {
            "action": state.get("last_action") or "idle",
            "mood": state.get("mood") or "平常",
            "energy": float(state.get("energy") or 0),
            "at": float(state.get("last_action_at") or 0),
        },
    }


def save_image(data_url: str, toy_id: str) -> str:
    """把前端上传的 data URL 存成 PNG，返回可访问的相对路径。"""
    if not data_url.startswith("data:image/"):
        return ""
    try:
        head, b64 = data_url.split(",", 1)
        raw = base64.b64decode(b64, validate=False)
        if len(raw) > 4 * 1024 * 1024:
            return ""
        mime = head[5:].split(";", 1)[0]
        ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(mime, "png")
        TOYS_DIR.mkdir(parents=True, exist_ok=True)
        (TOYS_DIR / f"{toy_id}.{ext}").write_bytes(raw)
        return f"assets/home/toys/{toy_id}.{ext}"
    except Exception:
        return ""


def find_toy(name: str) -> dict | None:
    q = (name or "").strip()
    if not q:
        return None
    toys = _read().get("toys", [])
    for t in toys:
        if str(t.get("name") or "") == q:
            return t
    for t in toys:
        if q in str(t.get("name") or "") or str(t.get("name") or "") in q:
            return t
    return None


def find_toy_in_text(text: str) -> dict | None:
    """从一句话里找玩具：支持只说“背包”“拼图”这种短词。"""
    text = str(text or "")
    if not text:
        return None
    best = None
    best_len = 1
    for t in _read().get("toys", []):
        name = str(t.get("name") or "")
        desc = str(t.get("desc") or "")
        for size in range(min(6, len(name)), 1, -1):
            for i in range(0, len(name) - size + 1):
                frag = name[i:i + size]
                if frag in text and size > best_len:
                    best, best_len = t, size
        # “那个包”“这个球”这类单字说法
        for ch in ("包", "球", "拼图", "拼图"):
            if ch in text and (ch in name or ch in desc) and (best is None or best_len < len(ch)):
                best, best_len = t, len(ch)
    return best


def guess_type(name: str, desc: str) -> str:
    t = (name or "") + " " + (desc or "")
    if any(w in t for w in ("拼图", "积木", "乐高")):
        return "puzzle"
    if any(w in t for w in ("球", "弹力")):
        return "ball"
    if any(w in t for w in ("毛绒", "鲸", "熊", "兔", "猫", "狗", "玩偶", "抱枕", "公仔")):
        return "plush"
    return "gift"


def add_toy(name: str, desc: str = "", from_: str = "主人", img: str = "", image_data: str = "",
            secret: bool = False) -> dict:
    data = _read()
    name = (name or "").strip()[:30]
    if not name:
        raise ValueError("玩具要有名字")
    typ = guess_type(name, desc)
    toy = {
        "id": "toy_" + str(int(time.time() * 1000)),
        "name": name,
        "type": typ,
        "desc": (desc or "").strip()[:120],
        "novelty": 1.0,
        "from": (from_ or "主人").strip()[:20],
        "secret": bool(secret),
        "discovered": not bool(secret),
        "added_at": time.time(),
        "rarity": "普通",
        "durability": {"plush": 0.9, "puzzle": 0.5, "ball": 0.7}.get(typ, 0.8),
        "liking": 0.78 if typ == "plush" else 0.7,
        "img": (img or "").strip()[:200],
    }
    if not toy.get("img") and image_data:
        toy["img"] = save_image(image_data, toy["id"])
    data.setdefault("toys", []).append(toy)
    _write(data)
    return toy


def delete_toy(toy_id: str) -> dict | None:
    data = _read()
    keep = []
    gone = None
    for t in data.get("toys", []):
        if str(t.get("id")) == str(toy_id):
            gone = t
            continue
        keep.append(t)
    if gone is None:
        return None
    data["toys"] = keep
    _write(data)
    img = str(gone.get("img") or "")
    if img:
        try:
            f = PAGES / img
            if f.exists() and (PAGES / "assets/home/toys") in f.parents:
                f.unlink()
        except Exception:
            pass
    return gone


def update_toy(toy_id: str, **fields) -> dict | None:
    data = _read()
    for t in data.get("toys", []):
        if t.get("id") == toy_id:
            for k, v in fields.items():
                if v not in (None, ""):
                    t[k] = v
            _write(data)
            return t
    return None


def play_toy() -> dict | None:
    """生活层调用：挑一个最新鲜的玩具，新鲜度下降。"""
    data = _read()
    toys = data.get("toys", [])
    if not toys:
        return None
    toys.sort(key=lambda t: float(t.get("novelty", 0)), reverse=True)
    toy = toys[0]
    toy["novelty"] = max(0.0, round(float(toy.get("novelty", 0)) - 0.16, 2))
    toy["last_played"] = time.time()
    revealed = False
    if toy.get("secret") and not toy.get("discovered"):
        toy["discovered"] = True
        revealed = True
    _write(data)
    if revealed:
        toy = dict(toy); toy["secret_revealed"] = True
    return toy
