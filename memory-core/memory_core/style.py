# -*- coding: utf-8 -*-
"""可配置的表达清洗，不绑定具体角色。"""
from __future__ import annotations

import re


def clean(text: str, cfg: dict | None = None) -> str:
    cfg = cfg or {}
    t = str(text or "").strip()
    if cfg.get("strip_stage", True):
        t = re.sub(r"^[（(][^）)]{1,40}[）)]\s*", "", t)
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    max_lines = int(cfg.get("max_lines") or 3)
    lines = [ln for ln in t.split("\n") if ln.strip()]
    if len(lines) > max_lines:
        lines = lines[: max_lines - 1] + [" ".join(lines[max_lines - 1 :])]
    t = "\n".join(lines).strip()
    max_chars = int(cfg.get("max_chars") or 600)
    if len(t) > max_chars:
        t = t[:max_chars].rstrip()
    return t
