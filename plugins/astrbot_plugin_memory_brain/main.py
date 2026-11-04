# -*- coding: utf-8 -*-
"""记忆大脑：自己实现的长期记忆（存储 + 检索 + 遗忘），不依赖任何记忆插件。

这个文件只做 AstrBot 接线：
- 收消息 → 按会话攒成「片段」→ 写进记忆库
- 每轮请求 → 混合检索 top-k → 注入 system_prompt
- 每天一次 → 衰减 + 归档

记忆策略在 engine.py，存储细节在 store.py，可以单独测试。
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.core.message.components import Video
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api.star import Context, Star, register

from . import intention
from . import (
    abstract, conflict, consolidate, extractor, media, multimodal, procedural, profile,
    health, lessons, reflection, style, voice, working,
)
from .report import build as build_report

PLUGIN_ROOT = Path(__file__).resolve().parent
DATA_DIR = PLUGIN_ROOT.parents[1]  # AstrBot/data
WORKSPACE = PLUGIN_ROOT.parents[3]
LIFE_DIR = Path(os.environ.get("ASSISTANT_LIFE_DIR", str(WORKSPACE / "life")))
MOOD_FILE = Path(os.environ.get("ASSISTANT_MOOD_FILE", str(DATA_DIR / "plugin_data" / "astrbot_plugin_persona_state" / "mood.json")))
from .engine import MemoryEngine, _cosine
from .store import Store

PLUGIN_NAME = "astrbot_plugin_memory_brain"
MARKER = "【记忆片段】"
SPEAKER_MARKER = "【当前说话人】"
VOICE_MARKER = "【对方这条语音说的是】"
PROFILE_MARKER = "【对他们的印象】"
WORKING_MARKER = "【还没做完的事】"
STYLE_MARKER = "【最近的聊天习惯】"
GIFT_MARKER = "【你最近发现的礼物】"
TOY_MARKER = "【玩具图片】"
RENAME_MARKER = "【玩具改名】"
CLOSET_MARKER = "【你的衣柜】"
HOME_MARKER = "【房间和样子】"
GROUP_MARKER = "【群里最近说的话】"
PRIVATE_MARKER = "【私聊最近说的话】"
WEBCHAT_MARKER = "【QQ 那边最近说的话】"
CHARACTER_LOOK = "你本人是蓝发蓝眼、蓝裙子、白色围裙，围裙上有一只小拼图，头上有一撮呆毛"
LAYER_NAMES = {
    "bed": "床", "desk": "书桌和电脑", "bookshelf": "书架", "chair": "椅子",
    "rug": "地毯", "plant": "绿植", "window": "窗", "curtain": "窗帘",
    "curtain_left": "窗帘", "curtain_right": "窗帘", "lamp": "台灯",
    "ceiling_light": "吊灯", "toy_shelf": "玩具架",
}


def human_when(ts: float) -> str:
    """把时间说成人话：今天/昨天/前天/前几天/上周/大概几月。"""
    try:
        ts = float(ts or 0)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    now = datetime.datetime.now()
    then = datetime.datetime.fromtimestamp(ts)
    days = (now.date() - then.date()).days
    if days <= 0:
        return "今天"
    if days == 1:
        return "昨天"
    if days == 2:
        return "前天"
    if days == 3:
        return "大前天"
    if days <= 6:
        return "前几天"
    if days <= 13:
        return "上周"
    if days <= 29:
        return "前几周"
    if days <= 59:
        return "上个月"
    if days <= 364:
        return f"大概{then.month}月"
    return f"去年{then.month}月"

# 攒够多少条消息写一段记忆
BATCH_SIZE = 12
# 注入多少条
DEFAULT_K = 3


def _data_dir() -> Path:
    try:
        from astrbot.core.utils.astrbot_data_path import get_astrbot_data_path  # type: ignore
    except Exception:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path  # type: ignore
        except Exception:
            return Path("data").resolve() / "plugin_data" / PLUGIN_NAME
    return Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME


class MemoryBrainPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        base = _data_dir()
        base.mkdir(parents=True, exist_ok=True)
        self.store = Store(base / "brain.db")
        self.engine = MemoryEngine(self.store)

        self.inject_enabled = bool(self.config.get("inject", True))
        self.k = max(1, min(10, int(self.config.get("recall_k", DEFAULT_K) or DEFAULT_K)))
        try:
            self.recall_min_score = float(self.config.get("recall_min_score", 0.3) or 0.3)
        except (TypeError, ValueError):
            self.recall_min_score = 0.3
        self.inject_in_group = bool(self.config.get("inject_in_group", True))
        self.batch_size = max(4, int(self.config.get("batch_size", BATCH_SIZE) or BATCH_SIZE))
        self.embedding_provider_id = str(self.config.get("embedding_provider_id") or "").strip()
        self.llm_summary = bool(self.config.get("llm_summary", True))
        self.alias_map = self._parse_alias_map(self.config.get("alias_map", ""))
        self.reflection_enabled = bool(self.config.get("reflection_enabled", True))
        self.abstract_enabled = bool(self.config.get("abstract_enabled", True))
        self.inject_style = bool(self.config.get("inject_style", True))
        self.procedural_enabled = bool(self.config.get("procedural_enabled", True))
        self.lessons_enabled = bool(self.config.get("lessons_enabled", True))
        self.multimodal_enabled = bool(self.config.get("multimodal_enabled", True))
        self.voice_enabled = bool(self.config.get("voice_enabled", True))
        self.video_enabled = bool(self.config.get("video_enabled", True))
        try:
            self.video_daily = max(0, int(self.config.get("video_max_per_day", 5) or 0))
        except (TypeError, ValueError):
            self.video_daily = 5
        self.voice_model = str(self.config.get("voice_model", "small") or "small")
        self._last_voice_id = ""
        self._last_auto_toy: dict[str, tuple[str, float]] = {}
        self._last_image: dict[str, tuple[float, str]] = {}
        self._last_text: dict[str, tuple[float, str]] = {}
        self._sent_toy = None
        self._sent_home = None
        self._last_voice_block = ""
        self._last_hits: dict[str, list[int]] = {}
        self._correction_re = re.compile(
            r"不是这样|不是这个意思|不是那样|你记错|记错了|说错了|你错了|我没说过|别乱说|胡说"
        )
        self._vision_warned = False
        self.multimodal_in_group = bool(self.config.get("multimodal_in_group", False))
        try:
            self.vision_daily = max(0, int(self.config.get("multimodal_max_per_day", 10) or 0))
        except (TypeError, ValueError):
            self.vision_daily = 10
        self._skill_cooldown: dict[str, float] = {}
        try:
            self.style_samples = max(50, int(self.config.get("style_sample_size", 300) or 300))
        except (TypeError, ValueError):
            self.style_samples = 300
        try:
            self.abstract_max = max(1, min(12, int(self.config.get("abstract_max_facts", 5) or 5)))
        except (TypeError, ValueError):
            self.abstract_max = 5
        self.profile_enabled = bool(self.config.get("profile_enabled", True))
        self.inject_profile = bool(self.config.get("inject_profile", True))
        self.inject_gift = bool(self.config.get("inject_gift", True))
        self.auto_toy_image = bool(self.config.get("auto_toy_image", True))
        self.auto_home_image = bool(self.config.get("auto_home_image", True))
        self.inject_speaker = bool(self.config.get("inject_speaker", True))
        self.private_recall_group = bool(self.config.get("private_recall_group", True))
        self.group_recall_private = bool(self.config.get("group_recall_private", True))
        self.webchat_share_qq = bool(self.config.get("webchat_share_qq", True))
        self.profile_max = max(1, int(self.config.get("profile_max_per_run", 5) or 5))
        self.profile_min_degree = max(1, int(self.config.get("profile_min_degree", 2) or 2))
        try:
            self.profile_days = float(self.config.get("profile_interval_days", 7) or 7)
        except (TypeError, ValueError):
            self.profile_days = 7.0

        self._buffers: dict[str, list[tuple[str, str]]] = {}
        self._tasks: set[asyncio.Task] = set()
        self._activity_until = 0.0
        self._last_decay = 0.0
        self._embedding = None
        self._page_api = None

    @staticmethod
    def _parse_alias_map(raw) -> dict[str, str]:
        """\"我=助手, 阿红=小红\" → {我: 助手, 阿红: 小红}"""
        out: dict[str, str] = {}
        for part in re.split(r"[,，\n]", str(raw or "")):
            part = part.strip()
            if not part or "=" not in part:
                continue
            key, _, value = part.partition("=")
            key, value = key.strip(), value.strip()
            if key and value and key != value:
                out[key] = value
        return out

    # ---------------------------------------------------------------- lifecycle

    async def initialize(self) -> None:
        # 提供方可能在插件初始化之后才就绪，这里懒加载，第一次用到时再找
        self._embedding = None
        s = self.engine.stats()
        try:
            from .page_api import PageApi

            self._page_api = PageApi(self)
            self._page_api.register_routes()
        except Exception as exc:
            logger.warning(f"[brain] 页面接口注册失败（不影响记忆）：{exc}")
        task = asyncio.create_task(self._reminder_loop())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        logger.info(
            f"[brain] 已启动：记忆 {s['active']} 条（归档 {s['archived']}），"
            f"实体 {s['entities']}，关系 {s['relations']}；"
            f"向量 {'开' if self._embedding else '关'}；注入 top-{self.k}"
        )

    async def terminate(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()
        for key in list(self._buffers):
            await self._flush_rows(key, self._buffers.pop(key, []))
        self.store.close()

    def _find_embedding_provider(self):
        if not self.embedding_provider_id:
            return None
        try:
            providers = self.context.get_all_embedding_providers()
            ids = []
            for provider in providers:
                cfg = getattr(provider, "provider_config", None)
                ids.append(str(getattr(provider, "provider_id", "") or (cfg or {}).get("id") or "?"))
            logger.info(f"[brain] 可用 embedding 提供方：{ids}")
            for provider in providers:
                pid = getattr(provider, "provider_id", "")
                if not pid:
                    cfg = getattr(provider, "provider_config", None)
                    if isinstance(cfg, dict):
                        pid = str(cfg.get("id") or "")
                if pid == self.embedding_provider_id:
                    return provider
        except Exception:
            logger.exception("[brain] 查找 embedding 提供方失败")
        return None

    async def _embed(self, text: str):
        provider = self._embedding
        if provider is None:
            provider = self._embedding = self._find_embedding_provider()
            if provider is not None:
                logger.info("[brain] embedding 提供方已就绪，开启语义检索")
        if provider is None:
            return None
        try:
            vec = await provider.get_embedding(text)
            import numpy as np

            return np.asarray(vec, dtype=np.float32)
        except Exception:
            logger.debug("[brain] embedding 失败，本次只用关键词检索", exc_info=True)
            return None

    # ---------------------------------------------------------------- 写入

    def _session_key(self, event: AstrMessageEvent) -> str:
        return str(getattr(event, "unified_msg_origin", "") or f"{event.get_sender_id()}")

    def _buffer_message(self, event: AstrMessageEvent) -> None:
        text = (event.get_message_str() or "").strip()
        if not text:
            return
        key = self._session_key(event)
        name = (event.get_sender_name() or "").strip()[:20] or "某人"
        self._buffers.setdefault(key, []).append((name, text))
        if len(self._buffers[key]) >= self.batch_size:
            self._schedule_flush(key)

    def _schedule_flush(self, key: str) -> None:
        rows = self._buffers.pop(key, [])
        if not rows:
            return
        task = asyncio.create_task(self._flush_rows(key, rows))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _get_provider(self, umo: str):
        if self.context is None:
            return None
        try:
            return self.context.get_using_provider(umo)
        except Exception:
            logger.debug("[brain] 取 LLM 提供方失败", exc_info=True)
            return None

    async def _summarize(self, key: str, rows: list[tuple[str, str]]) -> dict[str, Any]:
        """让模型把一批消息整理成一条记忆；失败就用规则兜底。"""
        provider = self._get_provider(key) if self.llm_summary else None
        if provider is not None:
            conversation = "\n".join(f"{name}：{msg}" for name, msg in rows)[-3000:]
            try:
                resp = await provider.text_chat(
                    prompt=extractor.build_prompt(conversation),
                    system_prompt=extractor.SYSTEM_PROMPT,
                )
                data = extractor.normalize(extractor.parse_json(getattr(resp, "completion_text", "")))
                if data:
                    return data
                logger.warning("[brain] 摘要不是合法 JSON，改用规则兜底")
            except Exception:
                logger.exception("[brain] 摘要调用失败，改用规则兜底")
        data = extractor.fallback(rows)
        data["importance"] = self._importance(data["summary"], len(rows))
        return data

    async def _flush_rows(self, key: str, rows: list[tuple[str, str]]) -> None:
        if not rows:
            return
        try:
            data = await self._summarize(key, rows)
            vec = await self._embed(data["summary"])
            mid = self.engine.remember(
                data["summary"],
                kind=data.get("kind", "episode"),
                importance=float(data.get("importance", 0.45)),
                valence=float(data.get("valence", 0.0)),
                source=key,
                visibility=self._visibility_for(key),
                entities=[(e["name"], e["type"]) for e in data.get("entities", [])],
                relations=[(r["src"], r["dst"], r["relation"]) for r in data.get("relations", [])],
                vector=vec,
            )
            logger.info(
                f"[brain] 写入记忆 #{mid}（{len(rows)} 条消息，{data.get('kind')}，"
                f"重要性 {data.get('importance'):.2f}，实体 {len(data.get('entities', []))}，"
                f"关系 {len(data.get('relations', []))}）"
            )
        except Exception:
            logger.exception("[brain] 写入记忆失败")

    @staticmethod
    def _importance(text: str, count: int) -> float:
        score = 0.35 + min(0.2, count / 60.0)
        if len(text) > 200:
            score += 0.1
        if re.search(r"永远记住|答应|约定|第一次|生日|吵架|道歉|骗|委屈", text):
            score += 0.2
        return max(0.1, min(0.9, score))

    # ---------------------------------------------------------------- 检索与注入

    @filter.on_llm_request(priority=-7)
    async def inject(self, event: AstrMessageEvent, req) -> None:
        try:
            self._strip_audio(req)
            self._sent_toy = None
            self._sent_home = None
            self._cache_last_text(event)
            self._cache_last_image(event)
            await self._maybe_auto_toy(event)
            await self._maybe_auto_home(event)
            self._maybe_correction(event)
            self._capture_raw(event)
            await self._maybe_intention(event)
            await self._maybe_gift(event)
            self._capture_working(event)
            self._maybe_learn_skill(event)
            self._maybe_caption_images(event)
            self._maybe_caption_video(event)
            voice_block = await self._maybe_voice(event)
            self._buffer_message(event)
            self._maybe_decay()

            blocks: list[str] = []
            if self.inject_enabled:
                memory_block = await self._memory_block(event)
                if memory_block:
                    blocks.append(memory_block)
            group_block = self._group_recent_block(event)
            if group_block:
                blocks.append(group_block)
            private_block = self._private_recent_block(event)
            if private_block:
                blocks.append(private_block)
            webchat_block = self._webchat_recent_block(event)
            if webchat_block:
                blocks.append(webchat_block)
            if self.inject_speaker:
                speaker_block = self._speaker_block(event)
                if speaker_block:
                    blocks.append(speaker_block)
            if self.inject_profile:
                profile_block = self._profile_block(event)
                if profile_block:
                    blocks.append(profile_block)
            if self.inject_gift:
                gift_block = self._gift_block(event)
                if gift_block:
                    blocks.append(gift_block)
            toy_hint = self._toy_hint(event)
            if toy_hint:
                blocks.append(toy_hint)
            home_hint = self._home_hint(event)
            if home_hint:
                blocks.append(home_hint)
            else:
                home_knowledge = self._home_knowledge(event)
                if home_knowledge:
                    blocks.append(home_knowledge)
            rename_hint = self._rename_hint(event)
            if rename_hint:
                blocks.append(rename_hint)
            closet_hint = self._closet_hint(event)
            if closet_hint:
                blocks.append(closet_hint)
            style_block = self._style_block(event)
            if style_block:
                blocks.append(style_block)
            working_block = self._working_block(event)
            if working_block:
                blocks.append(working_block)
            if voice_block:
                blocks.append(voice_block)
            if not blocks:
                return

            body = "\n\n".join(blocks)
            # 动态记忆/画像/衣帽间放当前轮用户消息末尾，不写 system prompt。
            # 内容、检索、权限与历史保存语义都不变。
            from astrbot.core.agent.message import TextPart
            parts = getattr(req, "extra_user_content_parts", None)
            if parts is None:
                parts = req.extra_user_content_parts = []
            parts.append(TextPart(text=body).mark_as_temp())
            try:
                import hashlib
                import os as _os
                if _os.environ.get("ASTRBOT_CACHE_DEBUG") == "1":
                    sys_text = str(getattr(req, "system_prompt", "") or "")
                    extra_text = "\n".join(str(getattr(x, "text", "") or "") for x in parts)
                    logger.info(
                        "[cache-debug] system=%s/%d extra=%s/%d",
                        hashlib.sha1(sys_text.encode("utf-8")).hexdigest()[:12], len(sys_text),
                        hashlib.sha1(extra_text.encode("utf-8")).hexdigest()[:12], len(extra_text),
                    )
            except Exception:
                pass
        except Exception:
            logger.exception("[brain] 注入失败")

    # 这些短句本身没有检索价值，单发时不查记忆
    _TASK_RE = re.compile(
        r"帮|做|弄|改|写|查|整理|导|剪|画|发|建|修|买|订|转|生成|校对|核对|排版|翻译"
    )
    _WORKING_RE = re.compile(r"以后都|以后按|记住这个做法|下次也这么|就这么办")

    _GENERIC = {"在吗", "在不在", "嗯", "哦", "好", "好吧", "早安", "晚安", "哈哈", "哈", "?", "？", "...", "……"}

    @staticmethod
    def _time_window(text: str) -> tuple[float, float, str] | None:
        """把“早上/昨天/刚才”翻成时间范围，用于按时间补召回。"""
        now = datetime.datetime.now()
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        t = text or ""
        if "刚才" in t or "刚刚" in t:
            return ((now - datetime.timedelta(hours=2)).timestamp(), (now + datetime.timedelta(minutes=1)).timestamp(), "刚才")
        if "昨天" in t or "昨晚" in t:
            base = day - datetime.timedelta(days=1)
            if "晚" in t:
                return ((base + datetime.timedelta(hours=18)).timestamp(), (base + datetime.timedelta(days=1)).timestamp(), "昨晚")
            return (base.timestamp(), (base + datetime.timedelta(days=1)).timestamp(), "昨天")
        if "今早" in t or "早上" in t or "上午" in t:
            return ((day + datetime.timedelta(hours=5)).timestamp(), (day + datetime.timedelta(hours=12)).timestamp(), "今天早上")
        if "中午" in t:
            return ((day + datetime.timedelta(hours=11)).timestamp(), (day + datetime.timedelta(hours=14)).timestamp(), "今天中午")
        if "下午" in t:
            return ((day + datetime.timedelta(hours=12)).timestamp(), (day + datetime.timedelta(hours=18)).timestamp(), "今天下午")
        if "今晚" in t or "晚上" in t:
            return ((day + datetime.timedelta(hours=18)).timestamp(), (day + datetime.timedelta(days=1)).timestamp(), "今天晚上")
        if "今天" in t:
            return (day.timestamp(), (now + datetime.timedelta(minutes=1)).timestamp(), "今天")
        return None

    async def _memory_block(self, event: AstrMessageEvent) -> str:
        current = (event.get_message_str() or "").strip()
        if not current:
            return ""
        owner_set = {x.strip() for x in str(self.config.get("owner_names", "")).split(",") if x.strip()}
        is_group = bool(event.get_group_id())
        is_webchat = "webchat" in str(getattr(event, "unified_msg_origin", "") or "")
        if is_group and not self.inject_in_group:
            return ""

        # 用最近两句话补全检索语境（“在吗”这种短句不单独查）
        key = self._session_key(event)
        recent = [m[1] for m in self._buffers.get(key, [])[-2:]]
        context = " ".join(r[:60] for r in recent if r and r != current).strip()
        short = re.sub(r"\[At:[^\]]+\]", "", current).strip()
        if (not short or short in self._GENERIC
                or re.fullmatch(r"[？?！!。…\s]+", short)
                or (len(short) <= 3 and not context)):
            return ""
        query = (context + " " + current).strip()[:200]

        # 向量召回最多等 1.2 秒；超时退回关键词，不拖住整轮回复。
        try:
            vec = await asyncio.wait_for(self._embed(query), timeout=1.2)
        except Exception:
            vec = None
        k = int(self.config.get("recall_k_group", 1) or 1) if is_group else self.k
        min_score = (
            float(self.config.get("recall_min_score_group", 0.45) or 0.45)
            if is_group else self.recall_min_score
        )
        asks_group = "群" in current
        is_owner = (
            str(event.get_sender_id()) == str(self.config.get("owner_id") or "")
            or str(event.get_sender_name() or "") in owner_set
        )
        if is_group:
            allow = {"public", "group"}
            if self.group_recall_private:
                allow.add("private")
        elif is_webchat and (asks_group or self.webchat_share_qq):
            allow = {"public", "private", "group"}
        else:
            allow = {"public", "private"}
            # 主人的私聊可以带出群里的记忆；其他人私聊仍然只看 private/public
            if self.private_recall_group and is_owner:
                allow.add("group")
        hits = self.engine.recall(
            query, k=k, query_vec=vec,
            rerank=bool(self.config.get("rerank", True)),
            visibility_allow=allow,
            mood_valence=self._mood_valence(),
        )
        # 分数太低的宁可不注入：省 token，也减少“乱提旧事”
        hits = [h for h in hits if float(h.get("score", 0)) >= min_score]
        raw_notes: list[tuple[float, str]] = []
        tw = self._time_window(query)
        if tw:
            start, end, label = tw
            pool = self.store.memories_in_range(start, end, allow, limit=8, kinds=("episode", "reflection"))
            extra = [m for m in pool if "发的图" not in str(m.get("text") or "")][:3]
            if not extra:
                extra = pool[:3]
            if is_group:
                raw_all = self.store.raw_between(start, end, limit=8, session_like="%GroupMessage%")
                raw_notes = raw_all
            elif is_webchat:
                like = "%GroupMessage%" if asks_group else "%FriendMessage%"
                raw_all = self.store.raw_between(start, end, limit=12, session_like=like)
                raw_notes = [r for r in raw_all if not owner_set or r[1] in owner_set][:6]
            else:
                raw_all = self.store.raw_between(start, end, limit=8, session_like="%FriendMessage%")
                if self.private_recall_group and is_owner:
                    raw_all += self.store.raw_between(start, end, limit=8, session_like="%GroupMessage%")
                    raw_all.sort(key=lambda r: float(r[0] or 0), reverse=True)
                raw_notes = [r for r in raw_all if not owner_set or r[1] in owner_set][:6]
            if extra:
                seen = {int(h["id"]) for h in hits}
                kk = k if is_group else max(k, 3)
                hits = ([{**m, "score": 1.0, "time_label": label} for m in extra]
                        + [h for h in hits if int(h["id"]) not in seen])[:kk]
        reading = ""
        try:
            prog = json.loads((Path(__file__).resolve().parents[4] / "life" / "book_progress.json").read_text(encoding="utf-8"))
            if str(prog.get("book") or "").strip():
                off = int(prog.get("offset") or 0)
                total = int(prog.get("total") or 0)
                pct = (off / total * 100) if total else 0
                tail = f"，约 {pct:.0f}%，还没读完" if total and pct < 95 else ""
                reading = (f"【最近在读】《{prog['book']}》{prog.get('chapter') or ''}，"
                           f"已读约 {off} 字{tail}")
        except Exception:
            reading = ""
        if not hits and not reading:
            return ""

        today = time.localtime()
        lines = [MARKER]
        if reading:
            lines.append(reading)
        if hits:
            lines.append("以下是和当前话题相关的旧记忆，用来接话，别逐条复述：")
            for i, h in enumerate(hits, 1):
                ts = float(h.get("created_at") or time.time())
                dt = time.localtime(ts)
                days = (datetime.date(today.tm_year, today.tm_mon, today.tm_mday)
                        - datetime.date(dt.tm_year, dt.tm_mon, dt.tm_mday)).days
                rel = "今天" if days == 0 else ("昨天" if days == 1 else f"{days}天前")
                stamp = time.strftime("%Y-%m-%d", dt)
                lines.append(f"{i}. [{stamp} · {rel}] {h['text'][:160]}")
        if raw_notes:
            lines.append("你当时说的原话（按时间）：")
            for ts, sender, txt in raw_notes:
                who = "你" if (not owner_set or sender in owner_set) else (sender or "某人")
                lines.append(f"- [{time.strftime('%H:%M', time.localtime(ts))}] {who}：{txt[:80]}")
        block = "\n".join(lines)
        self._last_hits[key] = [int(h["id"]) for h in hits]
        self.store.bump_daily("记忆注入")
        logger.info(f"[brain] 注入记忆 {len(hits)} 条（{len(block)} 字）")
        return block


    def _cache_last_text(self, event: AstrMessageEvent) -> None:
        try:
            t = (event.get_message_str() or "").strip()
            if t:
                self._last_text[self._session_key(event)] = (time.time(), t)
        except Exception:
            pass

    def _cache_last_image(self, event: AstrMessageEvent) -> None:
        try:
            comps = getattr(getattr(event, "message_obj", None), "message", None) or []
            images = [c for c in comps if c.__class__.__name__ == "Image"]
            if not images:
                return
            from . import multimodal
            ref = multimodal.image_ref(images[0])
            if ref:
                self._last_image[self._session_key(event)] = (time.time(), ref)
        except Exception:
            pass

    async def _maybe_gift(self, event: AstrMessageEvent) -> None:
        """聊天里发图说“送你一个礼物”，直接收进玩具架。"""
        try:
            text = (event.get_message_str() or "").strip()
            if not text:
                hit = self._last_text.get(self._session_key(event))
                if hit and time.time() - hit[0] <= 180:
                    text = hit[1]
            if not text or not re.search(r"礼物|送你|送她|给她|给你买|送给你|送给|收下|一套|这件|这套|挂上|挂起来|备着|收着|留着|给你穿|给你备|给你留|给助手|要吗|要嘛|(?:放|挂|塞|收).{0,4}(?:衣柜|衣橱)|(?:衣柜|衣橱).{0,4}(?:放|挂|塞|收)", text):
                return
            comps = getattr(getattr(event, "message_obj", None), "message", None) or []
            images = [c for c in comps if c.__class__.__name__ == "Image"]
            cached = None
            if not images:
                hit = self._last_image.get(self._session_key(event))
                if hit and time.time() - hit[0] <= 900:
                    cached = hit[1]
            if not images and not cached:
                return
            import base64
            import urllib.request
            from . import home as home_mod
            from . import multimodal

            ref = multimodal.image_ref(images[0]) if images else str(cached or "")
            data_url = ""
            if ref.startswith("data:image/"):
                data_url = ref
            elif ref.startswith(("http://", "https://")):
                with urllib.request.urlopen(ref, timeout=20) as r:
                    raw = r.read()
                    if len(raw) <= 4 * 1024 * 1024:
                        data_url = "data:image/jpeg;base64," + base64.b64encode(raw).decode()
            if not data_url:
                return
            m = re.search(r"(?:送|给)(?:你|我)?(?:一个|一只|个|只|份)?\s*([^，。！？!?\s]{1,12})", text)
            name = (m.group(1) if m else "礼物").strip("的了吧呀啊") or "礼物"
            if re.search(r"这套|这件|一套|这身", text):
                m2 = re.search(r"(?:这套|这件|一套|这身)([^，。！？!?\s]{0,14})", text)
                cand = (m2.group(1) if m2 else "").strip("的了吧呀啊")
                cand = cand.replace("的衣服", "").rstrip("的")
                if cand and not re.search(r"衣柜|衣橱|挂|放|塞", cand):
                    name = cand[:30]
                if not name or name in ("礼物", "衣服", "的"):
                    name = "这套衣服"
            sender = (event.get_sender_name() or "主人").strip()
            secret = bool(re.search(r"偷偷|匿名|别告诉她|不告诉她", text))
            who = re.search(r"([\u4e00-\u9fa5]{1,4})(?:送的|送你的|托我|让我送)", text)
            from_name = who.group(1) if who and who.group(1) not in ("我", "你") else sender
            is_acc = bool(re.search(r"眼镜|墨镜|帽子|发饰|发夹|耳环|项链|手链|戒指|围巾|手套|头饰|胸针|腰带|配饰|鞋|袜子|饰品", text))
            is_cloth = bool(re.search(r"衣服|裙子|外套|上衣|裤子|睡衣|装扮|换上|穿上|衣柜|衣橱|挂上|挂起来|备着|收着|留着|给你穿|给你备|给你留|要吗|要嘛", text))
            if is_acc:
                acc = home_mod.add_accessory(name, desc="聊天里送的配饰", from_=from_name,
                                             image_data=data_url)
                effect = home_mod.gift_effect(str(self.config.get("owner_id") or "") if str(event.get_sender_id()) == str(self.config.get("owner_id") or "") else "")
                try:
                    mem = f"{from_name}送了一件配饰：{name}。"
                    vec = await self._embed(mem)
                    self.engine.remember(mem, kind="fact", importance=0.72, source="home",
                                         visibility="private", meta={"accessory": name}, vector=vec)
                except Exception:
                    pass
                extra = f"，好感 +{effect.get('affinity')}" if effect.get("affinity") else ""
                await event.send(MessageChain([Plain(f"收到啦，{name}我放配饰盒里了{extra}")]))
                event.stop_event()
                return
            if is_cloth and name in ("礼物", "衣服", "挂上", "挂起来", "备着", "收着", "留着", "要吗", "要嘛", "这个", "这套", "这件"):
                name = "这套衣服"
            if is_cloth:
                base = name
                n = 2
                while home_mod.find_clothing(name):
                    name = f"{base}{n}"
                    n += 1
                cloth = home_mod.add_clothing(name, desc="聊天里送的衣服", from_=from_name,
                                              image_data=data_url)
                effect = home_mod.gift_effect(str(self.config.get("owner_id") or "") if str(event.get_sender_id()) == str(self.config.get("owner_id") or "") else "")
                try:
                    mem = f"{from_name}送了一件衣服：{name}。"
                    vec = await self._embed(mem)
                    self.engine.remember(mem, kind="fact", importance=0.72, source="home",
                                         visibility="private", meta={"cloth": name}, vector=vec)
                except Exception:
                    pass
                extra = f"，好感 +{effect.get('affinity')}" if effect.get("affinity") else ""
                await event.send(MessageChain([Plain(f"收到啦，{name}我挂衣柜里了{extra}")]))
                event.stop_event()
                return
            toy = home_mod.add_toy(name, desc="聊天里送的礼物", from_=from_name,
                                   image_data=data_url, secret=secret)
            effect = home_mod.gift_effect(str(self.config.get("owner_id") or "") if str(event.get_sender_id()) == str(self.config.get("owner_id") or "") else "")
            memory = f"{from_name}送了一个玩具：{name}。" + ("（偷偷放的，她还不知道是谁）" if secret else "")
            try:
                vec = await self._embed(memory)
                self.engine.remember(memory, kind="fact", importance=0.7, source="home",
                                     visibility="private", meta={"toy": name}, vector=vec)
            except Exception:
                pass
            extra = f"，好感 +{effect.get('affinity')}" if effect.get("affinity") else ""
            await event.send(MessageChain([Plain(f"收到啦，{name}我放玩具架上了{extra}")]))
            event.stop_event()
        except Exception:
            logger.debug("[brain] 聊天送礼失败", exc_info=True)

    async def _maybe_intention(self, event: AstrMessageEvent) -> None:
        """识别“明天三点提醒我”这类话，存成带到期时间的提醒。"""
        try:
            text = (event.get_message_str() or "").strip()
            if len(text) < 6 or text.startswith("/"):
                return
            parsed = intention.parse(text)
            if not parsed:
                return
            due, task, when = parsed
            for m in self.store.list_memories(kind="intention", limit=100):
                meta = m.get("meta") or {}
                if not meta.get("done") and str(m.get("text") or "") == task:
                    return
            umo = str(event.unified_msg_origin)
            vec = await self._embed(task)
            mid = self.store.insert_memory(
                text=task, kind="intention", importance=0.9, source="intention",
                visibility="group" if event.get_group_id() else "private",
                meta={"due_at": float(due), "done": False, "when": when, "umo": umo},
                vector=vec,
            )
            logger.info(f"[brain] 记下提醒 #{mid}：{when} {task[:30]}")
        except Exception:
            logger.debug("[brain] 提醒识别失败", exc_info=True)

    async def _reminder_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await self._fire_reminders()
            except Exception:
                logger.debug("[brain] 提醒检查失败", exc_info=True)

    async def _fire_reminders(self) -> None:
        now = time.time()
        for m in self.store.list_memories(kind="intention", limit=200):
            meta = dict(m.get("meta") or {})
            if meta.get("done"):
                continue
            due = float(meta.get("due_at") or 0)
            if not due or due > now:
                continue
            umo = str(meta.get("umo") or "")
            mid = int(m["id"])
            if not umo:
                meta["done"] = True
                self.store.set_meta(mid, meta)
                continue
            try:
                await self.context.send_message(
                    umo, MessageChain([Plain(f"你之前让我提醒你：{m['text']}")])
                )
                meta["done"] = True
                meta["fired_at"] = now
                self.store.set_meta(mid, meta)
                self.store.conn.commit()
                logger.info(f"[brain] 发出提醒 #{mid}")
            except Exception:
                meta["attempts"] = int(meta.get("attempts", 0)) + 1
                if meta["attempts"] >= 3:
                    meta["done"] = True
                self.store.set_meta(mid, meta)
                self.store.conn.commit()

    def _capture_raw(self, event: AstrMessageEvent) -> None:
        """留一份真人原话，用来统计语感（不参与记忆检索）。"""
        try:
            text = (event.get_message_str() or "").strip()
            if len(text) < 2 or text.startswith("/"):
                return
            self.store.add_raw(
                self._session_key(event),
                "user",
                text,
                (event.get_sender_name() or "").strip()[:20],
                keep=int(self.config.get("raw_keep", 2000) or 2000),
            )
        except Exception:
            logger.debug("[brain] 原始消息采集失败", exc_info=True)


    @filter.llm_tool(name="send_toy_picture")
    async def send_toy_picture(self, event: AstrMessageEvent, name: str = "") -> str:
        """把玩具架上某个玩具的图片发到当前聊天。对方想看玩具长什么样、或提到某个玩具时调用。"""
        from . import home as home_mod

        try:
            toy = home_mod.find_toy(name)
            if not toy:
                toy = home_mod.find_toy_in_text(event.get_message_str() or "")
            if not toy:
                # 模型空参数：先看最近聊天里有没有提到玩具
                toy = self._context_toy(event)
            if not toy:
                # 再退到最新有图的玩具，别直接说没有
                toys = [t for t in home_mod.snapshot().get("toys", []) if t.get("img")]
                if toys:
                    toy = max(toys, key=lambda t: float(t.get("added_at") or 0))
            if not toy:
                return "玩具架上没有这个玩具"
            img = str(toy.get("img") or "")
            if not img:
                return f"{toy.get('name')}还没有图片"
            path = home_mod.PAGES / img
            if not path.exists():
                return "玩具图片文件不在了"
            await event.send(MessageChain([Image.fromFileSystem(str(path))]))
            self._remember_bot_media(event, "image", path, f"玩具「{toy.get('name')}」")
            return f"已把{toy.get('name')}的图片发出去"
        except Exception as exc:
            logger.debug("[brain] 发送玩具图失败", exc_info=True)
            return f"发送失败：{exc}"

    @filter.llm_tool(name="send_home_picture")
    async def send_home_picture(self, event: AstrMessageEvent, kind: str = "") -> str:
        """把她的房间或她本人的图片发到当前聊天。对方想看房间、你长什么样、自拍时调用；kind 传 room 或 character。"""
        from . import home as home_mod

        try:
            k = str(kind or "").lower()
            text = event.get_message_str() or ""
            if k not in ("room", "character"):
                k = "room" if re.search(r"房间|屋里", text) else "character"
            path = home_mod.PAGES / ("assets/home/room_base.jpg" if k == "room" else "assets/home/character.png")
            if not path.exists():
                return "图片文件不在了"
            await event.send(MessageChain([Image.fromFileSystem(str(path))]))
            self._remember_bot_media(event, "image", path, "房间" if k == "room" else "她的样子")
            return "已把房间图片发出去" if k == "room" else "已把你的样子发出去"
        except Exception as exc:
            logger.debug("[brain] 发送房间图失败", exc_info=True)
            return f"发送失败：{exc}"

    @filter.llm_tool(name="send_toy_selfie")
    async def send_toy_selfie(self, event: AstrMessageEvent, name: str = "") -> str:
        """把她的形象和某个玩具合成一张照片。对方说想看你背着、抱着、拿着某个玩具，或和玩具合照时调用。"""
        from . import home as home_mod
        from PIL import Image as PILImage

        try:
            toy = home_mod.find_toy(name) or home_mod.find_toy_in_text(event.get_message_str() or "")
            if not toy:
                return "玩具架上没有这个玩具"
            img = str(toy.get("img") or "")
            if not img:
                return f"{toy.get('name')}还没有图片"
            toy_path = home_mod.PAGES / img
            char_path = home_mod.PAGES / "assets/home/character.png"
            room_path = home_mod.PAGES / "assets/home/room_base.jpg"
            if not toy_path.exists() or not char_path.exists():
                return "图片文件不在了"
            char = PILImage.open(char_path).convert("RGBA")
            toy_img = PILImage.open(toy_path).convert("RGBA")
            if room_path.exists():
                canvas = PILImage.open(room_path).convert("RGBA")
            else:
                canvas = PILImage.new("RGBA", (900, 1200), (245, 245, 245, 255))
            h = min(800, canvas.height - 60)
            char = char.resize((int(char.width * h / char.height), h), PILImage.LANCZOS)
            canvas.alpha_composite(char, ((canvas.width - char.width) // 2, canvas.height - h))
            tw = 190
            toy_img = toy_img.resize((tw, int(toy_img.height * tw / toy_img.width)), PILImage.LANCZOS)
            canvas.alpha_composite(toy_img, ((canvas.width - tw) // 2 + 10, canvas.height - toy_img.height - 120))
            out = Path(tempfile.gettempdir()) / f"assistant-selfie-{int(time.time())}.jpg"
            canvas.convert("RGB").save(out, quality=92)
            await event.send(MessageChain([Image.fromFileSystem(str(out))]))
            self._remember_bot_media(event, "image", out, f"抱着玩具「{toy.get('name')}」的合照")
            return f"已把{toy.get('name')}和她的合照发出去"
        except Exception as exc:
            logger.debug("[brain] 合成玩具合照失败", exc_info=True)
            return f"合成失败：{exc}"

    @filter.llm_tool(name="draw_picture")
    async def draw_picture(self, event: AstrMessageEvent, prompt: str, size: str = "standard") -> str:
        """把 prompt 画成一张图发给对方。size 默认 standard（512），要高清传 hd（1024）。"""
        try:
            from .imagegen import generate

            base = str(self.config.get("comfy_url") or "http://127.0.0.1:8188")
            path = await asyncio.to_thread(generate, prompt, size, base)
            await event.send(MessageChain([Image.fromFileSystem(str(path))]))
            self._remember_bot_media(event, "image", path, prompt)
            return "画好了，已经发出去"
        except Exception as exc:
            logger.debug("[brain] 生图失败", exc_info=True)
            return f"画图失败：{exc}"

    @filter.llm_tool(name="draw_video")
    async def draw_video(self, event: AstrMessageEvent, prompt: str, seconds: int = 5) -> str:
        """把 prompt 画成一段 5 秒左右的视频发给对方，默认 608x352、带音频。"""
        try:
            from .videogen import generate

            base = str(self.config.get("comfy_url") or "http://127.0.0.1:8188")
            path = await asyncio.to_thread(generate, prompt, seconds, base)
            await event.send(MessageChain([Video.fromFileSystem(str(path))]))
            self._remember_bot_media(event, "video", path, prompt)
            return "视频做好了，已经发出去"
        except Exception as exc:
            logger.debug("[brain] 生视频失败", exc_info=True)
            return f"生视频失败：{exc}"

    @filter.llm_tool(name="rename_clothing")
    async def rename_clothing(self, event: AstrMessageEvent, old_name: str, new_name: str) -> str:
        """给衣柜里的衣服改名字。old_name 是现在的名字，new_name 是新名字。"""
        try:
            from . import home as home_mod
            hit = home_mod.rename_clothing(new_name, old_name=old_name)
            if not hit:
                return "衣柜里没有这件衣服"
            return f"改好了，现在叫「{hit.get('name')}」"
        except Exception as exc:
            logger.debug("[brain] 衣服改名失败", exc_info=True)
            return f"改名失败：{exc}"

    def _pick_clothing(self, event: AstrMessageEvent, name: str = "") -> dict | None:
        from . import home as home_mod

        text = event.get_message_str() or ""
        item = home_mod.find_clothing(name) or home_mod.find_clothing_in_text(text)
        if item:
            return item
        clothes = home_mod.snapshot().get("clothes") or []
        if len(clothes) == 1:
            return clothes[0]
        if re.search(r"那套|这套|新衣服|衣服|换上|快换", text) and clothes:
            return max(clothes, key=lambda c: float(c.get("added_at") or 0))
        return None

    @filter.llm_tool(name="wear_clothing")
    async def wear_clothing(self, event: AstrMessageEvent, name: str = "") -> str:
        """从衣柜里换上一件衣服。name 可以只给前几个字，比如“蓝格子”。"""
        try:
            from . import home as home_mod

            item = self._pick_clothing(event, name)
            if not item:
                return "衣柜里没有这件衣服"
            hit = home_mod.wear_clothing(cloth_id=str(item.get("id") or ""))
            if not hit:
                return "换衣服失败"
            return f"换上了「{hit.get('name')}」"
        except Exception as exc:
            logger.debug("[brain] 换衣服失败", exc_info=True)
            return f"换衣服失败：{exc}"

    @filter.llm_tool(name="try_on")
    async def try_on(self, event: AstrMessageEvent, cloth_name: str = "", prompt: str = "在房间里自然站着，微笑") -> str:
        """先换上指定衣服，再生成一张照片。cloth_name 可以只给前几个字。"""
        try:
            from . import home as home_mod
            item = self._pick_clothing(event, cloth_name)
            if not item:
                return "衣柜里没有这件衣服"
            home_mod.wear_clothing(cloth_id=str(item.get("id") or ""))
            await self.draw_self_room(event, prompt)
            return f"换上「{item.get('name')}」并拍好了"
        except Exception as exc:
            logger.debug("[brain] 换装拍照失败", exc_info=True)
            return f"换装拍照失败：{exc}"

    @filter.llm_tool(name="wear_accessory")
    async def wear_accessory(self, event: AstrMessageEvent, name: str) -> str:
        """从配饰盒里戴上一件配饰。name 是配饰名。"""
        try:
            from . import home as home_mod

            item = home_mod.find_accessory(name) or home_mod.find_accessory_in_text(event.get_message_str() or "")
            if not item:
                return "配饰盒里没有这件"
            hit = home_mod.wear_accessory(acc_id=str(item.get("id") or ""))
            if not hit:
                return "佩戴失败"
            return f"戴上了「{hit.get('name')}」"
        except Exception as exc:
            logger.debug("[brain] 戴配饰失败", exc_info=True)
            return f"戴配饰失败：{exc}"

    @filter.llm_tool(name="take_off_accessory")
    async def take_off_accessory(self, event: AstrMessageEvent, name: str) -> str:
        """摘下某件配饰。name 是配饰名。"""
        try:
            from . import home as home_mod

            item = home_mod.find_accessory(name) or home_mod.find_accessory_in_text(event.get_message_str() or "")
            if not item:
                return "配饰盒里没有这件"
            hit = home_mod.take_off_accessory(acc_id=str(item.get("id") or ""))
            if not hit:
                return "摘配饰失败"
            return f"摘下了「{hit.get('name')}」"
        except Exception as exc:
            logger.debug("[brain] 摘配饰失败", exc_info=True)
            return f"摘配饰失败：{exc}"

    @filter.llm_tool(name="rename_toy")
    async def rename_toy(self, event: AstrMessageEvent, old_name: str, new_name: str) -> str:
        """把玩具架上某个玩具改名字。old_name 是现在的名字，new_name 是新名字。"""
        try:
            from . import home as home_mod

            toy = home_mod.find_toy(old_name)
            if not toy:
                return "玩具架上没有这个玩具"
            hit = home_mod.rename_toy(toy_id=str(toy.get("id") or ""), new_name=new_name)
            if not hit:
                return "改名失败"
            return f"改好了，现在叫「{hit.get('name')}」"
        except Exception as exc:
            logger.debug("[brain] 玩具改名失败", exc_info=True)
            return f"改名失败：{exc}"

    @filter.llm_tool(name="send_video_to")
    async def send_video_to(self, event: AstrMessageEvent, target: str, prompt: str, seconds: int = 5) -> str:
        """生成一段视频并私聊发给指定的人，例如朋友。target 填名字。"""
        try:
            from .media_send import send_private, targets
            from .videogen import generate

            uid = targets(str(self.config.get("media_targets") or "")).get(str(target or "").strip())
            if not uid:
                return f"没有找到 {target} 的私聊 ID"
            base = str(self.config.get("comfy_url") or "http://127.0.0.1:8188")
            path = await asyncio.to_thread(generate, prompt, seconds, base)
            ok = await asyncio.to_thread(send_private, uid, str(path), "video")
            return "视频已经私聊发过去了" if ok else "视频生成了，但发送失败"
        except Exception as exc:
            logger.debug("[brain] 私聊发视频失败", exc_info=True)
            return f"发视频失败：{exc}"

    @filter.llm_tool(name="send_voice_to")
    async def send_voice_to(self, event: AstrMessageEvent, target: str, text: str) -> str:
        """用本地 TTS 生成语音并私聊发给指定的人，例如朋友。target 填名字。"""
        try:
            import subprocess, tempfile, os
            from .media_send import send_private, targets

            uid = targets(str(self.config.get("media_targets") or "")).get(str(target or "").strip())
            if not uid:
                return f"没有找到 {target} 的私聊 ID"
            exe = os.environ.get("ASSISTANT_VOICE", str(WORKSPACE / "bin" / "assistant-voice"))
            out = tempfile.mktemp(suffix=".wav")
            p = await asyncio.to_thread(subprocess.run, [exe, "--out", out, text], capture_output=True, text=True, timeout=120)
            if p.returncode != 0:
                return f"语音生成失败：{p.stderr.strip()[:120]}"
            ok = await asyncio.to_thread(send_private, uid, out, "voice")
            return "语音已经私聊发过去了" if ok else "语音生成了，但发送失败"
        except Exception as exc:
            logger.debug("[brain] 私聊发语音失败", exc_info=True)
            return f"发语音失败：{exc}"

    @filter.llm_tool(name="draw_self_room")
    async def draw_self_room(self, event: AstrMessageEvent, prompt: str = "自然待在房间里") -> str:
        """用参考图和房间背景生成她在房间里的照片，可以指定姿势/场景。"""
        try:
            from . import home as home_mod
            from .imagegen import generate_self

            char = home_mod.PAGES / "assets/home/character_video_ref.png"
            if not Path(char).exists():
                char = home_mod.PAGES / "assets/home/character.png"
            room = home_mod.PAGES / "assets/home/room_base.jpg"
            extras = []
            toy = home_mod.find_toy_in_text(prompt)
            if toy and toy.get("img") and Path(home_mod.PAGES / str(toy["img"])).exists():
                extras.append(str(home_mod.PAGES / str(toy["img"])))
            wearing = home_mod.wearing_clothing()
            outfit = ""
            if wearing:
                prompt += f" She is wearing the outfit「{wearing.get('name')}」."
                if wearing.get("img") and Path(home_mod.PAGES / str(wearing["img"])).exists():
                    outfit = str(home_mod.PAGES / str(wearing["img"]))
            accs = [str(home_mod.PAGES / str(a["img"])) for a in home_mod.wearing_accessories()
                    if a.get("img") and Path(home_mod.PAGES / str(a["img"])).exists()]
            base = str(self.config.get("comfy_url") or "http://127.0.0.1:8188")
            path = await asyncio.to_thread(generate_self, prompt, "standard", base, str(char), str(room), extras, outfit, accs)
            await event.send(MessageChain([Image.fromFileSystem(str(path))]))
            self._remember_bot_media(event, "image", path, f"在房间：{prompt}")
            return "房间照片做好了"
        except Exception as exc:
            logger.debug("[brain] 房间合成失败", exc_info=True)
            return f"房间合成失败：{exc}"

    @filter.llm_tool(name="draw_self")
    async def draw_self(self, event: AstrMessageEvent, prompt: str, size: str = "standard") -> str:
        """用她自己的参考图生图，保持助手的形象。默认 512，高清传 hd。"""
        try:
            from . import home as home_mod
            from .imagegen import generate_self

            ref = str(home_mod.PAGES / "assets/home/character.png")
            extras = []
            toy = home_mod.find_toy_in_text(prompt)
            if toy and toy.get("img") and Path(home_mod.PAGES / str(toy["img"])).exists():
                extras.append(str(home_mod.PAGES / str(toy["img"])))
            wearing = home_mod.wearing_clothing()
            outfit = ""
            if wearing:
                prompt += f" She is wearing the outfit「{wearing.get('name')}」."
                if wearing.get("img") and Path(home_mod.PAGES / str(wearing["img"])).exists():
                    outfit = str(home_mod.PAGES / str(wearing["img"]))
            accs = [str(home_mod.PAGES / str(a["img"])) for a in home_mod.wearing_accessories()
                    if a.get("img") and Path(home_mod.PAGES / str(a["img"])).exists()]
            base = str(self.config.get("comfy_url") or "http://127.0.0.1:8188")
            path = await asyncio.to_thread(generate_self, prompt, size, base, ref, "", extras, outfit, accs)
            await event.send(MessageChain([Image.fromFileSystem(str(path))]))
            self._remember_bot_media(event, "image", path, prompt)
            return "画好了，是她自己的样子"
        except Exception as exc:
            logger.debug("[brain] 参考图生图失败", exc_info=True)
            return f"画图失败：{exc}"

    @filter.llm_tool(name="draw_self_video")
    async def draw_self_video(self, event: AstrMessageEvent, prompt: str, seconds: int = 5) -> str:
        """用她自己的参考图生 5 秒视频，保持助手的形象。"""
        try:
            from . import home as home_mod
            from .videogen import generate_self as generate_self_video

            from .imagegen import generate_self as generate_room_frame

            char = home_mod.PAGES / "assets/home/character_video_ref.png"
            if not Path(char).exists():
                char = home_mod.PAGES / "assets/home/character.png"
            room = home_mod.PAGES / "assets/home/room_base.jpg"
            base = str(self.config.get("comfy_url") or "http://127.0.0.1:8188")
            if Path(room).exists():
                extras = []
                toy = home_mod.find_toy_in_text(prompt)
                if toy and toy.get("img") and Path(home_mod.PAGES / str(toy["img"])).exists():
                    extras.append(str(home_mod.PAGES / str(toy["img"])))
                wearing = home_mod.wearing_clothing()
                outfit = ""
                if wearing:
                    prompt += f" She is wearing the outfit「{wearing.get('name')}」."
                    if wearing.get("img") and Path(home_mod.PAGES / str(wearing["img"])).exists():
                        outfit = str(home_mod.PAGES / str(wearing["img"]))
                accs = [str(home_mod.PAGES / str(a["img"])) for a in home_mod.wearing_accessories()
                        if a.get("img") and Path(home_mod.PAGES / str(a["img"])).exists()]
                frame = await asyncio.to_thread(generate_room_frame, prompt, "standard", base, str(char), str(room), extras, outfit, accs)
                ref = str(frame)
            else:
                ref = str(char)
            path = await asyncio.to_thread(generate_self_video, prompt, seconds, base, ref)
            await event.send(MessageChain([Video.fromFileSystem(str(path))]))
            self._remember_bot_media(event, "video", path, prompt)
            return "视频做好了，是她自己的样子"
        except Exception as exc:
            logger.debug("[brain] 参考图生视频失败", exc_info=True)
            return f"生视频失败：{exc}"

    def _remember_bot_media(self, event: AstrMessageEvent, kind: str, path, note: str = "") -> None:
        """把她自己发出去的图片/视频记进上下文，避免下一轮不认账。"""
        try:
            names = {"image": "图片", "video": "视频", "voice": "语音", "sticker": "表情"}
            label = names.get(kind, kind)
            fname = Path(str(path)).name if path else ""
            text = f"我刚发了一张{label}" + (f"：{note}" if note else "") + (f"（文件 {fname}）" if fname else "")
            self.store.add_raw(
                self._session_key(event), "bot", text, "bot",
                keep=int(self.config.get("raw_keep", 2000) or 2000),
            )
            try:
                self.engine.remember(text, kind="episode", importance=0.32, source="self_media",
                                     visibility="private", meta={"media": kind, "file": fname})
            except Exception:
                pass
        except Exception:
            logger.debug("[brain] 发送媒体记忆失败", exc_info=True)

    @filter.on_llm_response()
    async def capture_reply(self, event: AstrMessageEvent, resp) -> None:
        try:
            if getattr(resp, "is_chunk", False):
                return
            text = (getattr(resp, "completion_text", "") or "").strip()
            if len(text) < 2:
                return
            self.store.add_raw(
                self._session_key(event),
                "bot",
                text,
                "bot",
                keep=int(self.config.get("raw_keep", 2000) or 2000),
            )
            # 面板用：她说这句话期间点亮神经网络
            self._activity_until = time.time() + min(8.0, max(2.0, len(text) / 7.0))
        except Exception:
            logger.debug("[brain] 回复采集失败", exc_info=True)

    @staticmethod
    def _mood_valence() -> float | None:
        """读角色心情，用来做情绪一致检索；读不到就不偏。"""
        try:
            data = json.loads(MOOD_FILE.read_text(encoding="utf-8"))
            return max(-1.0, min(1.0, float(data.get("valence", 0)) / 100.0))
        except Exception:
            return None

    def _rhythm_report(self, limit: int = 400, gap: float = 40.0) -> str:
        """看她一轮发几条，和真人对比。"""
        bot = self.store.recent_raw("bot", limit=limit)
        human = self.store.recent_raw("user", limit=limit)

        seg = [max(1, min(3, len([x for x in (r.get("text") or "").split("\n") if x.strip()]))) for r in bot]

        bursts: list[int] = []
        cur = 0
        last = None
        last_key = None
        for r in human:
            key = str(r.get("session_id") or "")
            ts = float(r.get("created_at") or 0)
            if last is not None and key == last_key and ts - last < gap:
                cur += 1
            else:
                if cur:
                    bursts.append(cur)
                cur = 1
            last, last_key = ts, key
        if cur:
            bursts.append(cur)

        if not seg or not bursts:
            return "【说话节奏】\n样本还不够，多聊一会儿再看。"

        def dist(xs: list[int]) -> str:
            total = len(xs)
            return "，".join(f"{min(i, 3)}条 {xs.count(i) / total:.0%}" for i in (1, 2, 3))

        def capped(xs: list[int]) -> list[int]:
            return [min(x, 3) for x in xs]

        b = capped(seg)
        h = capped(bursts)
        return (
            "【说话节奏】\n"
            f"我（{len(b)} 条回复）：平均 {sum(b) / len(b):.2f} 段，1条 {b.count(1)/len(b):.0%}，"
            f"2条 {b.count(2)/len(b):.0%}，3条+ {b.count(3)/len(b):.0%}\n"
            f"真人（{len(h)} 轮）：平均 {sum(h) / len(h):.2f} 条，1条 {h.count(1)/len(h):.0%}，"
            f"2条 {h.count(2)/len(h):.0%}，3条+ {h.count(3)/len(h):.0%}"
        )

    @staticmethod
    def _visibility_for(session_key: str) -> str:
        return "group" if "GroupMessage" in str(session_key) else "private"

    def _strip_audio(self, req) -> None:
        """DeepSeek 不吃音频输入，把音频摘掉，用我们自己的转写文本。"""
        try:
            if getattr(req, "audio_urls", None):
                req.audio_urls = []
                logger.info("[brain] 已移除音频输入，改用本地转写")
            for msg in getattr(req, "contexts", None) or []:
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for i, part in enumerate(content):
                    if isinstance(part, dict) and str(part.get("type", "")).lower() in ("input_audio", "audio_url"):
                        content[i] = {"type": "text", "text": "[语音消息]"}
        except Exception:
            logger.debug("[brain] 清理音频输入失败", exc_info=True)

    async def _maybe_voice(self, event: AstrMessageEvent) -> str:
        """收到语音：本地转文字，存进记忆，并让这一轮能接话。"""
        if not self.voice_enabled:
            return ""
        try:
            comps = getattr(getattr(event, "message_obj", None), "message", None) or []
            rec = next((c for c in comps if c.__class__.__name__ == "Record"), None)
            if rec is None:
                rec = next(
                    (c for c in comps
                     if c.__class__.__name__ == "File"
                     and voice.is_audio_name(str(getattr(c, "name", "") or getattr(c, "url", "")))),
                    None,
                )
        except Exception:
            return ""
        if rec is None:
            return ""
        mid = str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")
        if mid and mid == self._last_voice_id:
            return self._last_voice_block
        self._last_voice_id = mid
        # face_convert 已经转过就直接用，避免重复跑 whisper
        text = str(event.get_extra("_voice_text") or "").strip()
        if not text:
            path = await voice.save_audio(rec)
            if path is None:
                return ""
            try:
                text = await asyncio.wait_for(voice.transcribe(path, self.voice_model), timeout=90)
            except Exception:
                logger.warning("[brain] 语音转写失败", exc_info=True)
                return ""
        sender = (event.get_sender_name() or "").strip()
        filename = str(getattr(rec, "name", "") or "").strip()
        if len(text) < 2:
            if not filename:
                return ""
            text = ""
        key = self._session_key(event)
        vis = self._visibility_for(key)
        label = f"发来一段音频（{filename}）" if filename else "发来一条语音"
        who = f"{sender}{label}" if sender else f"对方{label}"
        body = text or "没听出内容（可能是纯音乐）"
        vec = await self._embed(body)
        self.engine.remember(
            f"{who}：{body}",
            kind="episode",
            importance=0.45,
            source="voice",
            visibility=vis,
            meta={"chars": len(text), "date": time.strftime("%Y-%m-%d")},
            vector=vec,
        )
        if not (event.get_message_str() or "").strip():
            self._buffers.setdefault(key, []).append((sender or "某人", f"[语音] {text}"))
            if len(self._buffers[key]) >= self.batch_size:
                self._schedule_flush(key)
        block = (
            f"【对方发来的音频】{filename or '语音'}" + (f"，转写：{text[:120]}" if text else "，没听出内容（可能是纯音乐），别装懂")
        )
        self._last_voice_block = block
        self.store.bump_daily("语音")
        logger.info(f"[brain] 音频处理：{filename or '语音'}，{text[:30] or '无转写'}")
        return block

    def _maybe_caption_video(self, event: AstrMessageEvent) -> None:
        """收到视频：抽帧 + 转写音轨，存成记忆。"""
        if not self.video_enabled or self.video_daily <= 0:
            return
        try:
            comps = getattr(getattr(event, "message_obj", None), "message", None) or []
            vid = next((c for c in comps if c.__class__.__name__ == "Video"), None)
        except Exception:
            return
        if vid is None:
            return
        day_start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        if self.store.count_memories(source="video", since=day_start) >= self.video_daily:
            return
        asyncio.create_task(self._caption_video(event, vid))

    async def _caption_video(self, event: AstrMessageEvent, vid) -> None:
        provider = self._get_provider("")
        if provider is None:
            return
        path = await asyncio.to_thread(multimodal.save_media, vid)
        if path is None:
            return
        frames = await asyncio.to_thread(multimodal.video_frames, path)
        if not frames:
            return
        urls = []
        for f in frames[:3]:
            try:
                urls.append("data:image/jpeg;base64," + base64.b64encode(f.read_bytes()).decode())
            except OSError:
                continue
        if not urls:
            return
        try:
            resp = await provider.text_chat(
                prompt=multimodal.VIDEO_PROMPT,
                system_prompt="你是一个只描述画面内容的视觉助手。只输出一段话。",
                image_urls=urls,
            )
            caption = multimodal.clean_caption(getattr(resp, "completion_text", ""), limit=120)
        except Exception as exc:
            if not getattr(self, "_video_warned", False):
                self._video_warned = True
                logger.warning(f"[brain] 视频描述失败：{exc}")
            return
        transcript = ""
        audio = await asyncio.to_thread(multimodal.video_audio, path, 60)
        if audio is not None:
            try:
                transcript = await asyncio.wait_for(voice.transcribe(audio, self.voice_model), timeout=120)
            except Exception:
                transcript = ""
        if not caption and not transcript:
            return
        sender = (event.get_sender_name() or "").strip()
        who = f"{sender}发来一段视频" if sender else "对方发来一段视频"
        text = f"{who}：{caption or '（画面没描述出来）'}"
        if transcript:
            text += f"；里面说：{transcript[:120]}"
        text = text[:300]
        vec = await self._embed(text)
        mid = self.engine.remember(
            text,
            kind="episode",
            importance=0.5,
            source="video",
            visibility=self._visibility_for(self._session_key(event)),
            meta={"caption": caption[:200], "transcript": transcript[:200],
                  "date": time.strftime("%Y-%m-%d")},
            vector=vec,
        )
        try:
            names = self.store.conn.execute("SELECT name, type FROM entities").fetchall()
            for row in names:
                if len(str(row["name"])) >= 2 and str(row["name"]) in (caption + transcript):
                    self.engine.link_entities(mid, [(str(row["name"]), str(row["type"] or "thing"))])
                    break
        except Exception:
            pass
        self.store.bump_daily("视频")
        logger.info(f"[brain] 记住一段视频：{(caption or transcript)[:30]}")

    def _maybe_caption_images(self, event: AstrMessageEvent) -> None:
        """对方发图时后台看图写一句话，存成记忆。"""
        if not self.multimodal_enabled or self.vision_daily <= 0:
            return
        if event.get_group_id() and not self.multimodal_in_group:
            return
        try:
            comps = getattr(getattr(event, "message_obj", None), "message", None) or []
            images = [c for c in comps if c.__class__.__name__ == "Image"]
        except Exception:
            return
        if not images:
            return
        day_start = time.mktime(
            time.localtime()[:3] + (0, 0, 0, 0, 0, -1)
        )
        if self.store.count_memories(source="vision", since=day_start) >= self.vision_daily:
            return
        ref = multimodal.image_ref(images[0])
        if not ref:
            return
        sender = (event.get_sender_name() or "").strip()
        asyncio.create_task(self._caption_image(event, ref, sender))

    async def _caption_image(self, event: AstrMessageEvent, ref: str, sender: str) -> None:
        provider = self._get_provider("")
        if provider is None:
            return
        try:
            resp = await provider.text_chat(
                prompt=multimodal.CAPTION_PROMPT,
                system_prompt=multimodal.CAPTION_SYSTEM,
                image_urls=[ref],
            )
            caption = multimodal.clean_caption(getattr(resp, "completion_text", ""))
        except Exception as exc:
            if not self._vision_warned:
                self._vision_warned = True
                logger.warning(f"[brain] 图片描述失败（首次，可能是模型不支持视觉）：{exc}")
            return
        if len(caption) < 4:
            return
        text = multimodal.to_memory_text(caption, sender)
        for old in self.store.list_memories(source="vision", limit=50):
            if self.engine._similar(text, str(old.get("text") or "")) >= 0.9:
                return
        vec = await self._embed(text)
        memory_id = self.engine.remember(
            text,
            kind="episode",
            importance=0.45,
            source="vision",
            visibility=(
                self._visibility_for(self._session_key(event)) if event is not None else "public"
            ),
            meta={"caption": caption, "sender": sender, "date": time.strftime("%Y-%m-%d")},
            vector=vec,
        )
        # 顺手挂到已有的实体上（只认名字完全出现的，不多花一次模型）
        try:
            names = self.store.conn.execute("SELECT id, name FROM entities").fetchall()
            for row in names:
                name = str(row["name"])
                if len(name) >= 2 and name in caption:
                    self.engine.link_entities(memory_id, [(name, str(row["type"] or "thing"))])
                    break
        except Exception:
            logger.debug("[brain] 图片记忆挂实体失败", exc_info=True)
        self.store.bump_daily("图片")
        logger.info(f"[brain] 记住一张图：{caption[:30]}")

    def _maybe_learn_skill(self, event: AstrMessageEvent) -> None:
        """用户确认完成时，后台把这次做法整理成 skill（不阻塞回复）。"""
        if not self.procedural_enabled:
            return
        text = (event.get_message_str() or "").strip()
        if not text:
            return
        force = bool(self._WORKING_RE.search(text))
        if not force and not procedural.CONFIRM_RE.search(text):
            return
        session = self._session_key(event)
        now = time.time()
        if now - float(self._skill_cooldown.get(session, 0) or 0) < 600:
            return
        rows = self.store.recent_raw("user", limit=40)
        own = [r for r in rows if str(r.get("session_id") or "") == session]
        if len(own) < 3:
            return
        if not force:
            recent_text = " ".join(str(r.get("text") or "") for r in own[-8:])
            if not self._TASK_RE.search(recent_text):
                return
        self._skill_cooldown[session] = now
        asyncio.create_task(self._learn_skill(session))

    async def _learn_skill(self, session: str) -> int:
        provider = self._get_provider("")
        if provider is None:
            return 0
        rows = [r for r in self.store.recent_raw("user", limit=40)
                if str(r.get("session_id") or "") == session][-16:]
        if len(rows) < 3:
            return 0
        try:
            resp = await provider.text_chat(
                prompt=procedural.build_prompt(rows),
                system_prompt=procedural.SYSTEM_PROMPT,
            )
            skill = procedural.normalize(procedural.parse_object(getattr(resp, "completion_text", "")))
        except Exception:
            logger.exception("[brain] 程序记忆抽取失败")
            return 0
        if not skill:
            return 0
        text = procedural.to_text(skill)
        for old in self.store.list_memories(kind="skill", limit=100):
            if self.engine._similar(text, str(old.get("text") or "")) >= 0.6:
                return 0
        vec = await self._embed(text)
        self.engine.remember(
            text,
            kind="skill",
            importance=0.7,
            source="procedural",
            meta={"date": time.strftime("%Y-%m-%d"), "task": skill["task"]},
            entities=[(e["name"], e["type"]) for e in skill["entities"]],
            vector=vec,
        )
        logger.info(f"[brain] 学会做法：{skill['task']}")
        return 1

    def _maybe_correction(self, event: AstrMessageEvent) -> None:
        """用户说“不是/记错了”时，把刚注入的记忆降权，并记一条纠正。"""
        text = (event.get_message_str() or "").strip()
        if not text or not self._correction_re.search(text):
            return
        key = self._session_key(event)
        ids = self._last_hits.get(key) or []
        if not ids:
            return
        for mid in ids[:3]:
            mem = self.store.get_memory(mid)
            if not mem:
                continue
            meta = dict(mem.get("meta") or {})
            meta["corrected"] = True
            self.store.set_meta(mid, meta)
            self.store.conn.execute(
                "UPDATE memories SET importance = MAX(0.05, importance * 0.85) WHERE id=?",
                (mid,),
            )
        self.store.conn.commit()
        try:
            vec = None
            self.engine.remember(
                f"主人纠正过我：{text[:60]}",
                kind="fact",
                importance=0.7,
                source="correction",
                visibility="private",
                meta={"corrected_ids": ids[:3], "date": time.strftime("%Y-%m-%d")},
                vector=vec,
            )
            self.store.bump_daily("纠正")
            logger.info(f"[brain] 记下纠正：{text[:40]}")
        except Exception:
            logger.debug("[brain] 纠正记忆写入失败", exc_info=True)
        self._last_hits.pop(key, None)

    def _capture_working(self, event: AstrMessageEvent) -> None:
        """从用户消息里认领待办；识别收尾就关掉。"""
        try:
            text = (event.get_message_str() or "").strip()
            if len(text) < 4 or text.startswith("/"):
                return
            session = self._session_key(event)
            if working.is_done(text):
                self.store.close_working(session, text)
                return
            for phrase, kind in working.extract(text):
                self.store.add_working(session, phrase, kind)
        except Exception:
            logger.debug("[brain] 工作记忆采集失败", exc_info=True)

    def _working_block(self, event: AstrMessageEvent) -> str:
        try:
            key = self._session_key(event)
            items = self.store.list_working(key, limit=3)
            # 她出门时记的临时笔记，也在聊天里可见
            life = self.store.list_working("life", limit=2)
            if key != "life":
                items = items + life
            return working.build_block(items, limit=3)
        except Exception:
            return ""

    def _style_block(self, event: AstrMessageEvent) -> str:
        if not self.inject_style:
            return ""
        is_group = bool(event.get_group_id())
        is_webchat = "webchat" in str(getattr(event, "unified_msg_origin", "") or "")
        if is_group and not bool(self.config.get("style_in_group", False)):
            return ""
        # 只取「同一个会话」里对方的原话，避免把别人的私聊说法带进来
        session_key = self._session_key(event)
        rows = [
            r for r in self.store.recent_raw("user", limit=self.style_samples * 3)
            if str(r.get("session_id") or "") == session_key
        ][-self.style_samples:]
        stats = style.analyze(rows)
        block = style.build_block(stats)
        if block:
            logger.debug(f"[brain] 语感注入（样本 {stats.get('n')}）")
        return block

    def _speaker_block(self, event: AstrMessageEvent) -> str:
        """群聊里明确“现在是谁在说话”，避免把 A 的事安到 B 头上。"""
        if not self.inject_speaker or not event.get_group_id():
            return ""
        sender = (event.get_sender_name() or "").strip() or "某人"
        sender = self.alias_map.get(sender, sender)
        key = self._session_key(event)
        others: list[str] = []

        def canon(name: str) -> str:
            return self.alias_map.get(name, name)

        for r in self.store.recent_raw("user", limit=200):
            if str(r.get("session_id") or "") != key:
                continue
            name = canon(str(r.get("sender") or "").strip())
            if not re.search(r"[\w\u4e00-\u9fff]", name):
                continue
            if name and name != sender and name != "助手" and name not in others:
                others.append(name)
        line = f"{SPEAKER_MARKER}{sender}"
        if others:
            line += "\n群里其他人：" + "、".join(others[:4]) + "。回谁的话先认准名字。"
        else:
            line += "\n群里可能还有别人，回话前先看名字。"
        return line

    def _context_toy(self, event: AstrMessageEvent) -> dict | None:
        """当前句没点名时，从最近聊天里找刚说过的玩具。"""
        try:
            from . import home as home_mod

            toy = home_mod.find_toy_in_text(event.get_message_str() or "")
            if toy:
                return toy
            key = self._session_key(event)
            rows = self.store.conn.execute(
                "SELECT text FROM raw_messages WHERE session_id=? ORDER BY id DESC LIMIT 12",
                (key,),
            ).fetchall()
            for row in rows:
                toy = home_mod.find_toy_in_text(str(row["text"] or ""))
                if toy:
                    return toy
        except Exception:
            pass
        return None

    async def _maybe_auto_toy(self, event: AstrMessageEvent) -> bool:
        """对方明确要看玩具时，直接发图，不再赌模型会不会调工具。"""
        if not self.auto_toy_image:
            return False
        try:
            text = event.get_message_str() or ""
            if "对方发来一张图片" in text or "请查看图片内容" in text:
                return False
            if re.search(r"玩具架.*(是啥|有什么|哪些|都有啥|都有什么|介绍|里面是)", text):
                return False
            if not re.search(r"看看|给我看|发.*图|图片|长什么样|啥样|样子|拍张|找找|翻翻", text):
                return False
            # 当前句必须真的在说玩具/礼物，不能只凭“看看”就回退到上次的包
            if not re.search(r"玩具|礼物|背包|包|拼图|拼图|皮球|球", text):
                return False
            from . import home as home_mod

            toy = self._context_toy(event)
            if not toy and re.search(r"玩具|礼物", text):
                toys = [t for t in home_mod.snapshot().get("toys", []) if t.get("img")]
                if toys:
                    toy = max(toys, key=lambda t: float(t.get("added_at") or 0))
            img = str((toy or {}).get("img") or "")
            if not toy or not img:
                return False
            path = home_mod.PAGES / img
            if not path.exists():
                return False
            if re.search(r"背着|抱着|拿着|挎着|拎着|合.*照|一起.*拍|合个影|你.*背.*(?:包|玩具)", text):
                await self.send_toy_selfie(event, str(toy.get("name") or ""))
                self._sent_toy = toy
                return False
            key = self._session_key(event)
            now = time.time()
            toy_id = str(toy.get("id") or "")
            last = self._last_auto_toy.get(key)
            if last and last[0] == toy_id and now - last[1] < 45:
                return False
            await event.send(MessageChain([Image.fromFileSystem(str(path))]))
            self._remember_bot_media(event, "image", path, f"玩具「{toy.get('name')}」")
            self._sent_toy = toy
            self._last_auto_toy[key] = (toy_id, now)
            return False
        except Exception:
            logger.debug("[brain] 自动发玩具图失败", exc_info=True)
            return False

    async def _maybe_auto_home(self, event: AstrMessageEvent) -> bool:
        """对方要看房间/她本人时，直接发房间或人物图。"""
        if not self.auto_home_image:
            return False
        try:
            text = event.get_message_str() or ""
            if not re.search(r"看看|给我看|发.*图|图片|长什么样|啥样|样子|拍张|参观", text):
                return False
            if not re.search(r"房间|屋里|你长什么样|自拍|你的照片|你的图|人物形象", text):
                return False
            from . import home as home_mod

            if re.search(r"房间|屋里", text):
                path = home_mod.PAGES / "assets/home/room_base.jpg"
            else:
                path = home_mod.PAGES / "assets/home/character.png"
            if not path.exists():
                return False
            await event.send(MessageChain([Image.fromFileSystem(str(path))]))
            self._remember_bot_media(event, "image", path, "房间" if "房间" in text or "屋里" in text else "她的样子")
            self._sent_home = "房间" if "房间" in text or "屋里" in text else "你的样子"
            return False
        except Exception:
            logger.debug("[brain] 自动发房间图失败", exc_info=True)
            return False

    def _closet_hint(self, event: AstrMessageEvent) -> str:
        try:
            text = event.get_message_str() or ""
            if not re.search(r"衣柜|衣服|裙子|外套|上衣|裤子|鞋|帽|围巾|睡衣|穿|换", text):
                return ""
            from . import home as home_mod

            snap = home_mod.snapshot()
            clothes = snap.get("clothes") or []
            wearing = snap.get("wearing") or {}
            if not clothes:
                return f"{CLOSET_MARKER}你的衣柜还是空的。有人送衣服时会自动挂进去。"
            names = "、".join(str(c.get("name") or "") for c in clothes)
            now = str(wearing.get("name") or "还没换")
            accs = snap.get("accessories") or []
            worn_acc = snap.get("wearing_accessories") or []
            acc_names = "、".join(str(a.get("name") or "") for a in accs) or "空"
            worn_names = "、".join(str(a.get("name") or "") for a in worn_acc) or "没戴"
            return (f"{CLOSET_MARKER}你现在穿的是：{now}。衣柜里有：{names}。"
                    f"配饰盒：{acc_names}；当前佩戴：{worn_names}。"
                    "不用喊全名，说「蓝格子」「那套蓝的」这种短词就行。换衣服调用 wear_clothing；戴配饰调用 wear_accessory；摘掉用 take_off_accessory。"
                    "这些就是最新状态，直接回答，不要再翻文件或数据库。")
        except Exception:
            return ""

    def _rename_hint(self, event: AstrMessageEvent) -> str:
        try:
            text = event.get_message_str() or ""
            if not re.search(r"改名|改个名|改名字|改叫", text):
                return ""
            from . import home as home_mod

            toy = home_mod.find_toy_in_text(text)
            if not toy:
                return ""
            m = re.search(r"(?:叫|改成|改为|成)\s*([\u4e00-\u9fa5A-Za-z0-9]{1,12})", text)
            if not m:
                return ""
            new = m.group(1).strip("的了吧呀啊")
            for suffix in ("好吗", "行吗", "可以吗", "吧", "呢", "啊"):
                if new.endswith(suffix):
                    new = new[:-len(suffix)]
            if not new or new == str(toy.get("name") or ""):
                return ""
            return (f"{RENAME_MARKER}对方想把「{toy.get('name')}」改名为「{new}」。"
                    f"调用 rename_toy 工具，old_name 传「{toy.get('name')}」，new_name 传「{new}」。")
        except Exception:
            return ""

    def _home_hint(self, event: AstrMessageEvent) -> str:
        if self._sent_home:
            return (f"{HOME_MARKER}你刚把{self._sent_home}的图片发给对方了。"
                    f"{self._room_description()}"
                    "用一句自然的话接住，不要问对方要图，也不要假装没发过。")
        return ""

    def _room_description(self) -> str:
        """从 home.json 和 layers 目录生成，不写死家具。"""
        try:
            from . import home as home_mod

            data = home_mod._read()
            room = str(data.get("room") or "你的房间")
            names = []
            layers_dir = home_mod.PAGES / "assets/home/layers"
            if layers_dir.exists():
                for p in sorted(layers_dir.glob("*.png")):
                    name = LAYER_NAMES.get(p.stem)
                    if name and name not in names:
                        names.append(name)
            toys = [str(t.get("name") or "") for t in data.get("toys", []) if t.get("name")]
            parts = [f"你住在一间叫「{room}」的房间里"]
            if names:
                parts.append("里面有：" + "、".join(names))
            if toys:
                parts.append("玩具架上现在有：" + "、".join(toys))
            parts.append(CHARACTER_LOOK)
            return "。".join(parts) + "。"
        except Exception:
            return CHARACTER_LOOK

    def _home_knowledge(self, event: AstrMessageEvent) -> str:
        try:
            text = event.get_message_str() or ""
            if not re.search(r"房间|屋里|你长什么样|你的样子|自拍|人物形象|你的照片|你的图", text):
                return ""
            return (f"{HOME_MARKER}{self._room_description()}"
                    "有人问就照这个说；对方要看图时调用 send_home_picture。")
        except Exception:
            return ""

    def _toy_hint(self, event: AstrMessageEvent) -> str:
        """对方要看玩具时，明确提示模型调用发图工具。"""
        try:
            if self._sent_toy:
                name = str(self._sent_toy.get("name") or "玩具")
                return (f"{TOY_MARKER}你刚把「{name}」的图片发给对方了。"
                        "现在用一句符合当前语气的话接住，不要再说找不到，"
                        "也不要重复“找到了，是这个吧”。")
            text = event.get_message_str() or ""
            if "对方发来一张图片" in text or "请查看图片内容" in text:
                return ""
            from . import home as home_mod

            if re.search(r"玩具架.*(是啥|有什么|哪些|都有啥|都有什么|介绍|里面是)", text) or re.search(r"有(什么|哪些)玩具", text):
                names = "、".join(str(t.get("name") or "") for t in home_mod.snapshot().get("toys", []) if t.get("name"))
                return f"{TOY_MARKER}玩具架上有：{names}。对方是在问有什么，用你自己的话介绍，不要只发一张图。"
            if not re.search(r"看看|给我看|发.*图|图片|长什么样|啥样|样子|拍张", text):
                return ""
            toy = home_mod.find_toy_in_text(text)
            if not toy or not toy.get("img"):
                return ""
            name = str(toy.get("name") or "")
            if re.search(r"背着|抱着|拿着|挎着|拎着|合.*照|一起.*拍|合个影|你.*背.*(?:包|玩具)", text):
                return (f"{TOY_MARKER}对方想看你和「{name}」在一起的样子。"
                        f"调用 send_toy_selfie 工具，name 传「{name}」；不要只发一张商品图。")
            return (f"{TOY_MARKER}对方想看「{name}」。调用 send_toy_picture 工具，"
                    f"name 传「{name}」；不要回答找不到，也不要说自己翻不着。")
        except Exception:
            return ""

    def _webchat_recent_block(self, event: AstrMessageEvent) -> str:
        """桌宠/WebChat 这边带上 QQ 群和主人私聊的最近原话。"""
        try:
            if "webchat" not in str(getattr(event, "unified_msg_origin", "") or ""):
                return ""
            if not self.webchat_share_qq:
                return ""
            owner_id = str(self.config.get("owner_id") or "")
            rows = []
            if owner_id:
                rows += self.store.conn.execute(
                    "SELECT created_at, role, sender, text FROM raw_messages "
                    "WHERE session_id LIKE ? ORDER BY id DESC LIMIT 6",
                    (f"%FriendMessage:{owner_id}",),
                ).fetchall()
            rows += self.store.conn.execute(
                "SELECT created_at, role, sender, text FROM raw_messages "
                "WHERE session_id LIKE '%GroupMessage%' ORDER BY id DESC LIMIT 8"
            ).fetchall()
            if not rows:
                return ""
            rows.sort(key=lambda r: float(r["created_at"] or 0))
            lines = [WEBCHAT_MARKER, "这是 QQ 群和主人私聊里最近的话，桌面这边接着聊，不要说自己没接到："]
            for r in rows[-12:]:
                stamp = datetime.datetime.fromtimestamp(float(r["created_at"] or 0)).strftime("%H:%M")
                who = "助手" if str(r["role"]) == "bot" else str(r["sender"] or "QQ 那边")
                lines.append(f"[{stamp}] {who}: {str(r['text'] or '')[:160]}")
            return "\n".join(lines)
        except Exception:
            return ""

    def _private_recent_block(self, event: AstrMessageEvent) -> str:
        """群聊里带上主人最近私聊的几句话，避免两边说法打架。"""
        try:
            if not event.get_group_id():
                return ""
            if not self.group_recall_private:
                return ""
            owner_id = str(self.config.get("owner_id") or "")
            if not owner_id:
                return ""
            rows = self.store.conn.execute(
                "SELECT created_at, role, text FROM raw_messages "
                "WHERE session_id LIKE ? ORDER BY id DESC LIMIT 6",
                (f"%FriendMessage:{owner_id}",),
            ).fetchall()
            if not rows:
                return ""
            logger.info("[brain] 私聊同步：group=%r rows=%d", event.get_group_id(), len(rows))
            lines = [PRIVATE_MARKER, "这是主人和你在私聊里刚说的话；群里的对话要接得上，但不要主动复述私聊细节："]
            for r in reversed(rows):
                stamp = datetime.datetime.fromtimestamp(float(r["created_at"] or 0)).strftime("%H:%M")
                who = "助手" if str(r["role"]) == "bot" else "主人"
                lines.append(f"[{stamp}] {who}: {str(r['text'] or '')[:160]}")
            return "\n".join(lines)
        except Exception:
            return ""

    def _group_recent_block(self, event: AstrMessageEvent) -> str:
        """主人私聊问群里的事时，把最近群消息直接带进来，不等攒批摘要。"""
        try:
            if "FriendMessage" not in str(getattr(event, "unified_msg_origin", "")):
                return ""
            if not self.private_recall_group:
                return ""
            owner_set = {x.strip() for x in str(self.config.get("owner_names", "")).split(",") if x.strip()}
            is_owner = (
                str(event.get_sender_id()) == str(self.config.get("owner_id") or "")
                or str(event.get_sender_name() or "") in owner_set
            )
            if not is_owner:
                return ""
            text = event.get_message_str() or ""
            if not re.search(r"群|刚才|刚刚|他们|朋友", text):
                return ""
            rows = self.store.conn.execute(
                "SELECT created_at, role, sender, text FROM raw_messages "
                "WHERE session_id LIKE '%GroupMessage%' ORDER BY id DESC LIMIT 8"
            ).fetchall()
            if not rows:
                return ""
            logger.info("[brain] 群同步：owner=%s group=%r text=%r rows=%d", is_owner, event.get_group_id(), text[:40], len(rows))
            lines = [GROUP_MARKER, "这是群里最近的对话，你可以接着聊，不要说自己没接到："]
            for r in reversed(rows):
                stamp = datetime.datetime.fromtimestamp(float(r["created_at"] or 0)).strftime("%H:%M")
                who = "助手" if str(r["role"]) == "bot" else str(r["sender"] or "群里的人")
                lines.append(f"[{stamp}] {who}: {str(r['text'] or '')[:160]}")
            return "\n".join(lines)
        except Exception:
            return ""

    def _gift_block(self, event: AstrMessageEvent) -> str:
        """已经揭晓的秘密礼物：让聊天里的她知道是谁送的，不用再问。"""
        try:
            text = event.get_message_str() or ""
            if not re.search(r"礼物|包|背包|送的|谢谢|谢|朋友|惊喜", text):
                return ""
            from . import home as home_mod

            data = home_mod._read()
            items = [t for t in data.get("toys", []) if t.get("secret_revealed")]
            if not items:
                return ""
            lines = [GIFT_MARKER, "只在对方问起时提，不要主动反复感谢："]
            for toy in items[-3:]:
                when = human_when(float(toy.get("added_at") or 0))
                lines.append(
                    f"- {toy.get('name')}：{toy.get('from')}{when}送的。"
                    f"你看不见字，但记得这句话；有人问就直接说 {toy.get('from')} 和大概时间。"
                    f"你已经谢过，不用每次再谢。"
                )
            return "\n".join(lines)
        except Exception:
            return ""

    def _profile_block(self, event: AstrMessageEvent) -> str:
        """当前说话人 + 话里提到的人，带出对他们的整体印象。"""
        if not self.profile_enabled or not self.inject_profile:
            return ""
        text = (event.get_message_str() or "").strip()
        sender_name = (event.get_sender_name() or "").strip()
        sender_id = str(event.get_sender_id() or "")

        picked: dict[int, dict] = {}
        # 1) 说话人：先按名字找，找不到再按 id 的实体（一般不会有）
        for name in (sender_name, self.alias_map.get(sender_name, "")):
            if not name:
                continue
            row = self.store.find_entity_by_name(name)
            if row and (row.get("profile") or ""):
                picked[int(row["id"])] = row
                break

        # 2) 话里提到的人/话题（最多再加 1 个）
        for row in self.store.profiled_entities():
            if len(picked) >= 2:
                break
            name = str(row.get("name") or "")
            if len(name) < 2 or int(row["id"]) in picked:
                continue
            if name in text:
                picked[int(row["id"])] = row

        if not picked:
            return ""
        lines = [PROFILE_MARKER, "这是你对他们已有的整体印象，聊天时自然用，不要照念，也不要说“根据记忆”。"]
        for row in picked.values():
            lines.append(f"- {row['name']}：{str(row['profile'])[:180]}")
        if event.get_group_id():
            lines.append("群里不要把这些印象说出来。")
        logger.info(f"[brain] 注入画像：{'、'.join(r['name'] for r in picked.values())}")
        return "\n".join(lines)

    def _maybe_decay(self) -> None:
        now = time.time()
        if now - self._last_decay < 86400:
            return
        self._last_decay = now
        task = asyncio.create_task(self._daily_maintenance())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _daily_maintenance(self) -> None:
        """每天一次：衰减、归档、睡觉整理、更新几个画像。"""
        try:
            affected = self.engine.decay(
                float(self.config.get("decay_rate", 0.01) or 0.01),
                access_boost=float(self.config.get("access_decay_boost", 0.5) or 0.5),
                access_window_days=float(self.config.get("access_window_days", 30) or 30),
                protect_names=[x.strip() for x in str(self.config.get("protect_names", "")).split(",") if x.strip()],
                core_floor=float(self.config.get("core_floor", 0.45) or 0.45),
                emotion_factor=float(self.config.get("emotion_decay_factor", 0.4) or 0.4),
            )
            archived = self.engine.archive(
                importance_below=float(self.config.get("archive_below", 0.3) or 0.3),
                older_than_days=float(self.config.get("archive_days", 30) or 30),
                protect_days=float(self.config.get("archive_protect_days", 30) or 30),
            )
            purged = self.engine.purge(
                keep_days=float(self.config.get("archive_keep_days", 180) or 180)
            )
            orphans = self.engine.cleanup_orphans()
            expired = self.store.prune_working()
            conflicts = self._detect_conflicts()
            trimmed = (self.store.trim_kind("fact", int(self.config.get("fact_max", 3000) or 3000))
                       + self.store.trim_kind("lesson", int(self.config.get("lesson_max", 500) or 500)))
            media_count = await self._import_media()
            lesson_count = await self._generate_lessons()
            result = consolidate.sleep(self.store, alias_map=self.alias_map)
            profiles = await self._generate_profiles()
            reflections = await self._generate_reflection()
            facts = await self._generate_facts()
            self._backup_memory()
            logger.info(
                f"[brain] 衰减 {affected} 条，归档 {archived} 条；"
                f"睡觉整理：实体并 {result['entities_merged']}、记忆并 {result['memories_merged']}；"
                f"画像 {profiles} 个、反思 {reflections} 篇、事实 {facts} 条；"
                f"清理归档 {purged} 条、空实体 {orphans} 个、过期待办 {expired} 条、冲突 {conflicts} 组、超限归档 {trimmed} 条、媒体 {media_count} 条、道理 {lesson_count} 条"
            )
        except Exception:
            logger.exception("[brain] 每日维护失败")

    def _backup_memory(self, keep: int = 7) -> None:
        """每日备份：记忆库 + 情绪状态 + 生活层，保留最近 keep 份。"""
        import shutil
        import sqlite3

        try:
            root = Path(__file__).resolve().parents[2] / "backup" / "brain"
            dst = root / time.strftime("%Y%m%d-%H%M")
            dst.mkdir(parents=True, exist_ok=True)
            out = sqlite3.connect(str(dst / "brain.db"))
            self.store.conn.backup(out)
            out.close()
            data = Path(__file__).resolve().parents[2]
            for name in ("mood.json", "affinity.json", "emotional.json", "core_memory.json"):
                src = data / "plugin_data" / "astrbot_plugin_persona_state" / name
                if src.exists():
                    shutil.copy2(src, dst / name)
            life = Path(__file__).resolve().parents[4] / "life"
            for name in ("interests.json", "book_progress.json", "goals.json", "soul.md"):
                src = life / name
                if src.exists():
                    shutil.copy2(src, dst / name)
            backups = sorted([p for p in root.iterdir() if p.is_dir()])
            for old in backups[:-keep]:
                shutil.rmtree(old, ignore_errors=True)
            logger.info(f"[brain] 已备份到 {dst.name}")
        except Exception:
            logger.exception("[brain] 备份失败")

    async def _generate_reflection(self, force_day: float | None = None) -> int:
        """给「最近几天的某一天」写一篇今日三件事；同一天只写一次。"""
        if not self.reflection_enabled:
            return 0
        provider = self._get_provider("")
        if provider is None:
            return 0

        # 已经写过的日子
        done_days = set()
        for m in self.store.list_memories(kind="reflection", limit=60):
            day = (m.get("meta") or {}).get("reflection_date")
            if day:
                done_days.add(str(day))

        now = time.time()

        def day_range(ts: float) -> tuple[str, float, float]:
            t = time.localtime(ts)
            start = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
            return time.strftime("%Y-%m-%d", t), start, start + 86400

        candidates: list[tuple[str, float, float]] = []
        if force_day is not None:
            candidates.append(day_range(force_day))
        else:
            # 今天、昨天、前天，找第一个够料又没写过反思的
            for back in (0, 1, 2):
                day, start, end = day_range(now - back * 86400)
                if day in done_days:
                    continue
                candidates.append((day, start, end))

        for day, start, end in candidates:
            memories = self.store.memories_between(start, end, limit=12)
            if len(memories) < 3:
                continue
            try:
                resp = await provider.text_chat(
                    prompt=reflection.build_prompt(day, memories),
                    system_prompt=reflection.SYSTEM_PROMPT,
                )
                text = reflection.clean(getattr(resp, "completion_text", ""))
                if len(text) < 10:
                    continue
                vec = await self._embed(text)
                self.engine.remember(
                    text,
                    kind="reflection",
                    importance=0.7,
                    source="reflection",
                    visibility="private",
                    meta={"reflection_date": day, "memory_count": len(memories)},
                    vector=vec,
                )
                logger.info(f"[brain] 写下 {day} 的反思：{text[:40]}…")
                return 1
            except Exception:
                logger.exception("[brain] 生成反思失败")
                return 0
        return 0

    async def _import_media(self, limit: int = 20, paths: list[Path] | None = None) -> int:
        """把看过的文章/视频/评论导入记忆（source=media，公开可见）。"""
        production = paths is None
        if production:
            base = Path(os.environ.get("ASSISTANT_SELF_DIR", str(WORKSPACE / "AstrBot" / "data" / "self")))
            paths = [base / "learning_journal.jsonl", base / "share_queue.jsonl"]
        items = media.load_items(list(paths), limit=400)
        if production:
            items += media.load_watched(LIFE_DIR / "watched.jsonl")
        if not items:
            return 0
        known = set()
        for row in self.store.list_memories(source="media", limit=500):
            url = str((row.get("meta") or {}).get("url") or "")
            if url:
                known.add(media.item_key({"url": url}))
        added = 0
        for item in items:
            if added >= limit:
                break
            key = media.item_key(item)
            if not key or key in known:
                continue
            text = media.to_text(item)
            if len(text) < 8:
                continue
            vec = await self._embed(text)
            mid = self.engine.remember(
                text,
                kind="fact",
                importance=0.5,
                source="media",
                visibility="public",
                meta=media.meta_of(item),
                vector=vec,
            )
            try:
                names = self.store.conn.execute("SELECT name, type FROM entities").fetchall()
                for row in names:
                    name = str(row["name"])
                    if len(name) >= 2 and name in text:
                        self.engine.link_entities(mid, [(name, str(row["type"] or "thing"))])
                        break
            except Exception:
                pass
            known.add(key)
            added += 1
        if added:
            self.store.bump_daily("媒体导入", added)
            logger.info(f"[brain] 媒体知识导入 {added} 条")
        if production:
            added += await self._import_life_notes()
        return added

    async def _generate_lessons(self, limit: int = 3) -> int:
        """从最近的笔记和看过的内容里提炼可照着做的道理。"""
        if not self.lessons_enabled:
            return 0
        notes = media.load_life_notes(LIFE_DIR / "notes.jsonl", limit=80)
        picked = [
            n["text"] for n in notes
            if n["kind"] in ("study", "book", "diary") or n["keep"] == "long"
        ][-20:]
        if len(picked) < 3:
            media_rows = self.store.list_memories(source="media", limit=10)
            picked += [r["text"] for r in media_rows][:10]
        if len(picked) < 3:
            return 0
        provider = self._get_provider("")
        if provider is None:
            return 0
        try:
            resp = await provider.text_chat(
                prompt=lessons.build_prompt(picked),
                system_prompt=lessons.SYSTEM_PROMPT,
            )
            items = lessons.normalize(lessons.parse_array(getattr(resp, "completion_text", "")), limit)
        except Exception:
            logger.exception("[brain] 道理提炼失败")
            return 0
        if not items:
            return 0
        existing_rows = self.store.list_memories(source="lesson", limit=100)
        existing = [str(r["text"]) for r in existing_rows]
        lesson_ids = {int(r["id"]) for r in existing_rows}
        try:
            vec_ids, mat = self.store.all_vectors()
        except Exception:
            vec_ids, mat = [], None
        added = 0
        for item in items:
            text = lessons.to_text(item)
            if any(self.engine._similar(text, old) >= 0.75 for old in existing):
                continue
            vec = await self._embed(text)
            if vec is not None and mat is not None and len(vec_ids):
                try:
                    sims = _cosine(vec, mat)
                    if any(
                        float(sims[i]) >= 0.85
                        for i, mid in enumerate(vec_ids) if int(mid) in lesson_ids
                    ):
                        continue
                except Exception:
                    pass
            self.engine.remember(
                text,
                kind="fact",
                importance=0.65,
                source="lesson",
                visibility="private",
                meta={"why": item.get("why") or "", "date": time.strftime("%Y-%m-%d")},
                vector=vec,
            )
            existing.append(text)
            added += 1
            self.store.bump_daily("道理")
            logger.info(f"[brain] 学到一个道理：{item['lesson'][:30]}")
        return added

    async def _import_life_notes(self, limit: int = 20, path: Path | None = None) -> int:
        """她自己的笔记：long 进长期记忆，temp 进 1 天的工作记忆。"""
        notes = media.load_life_notes(path or LIFE_DIR / "notes.jsonl")
        if not notes:
            return 0
        known = set()
        for row in self.store.list_memories(source="life", limit=500):
            ts = str((row.get("meta") or {}).get("ts") or "")
            if ts:
                known.add(ts)
        temp_texts = [str(r.get("text") or "") for r in self.store.list_working("life", limit=50)]
        added = 0
        for note in notes:
            if added >= limit:
                break
            key = f"{note['ts']:.0f}"
            if key in known:
                continue
            if note["keep"] == "long":
                vec = await self._embed(note["text"])
                self.engine.remember(
                    note["text"],
                    kind="fact",
                    importance=0.7 if note["kind"] in ("book", "study") else 0.6,
                    source="life",
                    # 读书/学习笔记两边都能想起来；日记/午睡这类只留在私聊
                    visibility="public" if note["kind"] in ("book", "study") else "private",
                    meta={"ts": key, "kind": note["kind"], "date": note["date"]},
                    vector=vec,
                )
            else:
                if any(self.engine._similar(note["text"], t) >= 0.9 for t in temp_texts):
                    known.add(key)
                    continue
                self.store.add_working("life", note["text"], kind="note", ttl_days=1.0)
                temp_texts.append(note["text"])
            known.add(key)
            added += 1
        if added:
            logger.info(f"[brain] 生活笔记导入 {added} 条")
        try:
            await self._update_reading_state()
        except Exception:
            logger.debug("[brain] 阅读状态更新失败", exc_info=True)
        return added

    async def _update_reading_state(self) -> None:
        """把“最近在读哪本书、读到哪”维护成一条会自己更新的记忆。"""
        import json as _json

        life = Path(__file__).resolve().parents[4] / "life"
        prog_path = life / "book_progress.json"
        try:
            prog = _json.loads(prog_path.read_text(encoding="utf-8"))
        except Exception:
            return
        book = str(prog.get("book") or "").strip()
        if not book:
            return
        offset = int(prog.get("offset") or 0)
        chapter = str(prog.get("chapter") or "").strip()
        text = f"最近在读《{book}》" + (f"，读到{chapter}" if chapter else "") + f"，已读约 {offset} 字。"
        rows = [
            m for m in self.store.list_memories(limit=500)
            if str((m.get("meta") or {}).get("kind") or "") == "reading_state"
        ]
        vec = await self._embed(text)
        if rows:
            mid = int(rows[0]["id"])
            self.store.conn.execute(
                "UPDATE memories SET text=?, importance=?, visibility='public' WHERE id=?",
                (text, 0.75, mid),
            )
            self.store.set_vector(mid, vec)
            meta = dict(rows[0].get("meta") or {})
            meta.update({"kind": "reading_state", "pin": True, "book": book,
                         "chapter": chapter, "offset": offset, "updated_at": time.time()})
            self.store.set_meta(mid, meta)
        else:
            self.engine.remember(
                text, kind="fact", importance=0.75, source="life", visibility="public",
                meta={"kind": "reading_state", "pin": True, "book": book,
                      "chapter": chapter, "offset": offset, "updated_at": time.time()},
                vector=vec,
            )
        self.store.conn.commit()

    def _detect_conflicts(self) -> int:
        """同一实体的两条事实极性相反时记一笔，注入/后台可查。"""
        facts = self.store.list_memories(kind="fact", limit=300)
        rows = []
        for m in facts:
            ids = {
                int(r["entity_id"])
                for r in self.store.conn.execute(
                    "SELECT entity_id FROM memory_entities WHERE memory_id=?", (int(m["id"]),)
                )
            }
            if ids:
                rows.append({"id": int(m["id"]), "text": str(m["text"]), "entity_ids": ids})
        found = conflict.find(rows)
        added = 0
        for a_id, b_id, reason in found:
            if self.store.add_conflict(a_id, b_id, reason):
                for mid, other in ((a_id, b_id), (b_id, a_id)):
                    mem = self.store.get_memory(mid)
                    meta = dict((mem or {}).get("meta") or {})
                    meta["conflict_with"] = other
                    self.store.set_meta(mid, meta)
                added += 1
                self.store.bump_daily("冲突")
                logger.info(f"[brain] 发现冲突记忆：#{a_id} ↔ #{b_id}（{reason}）")
        return added

    async def _generate_facts(self, force: bool = False) -> int:
        """从今天的 episode 里抽长期事实；同一条事实只写一次。"""
        if not self.abstract_enabled:
            return 0
        provider = self._get_provider("")
        if provider is None:
            return 0

        now = time.time()
        done_dates = {
            str((m.get("meta") or {}).get("date") or "")
            for m in self.store.list_memories(kind="fact", limit=300)
        }
        episodes: list[dict] = []
        start = 0.0
        day_label = ""
        # 今天 → 昨天 → 前天，找第一个够料且还没抽过的
        for back in (0, 1, 2):
            t = time.localtime(now - back * 86400)
            start = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
            day_label = time.strftime("%Y-%m-%d", t)
            if day_label in done_dates:
                continue
            episodes = [
                m for m in self.store.memories_between(start, start + 86400, limit=24)
                if m.get("kind") in ("episode", "reflection")
            ]
            if len(episodes) >= 3:
                break
        if len(episodes) < 3:
            return 0

        try:
            resp = await provider.text_chat(
                prompt=abstract.build_prompt(episodes),
                system_prompt=abstract.SYSTEM_PROMPT,
            )
            items = abstract.normalize(abstract.parse_array(getattr(resp, "completion_text", "")), self.abstract_max)
        except Exception:
            logger.exception("[brain] 事实抽取失败")
            return 0

        existing = [str(m.get("text") or "") for m in self.store.list_memories(kind="fact", limit=300)]
        def norm(x: str) -> str:
            return re.sub(r"[\s\W_]+", "", x).lower()

        created = 0
        # 私聊里抽出来的事实不外露；只有来源全是群聊的才进群
        vis = "group" if all(str(m.get("visibility") or "") == "group" for m in episodes) else "private"
        source_ids = [m["id"] for m in episodes[:12]]
        for item in items:
            key = norm(item["fact"])
            if any(key in norm(e) or norm(e) in key for e in existing if e):
                continue
            vec = await self._embed(item["fact"])
            self.engine.remember(
                item["fact"],
                kind="fact",
                importance=float(item["importance"]),
                source="abstract",
                visibility=vis,
                meta={
                    "source_ids": source_ids,
                    "date": day_label,
                    "session": str(episodes[0].get("source") or ""),
                    "subject": (item["entities"][0]["name"] if item.get("entities") else ""),
                    "abstracted_at": now,
                    "abstracted_by": "auto",
                },
                entities=[(e["name"], e["type"]) for e in item["entities"]],
                vector=vec,
            )
            existing.append(item["fact"])
            created += 1
            self.store.bump_daily("事实")
            logger.info(f"[brain] 沉淀事实：{item['fact'][:40]}")
        return created

    async def _generate_profiles(self, limit: int | None = None, force_id: int | None = None) -> int:
        """给最活跃的实体生成/更新画像；一天最多 profile_max 个。"""
        if not self.profile_enabled:
            return 0
        provider = self._get_provider("")
        if provider is None:
            return 0

        if force_id is not None:
            row = self.store.get_entity(force_id)
            rows = [row] if row else []
        else:
            rows = self.store.entities_needing_profile(
                min_degree=self.profile_min_degree,
                older_than=self.profile_days * 86400,
                limit=limit or self.profile_max,
            )

        done = 0
        for row in rows:
            try:
                memories = self.store.memories_of_entity(int(row["id"]), limit=10)
                mem_items = [
                    {
                        "text": m["text"],
                        "date": time.strftime("%Y-%m-%d", time.localtime(float(m["created_at"]))),
                    }
                    for m in memories
                ]
                rels = self.store.entity_relations(int(row["id"]))
                prompt = profile.build_prompt(row["name"], row.get("type") or "person", mem_items, rels)
                resp = await provider.text_chat(
                    prompt=prompt, system_prompt=profile.SYSTEM_PROMPT
                )
                text = profile.clean(getattr(resp, "completion_text", ""))
                if len(text) < 10:
                    continue
                self.store.set_profile(int(row["id"]), text)
                done += 1
                logger.info(f"[brain] 画像更新：{row['name']}（{text[:30]}…）")
            except Exception:
                logger.exception(f"[brain] 生成画像失败：{row.get('name')}")
        return done

    # ---------------------------------------------------------------- 命令

    @filter.command("大脑", alias={"记忆库"})
    async def cmd_brain(self, event: AstrMessageEvent):
        """看记忆库状态。用法：/大脑；/大脑 检索 关键词；/大脑 整理"""
        parts = (event.message_str or "").strip().split()
        sub = parts[1] if len(parts) > 1 else ""

        if sub in {"检索", "搜"}:
            query = " ".join(parts[2:])
            if not query:
                yield event.plain_result("用法：/大脑 检索 关键词")
                return
            vec = await self._embed(query)
            hits = self.engine.recall(query, k=5, query_vec=vec, touch=False, explain=True)
            if not hits:
                yield event.plain_result("没想起来相关的。")
                return
            lines = ["想起来这些："]
            for h in hits:
                why = h.get("why", {})
                lines.append(
                    f"#{h['id']} [{h['score']}] {h['text'][:60]}"
                    f"（语义 {why.get('semantic')} 关键词 {why.get('keyword')} 图谱 {why.get('graph')}）"
                )
            yield event.plain_result("\n".join(lines)[:3000])
            return

        if sub in {"体检", "健康", "health"}:
            yield event.plain_result(health.report(self.store))
            return

        if sub in {"自省", "复盘", "review"}:
            provider = self._get_provider("")
            if provider is None:
                yield event.plain_result("现在没有可用的模型，回头再复盘。")
                return
            lessons_rows = self.store.list_memories(source="lesson", limit=10)
            eps = self.store.list_memories(kind="episode", limit=10)
            body = "学到的道理：" + "；".join(r["text"][:40] for r in lessons_rows) + "\n最近的事：" + "；".join(r["text"][:40] for r in eps)
            try:
                resp = await provider.text_chat(
                    prompt=("下面是一个角色学到的道理和最近的事。用 2–3 句做一次自省：哪条道理这周真用上了？"
                            "哪条没做到？只输出自省本身，不要客套。\n" + body),
                    system_prompt="你是助手，在写给自己看的一页复盘。",
                )
                text = (getattr(resp, "completion_text", "") or "").strip()[:300]
            except Exception as exc:
                yield event.plain_result(f"复盘失败：{exc}")
                return
            if text:
                self.engine.remember(text, kind="reflection", importance=0.6,
                                     source="review", visibility="private",
                                     meta={"date": time.strftime("%Y-%m-%d")})
                self.store.bump_daily("复盘")
            yield event.plain_result(text or "这周好像没什么可复盘的。")
            return

        if sub in {"报表", "运行", "report"}:
            yield event.plain_result(build_report(self.store))
            return

        if sub in {"道理", "学到", "lesson"}:
            n = await self._generate_lessons()
            rows = self.store.list_memories(source="lesson", limit=8)
            yield event.plain_result(
                (f"新学到 {n} 条。\n" if n else "")
                + ("\n".join(f"- {r['text'][:50]}" for r in rows) or "还没提炼出道理。")
            )
            return

        if sub in {"笔记", "生活", "life"}:
            n = await self._import_life_notes()
            rows = self.store.list_memories(source="life", limit=5)
            tmps = self.store.list_working("life", limit=3)
            lines = []
            if rows:
                lines.append("长期记住的：")
                lines += [f"- {r['text'][:50]}" for r in rows]
            if tmps:
                lines.append("临时记着（1 天）：")
                lines += [f"- {t['text'][:50]}" for t in tmps]
            yield event.plain_result(
                (f"导入了 {n} 条。\n" if n else "") + ("\n".join(lines) or "还没有生活笔记。")
            )
            return

        if sub in {"节奏", "分段"}:
            yield event.plain_result(self._rhythm_report())
            return

        if sub in {"媒体", "看过"}:
            n = await self._import_media()
            rows = self.store.list_memories(source="media", limit=5)
            yield event.plain_result(
                (f"导入了 {n} 条。" if n else "没有新的可导入。")
                + ("\n最近看过的：\n" + "\n".join(f"- {r['text'][:50]}" for r in rows) if rows else "")
            )
            return

        if sub in {"图", "图片", "看图"}:
            rows = self.store.list_memories(source="vision", limit=5)
            if not rows:
                yield event.plain_result("还没记住过图片。")
            else:
                yield event.plain_result("记住的图：\n" + "\n".join(f"- {r['text'][:50]}" for r in rows))
            return

        if sub in {"冲突", "矛盾"}:
            groups = self.store.list_conflicts(limit=10)
            if not groups:
                yield event.plain_result("没有发现互相矛盾的事实。")
            else:
                lines = []
                for g in groups:
                    a = (g.get("a") or {}).get("text", "")
                    b = (g.get("b") or {}).get("text", "")
                    lines.append(f"#{g['id']} {a[:24]} ⇄ {b[:24]}")
                yield event.plain_result("发现这些矛盾：\n" + "\n".join(lines))
            return

        if sub in {"技能", "做法", "skill"}:
            skills = self.store.list_memories(kind="skill", limit=10)
            if not skills:
                yield event.plain_result("还没学会什么做法。")
            else:
                yield event.plain_result(
                    "学会的做法：\n" + "\n".join(f"- {s['text'][:60]}" for s in skills)
                )
            return

        if sub in {"待办", "事", "todo"}:
            items = self.store.list_working(self._session_key(event), limit=10)
            if not items:
                yield event.plain_result("现在没有记挂的事。")
            else:
                yield event.plain_result(
                    "记挂着：\n" + "\n".join(f"- {i['text']}" for i in items)
                )
            return

        if sub in {"语感", "语气"}:
            human = style.analyze(self.store.recent_raw("user", limit=self.style_samples))
            bot = style.analyze(self.store.recent_raw("bot", limit=self.style_samples))
            def fmt(x):
                if not x or x.get("n", 0) == 0:
                    return "样本不足"
                return (f"中位 {int(x['median_len'])} 字、短句 {int(x['short_ratio']*100)}%、"
                        f"句尾无标点 {int(x['no_punct_ratio']*100)}%、"
                        f"QQ表情 {int(x['qq_face_ratio']*100)}%、emoji {int(x['emoji_ratio']*100)}%")
            yield event.plain_result(
                f"真人（{human.get('n', 0)} 条）：{fmt(human)}\n"
                f"我（{bot.get('n', 0)} 条）：{fmt(bot)}"
            )
            return

        if sub in {"抽象", "沉淀", "事实"}:
            done = await self._generate_facts()
            yield event.plain_result(f"沉淀了 {done} 条事实。" if done else "今天没有可沉淀的，或已经抽过了。")
            return

        if sub in {"反思", "回顾", "日记"}:
            done = await self._generate_reflection()
            yield event.plain_result("写了一篇。" if done else "今天还没有足够的记忆，或已经写过了。")
            return

        if sub in {"画像", "印象"}:
            done = await self._generate_profiles(limit=self.profile_max)
            yield event.plain_result(
                f"更新了 {done} 个画像。" if done else "没有需要更新的画像（或没有可用的模型）。"
            )
            return

        if sub in {"睡觉", "睡眠", "深度整理"}:
            result = consolidate.sleep(self.store, alias_map=self.alias_map)
            yield event.plain_result(
                f"睡了一觉：实体合并 {result['entities_merged']} 个，"
                f"记忆合并 {result['memories_merged']} 条（检查 {result['pairs_checked']} 对）"
            )
            return

        if sub in {"整理", "维护"}:
            affected = self.engine.decay(
                float(self.config.get("decay_rate", 0.01) or 0.01),
                access_boost=float(self.config.get("access_decay_boost", 0.5) or 0.5),
                access_window_days=float(self.config.get("access_window_days", 30) or 30),
                protect_names=[x.strip() for x in str(self.config.get("protect_names", "")).split(",") if x.strip()],
                core_floor=float(self.config.get("core_floor", 0.45) or 0.45),
                emotion_factor=float(self.config.get("emotion_decay_factor", 0.4) or 0.4),
            )
            archived = self.engine.archive(
                importance_below=float(self.config.get("archive_below", 0.3) or 0.3),
                older_than_days=float(self.config.get("archive_days", 30) or 30),
                protect_days=float(self.config.get("archive_protect_days", 30) or 30),
            )
            purged = self.engine.purge(
                keep_days=float(self.config.get("archive_keep_days", 180) or 180)
            )
            orphans = self.engine.cleanup_orphans()
            expired = self.store.prune_working()
            conflicts = self._detect_conflicts()
            trimmed = (self.store.trim_kind("fact", int(self.config.get("fact_max", 3000) or 3000))
                       + self.store.trim_kind("lesson", int(self.config.get("lesson_max", 500) or 500)))
            media_count = await self._import_media()
            lesson_count = await self._generate_lessons()
            yield event.plain_result(f"衰减 {affected} 条，归档 {archived} 条。")
            return

        s = self.engine.stats()
        kinds = "、".join(f"{k} {v}" for k, v in s["kinds"].items()) or "空"
        age = "——"
        if s["oldest"]:
            age = time.strftime("%Y-%m-%d", time.localtime(s["oldest"]))
        yield event.plain_result(
            f"记忆 {s['active']} 条（归档 {s['archived']}）｜实体 {s['entities']}｜关系 {s['relations']}\n"
            f"类型：{kinds}\n平均重要性 {s['avg_importance']}｜最早一条 {age}"
        )
