# -*- coding: utf-8 -*-
"""可配置的 PAD 情绪：不绑定任何角色，规则和提示都由配置决定。"""
from __future__ import annotations

import math
import time


def _clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, float(v)))


def decay(state: dict, cfg: dict, now: float | None = None) -> dict:
    now = now or time.time()
    at = float(state.get("at") or now)
    minutes = max(0.0, (now - at) / 60.0)
    if minutes:
        half = max(1.0, float(cfg.get("half_life_min") or 45))
        k = 0.5 ** (minutes / half)
        base = cfg.get("baseline") or [0.3, 0.4, 0.5]
        for i, key in enumerate(("p", "a", "d")):
            state[key] = round(float(base[i]) + (float(state.get(key, base[i])) - float(base[i])) * k, 4)
        labels = state.setdefault("labels", {})
        for name in list(labels):
            labels[name] = round(float(labels[name]) * k, 3)
            if labels[name] < 0.02:
                labels.pop(name, None)
    state["at"] = now
    return state


def appraise(text: str, cfg: dict) -> list[dict]:
    t = str(text or "")
    out = []
    for rule in cfg.get("rules") or []:
        words = rule.get("words") or []
        if not any(str(w) and str(w) in t for w in words):
            continue
        d = rule.get("delta") or [0, 0, 0]
        out.append({
            "label": str(rule.get("label") or "情绪"),
            "dp": float(d[0] if len(d) > 0 else 0),
            "da": float(d[1] if len(d) > 1 else 0),
            "dd": float(d[2] if len(d) > 2 else 0),
            "hint": str(rule.get("hint") or ""),
        })
    return out


def apply(state: dict, events: list[dict], cfg: dict) -> dict:
    step = float(cfg.get("max_step") or 0.12)
    for ev in events:
        for key, dk in (("p", "dp"), ("a", "da"), ("d", "dd")):
            state[key] = _clamp(float(state.get(key, 0.3)) + max(-step, min(step, ev[dk])))
        labels = state.setdefault("labels", {})
        name = ev["label"]
        labels[name] = round(min(0.6, float(labels.get(name) or 0) + 0.05), 3)
    return state


def top(state: dict) -> tuple[str, float]:
    labels = state.get("labels") or {}
    if not labels:
        return "", 0.0
    name = max(labels, key=lambda k: float(labels[k]))
    return name, float(labels[name])


def hint(state: dict, cfg: dict) -> str:
    name, score = top(state)
    if score < float(cfg.get("threshold") or 0.12):
        return ""
    for rule in cfg.get("rules") or []:
        if str(rule.get("label")) == name:
            return str(rule.get("hint") or "")
    return ""
