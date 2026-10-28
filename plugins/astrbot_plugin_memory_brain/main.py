# -*- coding: utf-8 -*-
"""记忆大脑：自己实现的长期记忆（存储 + 检索 + 遗忘），不依赖任何记忆插件。

这个文件只做 AstrBot 接线：
- 收消息 → 按会话攒成「片段」→ 写进记忆库
- 每轮请求 → 混合检索 top-k → 注入 system_prompt
- 每天一次 → 衰减 + 归档

记忆策略在 engine.py，存储细节在 store.py，可以单独测试。
"""

from __future__ import annotations
# 路径都从 AstrBot 数据目录推导，不写死个人机器
import os
from pathlib import Path as _P

_DATA = _P(__file__).resolve().parents[2]
_LIFE_DIR = _P(os.environ.get("ASTRBOT_LIFE_DIR", str(_DATA.parent / "life")))


import asyncio
import base64
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from . import (
    abstract, conflict, consolidate, extractor, media, multimodal, procedural, profile,
    health, lessons, reflection, style, voice, working,
)
from .report import build as build_report

MOOD_FILE = (_DATA / "plugin_data/astrbot_plugin_persona_state/mood.json")
from .engine import MemoryEngine, _cosine
from .store import Store

PLUGIN_NAME = "astrbot_plugin_memory_brain"
MARKER = "【记忆片段】"
SPEAKER_MARKER = "【当前说话人】"
VOICE_MARKER = "【对方这条语音说的是】"
PROFILE_MARKER = "【对他们的印象】"
WORKING_MARKER = "【还没做完的事】"
STYLE_MARKER = "【最近的聊天习惯】"

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
        self.inject_speaker = bool(self.config.get("inject_speaker", True))
        self.profile_max = max(1, int(self.config.get("profile_max_per_run", 5) or 5))
        self.profile_min_degree = max(1, int(self.config.get("profile_min_degree", 2) or 2))
        try:
            self.profile_days = float(self.config.get("profile_interval_days", 7) or 7)
        except (TypeError, ValueError):
            self.profile_days = 7.0

        self._buffers: dict[str, list[tuple[str, str]]] = {}
        self._tasks: set[asyncio.Task] = set()
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
            self._maybe_correction(event)
            self._capture_raw(event)
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
            if self.inject_speaker:
                speaker_block = self._speaker_block(event)
                if speaker_block:
                    blocks.append(speaker_block)
            if self.inject_profile:
                profile_block = self._profile_block(event)
                if profile_block:
                    blocks.append(profile_block)
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
            base = req.system_prompt or ""
            cuts = [base.find(m) for m in (
                MARKER, SPEAKER_MARKER, PROFILE_MARKER, STYLE_MARKER, WORKING_MARKER,
                VOICE_MARKER
            )]
            cuts = [c for c in cuts if c != -1]
            if cuts:
                base = base[: min(cuts)].rstrip()
            req.system_prompt = (base + "\n\n" + body).strip()
        except Exception:
            logger.exception("[brain] 注入失败")

    # 这些短句本身没有检索价值，单发时不查记忆
    _TASK_RE = re.compile(
        r"帮|做|弄|改|写|查|整理|导|剪|画|发|建|修|买|订|转|生成|校对|核对|排版|翻译"
    )
    _WORKING_RE = re.compile(r"以后都|以后按|记住这个做法|下次也这么|就这么办")

    _GENERIC = {"在吗", "在不在", "嗯", "哦", "好", "好吧", "早安", "晚安", "哈哈", "哈", "?", "？", "...", "……"}

    async def _memory_block(self, event: AstrMessageEvent) -> str:
        current = (event.get_message_str() or "").strip()
        if not current:
            return ""
        is_group = bool(event.get_group_id())
        if is_group and not self.inject_in_group:
            return ""

        # 用最近两句话补全检索语境（“在吗”这种短句不单独查）
        key = self._session_key(event)
        recent = [m[1] for m in self._buffers.get(key, [])[-2:]]
        context = " ".join(r[:60] for r in recent if r and r != current).strip()
        if current in self._GENERIC and not context:
            return ""
        query = (context + " " + current).strip()[:200]

        vec = await self._embed(query)
        k = int(self.config.get("recall_k_group", 1) or 1) if is_group else self.k
        min_score = (
            float(self.config.get("recall_min_score_group", 0.45) or 0.45)
            if is_group else self.recall_min_score
        )
        allow = {"public", "group"} if is_group else {"public", "private"}
        hits = self.engine.recall(
            query, k=k, query_vec=vec,
            rerank=bool(self.config.get("rerank", True)),
            visibility_allow=allow,
            mood_valence=self._mood_valence(),
        )
        # 分数太低的宁可不注入：省 token，也减少“乱提旧事”
        hits = [h for h in hits if float(h.get("score", 0)) >= min_score]
        if not hits:
            return ""
        lines = [MARKER, "以下是和当前话题相关的旧记忆，用来接话，别逐条复述："]
        for i, h in enumerate(hits, 1):
            stamp = time.strftime("%m-%d", time.localtime(float(h.get("created_at") or time.time())))
            lines.append(f"{i}. [{stamp}] {h['text'][:160]}")
        block = "\n".join(lines)
        self._last_hits[key] = [int(h["id"]) for h in hits]
        self.store.bump_daily("记忆注入")
        logger.info(f"[brain] 注入记忆 {len(hits)} 条（{len(block)} 字）")
        return block

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
        except Exception:
            logger.debug("[brain] 回复采集失败", exc_info=True)

    @staticmethod
    def _mood_valence() -> float | None:
        """读角色心情，用来做情绪一致检索；读不到就不偏。"""
        try:
            import json
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
                f"管理员纠正过我：{text[:60]}",
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
            if name and name != sender and name != "角色" and name not in others:
                others.append(name)
        line = f"{SPEAKER_MARKER}{sender}"
        if others:
            line += "\n群里其他人：" + "、".join(others[:4]) + "。回谁的话先认准名字。"
        else:
            line += "\n群里可能还有别人，回话前先看名字。"
        return line

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
            logger.info(
                f"[brain] 衰减 {affected} 条，归档 {archived} 条；"
                f"睡觉整理：实体并 {result['entities_merged']}、记忆并 {result['memories_merged']}；"
                f"画像 {profiles} 个、反思 {reflections} 篇、事实 {facts} 条；"
                f"清理归档 {purged} 条、空实体 {orphans} 个、过期待办 {expired} 条、冲突 {conflicts} 组、超限归档 {trimmed} 条、媒体 {media_count} 条、道理 {lesson_count} 条"
            )
        except Exception:
            logger.exception("[brain] 每日维护失败")

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
            base = (_DATA / "self")
            paths = [base / "learning_journal.jsonl", base / "share_queue.jsonl"]
        items = media.load_items(list(paths), limit=400)
        if production:
            items += media.load_watched((_LIFE_DIR / "watched.jsonl"))
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
        notes = media.load_life_notes((_LIFE_DIR / "notes.jsonl"), limit=80)
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
        notes = media.load_life_notes(path or (_LIFE_DIR / "notes.jsonl"))
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
                    importance=0.6,
                    source="life",
                    visibility="private",
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
        return added

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
                meta={"source_ids": source_ids, "date": day_label},
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
                    system_prompt="你是角色，在写给自己看的一页复盘。",
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
