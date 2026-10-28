# -*- coding: utf-8 -*-
"""角色的自我档案 + 语气样例库。"""
from __future__ import annotations

import json
import random
import re
import time
import urllib.request
from pathlib import Path

from bs4 import BeautifulSoup

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

BASE = (Path(__file__).resolve().parents[2] / "self")
PROFILE = BASE / "self_profile.json"
STYLE = BASE / "style_examples.jsonl"
VOICE = BASE / "voice_profile.json"
LEARNING = BASE / "learning_journal.jsonl"
MARKER = "【角色的自我档案与语气】"


def _load_profile() -> dict:
    try:
        return json.loads(PROFILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_voice() -> dict:
    try:
        return json.loads(VOICE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_examples() -> list[dict]:
    rows = []
    try:
        for line in STYLE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    except Exception:
        pass
    return rows


def _save_profile(data: dict) -> None:
    PROFILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@register(
    "astrbot_plugin_self_style",
    "community",
    "注入角色自己的喜好、最近状态和真人语气样例，并允许管理员更新",
    "1.0.0",
)
class SelfStylePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.on_llm_request()
    async def inject_self(self, event: AstrMessageEvent, req) -> None:
        try:
            profile = _load_profile()
            voice = _load_voice()
            examples = _load_examples()
            if not profile:
                return
            lines = [MARKER]
            for key, label in (
                ("likes", "喜欢"),
                ("dislikes", "不喜欢"),
                ("current", "最近在关注"),
                ("speech", "说话习惯"),
            ):
                vals = profile.get(key) or []
                if vals:
                    lines.append(f"{label}：" + "、".join(str(v) for v in vals))
            if voice:
                lines.append(
                    "我的声音："
                    + str(voice.get("voice", ""))
                    + "，Mac Breeze，"
                    + str(voice.get("description", ""))[:120]
                )
            lines.append(
                "记忆规则：群聊里遇到重要事实、约定、关系变化、管理员明确说“记住”，"
                "就调用 memorize_long_term_memory 写入长期记忆；普通闲聊不要写，不要每句都写。"
            )
            if examples:
                picked = random.sample(examples, min(5, len(examples)))
                lines.append("语气样例（只学感觉，不要逐字照抄）：")
                for ex in picked:
                    lines.append(f"用户：{ex.get('user','')}")
                    lines.append(f"角色：{ex.get('assistant','')}")
            block = "\n".join(lines)
            base = req.system_prompt or ""
            cut = base.find(MARKER)
            if cut != -1:
                base = base[:cut].rstrip()
            req.system_prompt = (base + "\n\n" + block).strip()
        except Exception:
            logger.exception("[self_style] 注入自我档案失败")

    @filter.llm_tool(name="update_self_profile")
    async def update_self_profile(
        self,
        event: AstrMessageEvent,
        section: str,
        item: str,
        remove: bool = False,
    ):
        """更新角色自己的长期喜好或状态。

        Args:
            section(string): likes / dislikes / current / speech
            item(string): 要加入或删除的内容
            remove(boolean): true 表示删除
        """
        if not event.is_admin():
            return "只有管理员能改我的自我档案。"
        section = (section or "").strip()
        item = (item or "").strip()
        if section not in {"likes", "dislikes", "current", "speech"} or not item:
            return "参数不合法。"
        profile = _load_profile()
        vals = [str(v) for v in profile.get(section, [])]
        if remove:
            vals = [v for v in vals if v != item]
        elif item not in vals:
            vals.append(item)
        profile[section] = vals
        _save_profile(profile)
        return f"已更新 {section}：{item}"

    @filter.llm_tool(name="add_style_example")
    async def add_style_example(
        self,
        event: AstrMessageEvent,
        user: str,
        assistant: str,
    ):
        """添加一组“用户这样说，角色这样回”的真人语气样例。"""
        if not event.is_admin():
            return "只有管理员能加语气样例。"
        user = (user or "").strip()
        assistant = (assistant or "").strip()
        if not user or not assistant:
            return "样例不能为空。"
        with STYLE.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"user": user[:300], "assistant": assistant[:300]}, ensure_ascii=False) + "\n")
        return "语气样例已添加。"

    @filter.llm_tool(name="read_url")
    async def read_url(self, event: AstrMessageEvent, url: str):
        """读取一个网页的正文，用于学习/看文章/逛贴吧。

        Args:
            url(string): 要读的网页链接
        """
        if not event.is_admin():
            return "只有管理员能让我读网页。"
        url = (url or "").strip()
        if not url.startswith(("http://", "https://")):
            return "链接不合法。"
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"},
            )
            with urllib.request.urlopen(req, timeout=25) as resp:
                raw = resp.read(2_000_000)
                ctype = resp.headers.get("Content-Type", "")
            if "html" not in ctype and "text" not in ctype:
                return f"不是网页正文：{ctype}"
            soup = BeautifulSoup(raw, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
                tag.decompose()
            text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n"))
            text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
            return text[:6000] if text else "页面没有可读正文。"
        except Exception as exc:  # noqa: BLE001
            return f"读取失败：{exc}"

    @filter.llm_tool(name="save_learning")
    async def save_learning(
        self,
        event: AstrMessageEvent,
        title: str,
        url: str,
        note: str,
        source: str = "",
    ):
        """把今天真正学到的东西写进学习笔记。

        Args:
            title(string): 标题
            url(string): 来源链接
            note(string): 她自己的理解，不要复制原文
            source(string): 来源类型，比如 网页/贴吧/书/抖音
        """
        if not event.is_admin():
            return "只有管理员能让我写学习笔记。"
        title = (title or "").strip()[:200]
        url = (url or "").strip()[:1000]
        note = (note or "").strip()[:600]
        if not title or not note:
            return "标题或笔记为空。"
        item = {
            "title": title,
            "url": url,
            "note": note,
            "source": (source or "").strip()[:40],
            "created_at": time.time(),
        }
        with LEARNING.open("a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
        return f"记下了：{title}"

    @filter.llm_tool(name="list_learning")
    async def list_learning(self, event: AstrMessageEvent, limit: int = 8):
        """查看最近的学习笔记。"""
        if not event.is_admin():
            return "只有管理员能看学习笔记。"
        rows = []
        try:
            for line in LEARNING.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        except Exception:
            pass
        rows = rows[-max(1, min(int(limit), 30)):]
        if not rows:
            return "学习笔记还是空的。"
        return "\n".join(
            f"- [{r.get('source','')}] {r.get('title','')}｜{r.get('note','')}｜{r.get('url','')}"
            for r in rows
        )
