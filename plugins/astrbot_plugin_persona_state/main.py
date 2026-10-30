# -*- coding: utf-8 -*-
"""角色状态与情绪：情绪三轴 + 好感度 + 情绪记忆（旧账）+ 核心记忆（识海）。

一个插件，四份数据，自带管理页面，不依赖任何其它记忆插件：
- 状态与好感：`data/plugin_data/<本插件>/mood.json`、`affinity.json`
- 旧账：`emotional.json`（只在情绪大波动时写，平时不注入）
- 识海：`core_memory.json`（被授权者说「永远记住」才写，永远注入）

设计目标：
- 平时几乎不吃 token：只有状态块（一两行）+ 识海块；旧账按概率才出现。
- 不碰别人的数据：只写自己的 plugin_data 目录，不读不写其它插件。
- 可以和现有的角色插件共存：只往 system_prompt 末尾追加自己的块。
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import CustomFilter
from astrbot.api.star import Context, Star, register

from . import emotional
from .core_state import (
    JsonStore,
    affinity_level,
    affinity_view,
    apply_message,
    clamp,
    decay_mood,
    default_affinity,
    default_mood,
    mood_labels,
    mood_view,
    touch_affinity,
)
from . import emotion
from .core_store import CoreMemoryStore, MARKER as CORE_MARKER, TRIGGERS, extract_trigger

PLUGIN_NAME = "astrbot_plugin_persona_state"
MARKER_STATE = "【角色当前状态】"
MARKER_GRUDGE = emotional.MARKER
SAVE_INTERVAL = 20.0

_ACTIVE: "PersonaStatePlugin | None" = None


def _empty_emotional() -> dict:
    return {"version": 1, "events": [], "next_id": 1}


def _fmt(value: float) -> str:
    return str(int(round(float(value))))


def _split_list(raw: Any) -> list[str]:
    return [p.strip() for p in re.split(r"[,，\n]", str(raw or "")) if p.strip()]


def _parse_allowed(raw: Any) -> dict[str, str]:
    """把 "id:名字, id2:名字2" 解析成 {id: 名字}；只写 id 时名字等于 id。"""
    out: dict[str, str] = {}
    for part in _split_list(raw):
        bits = re.split(r"[:：]", part, maxsplit=1)
        key = bits[0].strip()
        name = bits[1].strip() if len(bits) > 1 else key
        if key:
            out[key] = name or key
    return out


def _parse_floors(raw: Any) -> dict[str, float]:
    out: dict[str, float] = {}
    for part in _split_list(raw):
        bits = re.split(r"[:：=]", part, maxsplit=1)
        if len(bits) != 2:
            continue
        try:
            out[bits[0].strip()] = clamp(float(bits[1].strip()))
        except ValueError:
            continue
    return out


def _data_dir() -> Path:
    """插件自己的数据目录，兜底回到 AstrBot data 下。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        base = Path(get_astrbot_data_path())
    except Exception:
        base = Path("data").resolve()
    return base / "plugin_data" / PLUGIN_NAME


class CoreCaptureFilter(CustomFilter):
    """被授权者说「永远记住」时写入识海；群里只记录不唤醒。"""

    def __init__(self, raise_error: bool = False, **kwargs) -> None:
        super().__init__(raise_error=raise_error, **kwargs)

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        plugin = _ACTIVE
        if not plugin or not plugin.allowed:
            return False
        try:
            sender = str(event.get_sender_id() or "")
            if sender not in plugin.allowed:
                return False
            text = (event.get_message_str() or "").strip()
            if not text or text.startswith("/"):
                return False
            group = event.get_group_id()
            if group and not plugin.capture_in_group:
                return False
            plugin.capture_core(sender, text, event)
            return not bool(group)  # 私聊放行让她回话，群聊不叫醒
        except Exception:
            logger.exception("[state] 识海捕获失败")
            return False


@register(
    PLUGIN_NAME,
    "persona-state",
    "角色状态与情绪：情绪值、好感度、旧账、识海，自带管理页面，不依赖其它记忆插件",
    "1.0.0",
)
class PersonaStatePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        global _ACTIVE
        _ACTIVE = self
        base = _data_dir()
        base.mkdir(parents=True, exist_ok=True)

        self.base = base
        self._last_hist = 0.0
        self.mood = JsonStore(base / "mood.json", default_mood)
        self.affinity = JsonStore(base / "affinity.json", default_affinity)
        self.emotional = JsonStore(base / "emotional.json", _empty_emotional)
        try:
            self.core_max_chars = int(self.config.get("core_max_chars", 2400) or 2400)
        except (TypeError, ValueError):
            self.core_max_chars = 2400
        self.core = CoreMemoryStore(base / "core_memory.json", max_inject_chars=self.core_max_chars)

        self.allowed = _parse_allowed(self.config.get("allowed_ids", ""))
        self.floors = _parse_floors(self.config.get("floors", ""))
        self.inject_enabled = bool(self.config.get("inject", True))
        self.inject_in_group = bool(self.config.get("inject_in_group", True))
        self.enable_tools = bool(self.config.get("enable_tools", True))
        self.emotional_enabled = bool(self.config.get("emotional_memory", True))
        try:
            self.grudge = max(0.0, min(100.0, float(self.config.get("grudge", 45) or 45)))
        except (TypeError, ValueError):
            self.grudge = 45.0

        self._last_msg_key: tuple | None = None
        self._last_save = 0.0
        self._last_delta = 0.0
        self._recall_block = ""
        self._neg_window: list[tuple[float, float]] = []
        self._page_api = None
        self.capture_in_group = bool(self.config.get("capture_in_group", True))
        self._pending_confirm: dict | None = None

    # ---------------------------------------------------------------- lifecycle

    async def initialize(self) -> None:
        view = mood_view(self.mood.data, time.time())
        logger.info(
            f"[state] 已启动：心情 {_fmt(view['valence'])} 精力 {_fmt(view['energy'])} "
            f"黏人 {_fmt(view['clinginess'])}；好感 {len(self.affinity.data.get('users', {}))} 人；"
            f"旧账 {len(self.emotional.data.get('events', []))} 条；识海 {len(self.core.active())} 条"
        )
        try:
            from .page_api import PageApi

            self._page_api = PageApi(self)
            self._page_api.register_routes()
        except Exception as exc:  # 老版本 AstrBot 没有页面 API，不影响聊天
            logger.warning(f"[state] 管理页面注册失败（不影响聊天）：{exc}")

    async def terminate(self) -> None:
        self._save()

    # ---------------------------------------------------------------- 识海捕获

    @filter.custom_filter(CoreCaptureFilter, False)
    async def capture_hook(self, event: AstrMessageEvent):
        """捕获由 filter 完成；这里只占位，不阻断正常流程。"""
        return

    def capture_core(self, sender: str, text: str, event: AstrMessageEvent) -> None:
        core = extract_trigger(text, TRIGGERS)
        if not core:
            return
        who = self.allowed.get(sender, sender)
        source = "群聊" if event.get_group_id() else "私聊"
        entry, created = self.core.add(core, who, sender, source)
        if created and entry:
            self._pending_confirm = {"by_id": sender, "text": entry["text"], "ts": time.time()}
            logger.info(f"[state] 识海写入 #{entry['id']}（{who}/{source}）：{entry['text'][:50]}")

    # ---------------------------------------------------------------- 消息处理

    def _message_key(self, event: AstrMessageEvent) -> tuple:
        obj = getattr(event, "message_obj", None)
        mid = getattr(obj, "message_id", None)
        if mid:
            return ("id", str(mid))
        return (
            "key",
            str(getattr(event, "unified_msg_origin", "") or ""),
            str(event.get_sender_id() or ""),
            (event.get_message_str() or "")[:80],
        )

    def _update_state(self, event: AstrMessageEvent, now: float) -> bool:
        key = self._message_key(event)
        if key == self._last_msg_key:
            return False
        self._last_msg_key = key

        sender = str(event.get_sender_id() or "")
        try:
            self_id = str(event.get_self_id() or "")
        except Exception:
            self_id = ""
        if not sender or sender == "astrbot" or (self_id and sender == self_id):
            return False

        text = event.get_message_str() or ""
        is_group = bool(event.get_group_id())
        name = (event.get_sender_name() or "").strip()[:24]

        self._prev_last_msg = float(self.mood.data.get("last_user_msg_at") or 0)
        self._record_energy(now)
        # 先结算“沉默期间的自然衰减”，再取基准值；否则昨晚到今早的衰减
        # 会被算成“这条消息让她掉了 30 点心情”，误记成委屈/旧账。
        self.mood.data = decay_mood(self.mood.data, now)
        before = float(self.mood.data.get("valence", 10))
        self.mood.data = apply_message(self.mood.data, now, text, is_group=is_group)
        try:
            self.mood.data = emotion.apply(self.mood.data, text, now, is_group=is_group)
        except Exception:
            logger.debug("[state] 情绪层更新失败", exc_info=True)
        after = float(self.mood.data.get("valence", 10))
        self._last_delta = after - before

        rec, delta = touch_affinity(
            self.affinity.data, sender, now, name=name, text=text, floors=self.floors
        )
        if delta:
            logger.debug(f"[state] {rec.get('name', sender)} 好感 {delta:+.2f}")

        self._maybe_record(now, text, sender, name, self._last_delta)
        if now - self._last_save >= SAVE_INTERVAL:
            self._save()
        return True

    def _record_energy(self, now: float) -> None:
        """每小时记一个精力点，用来画曲线。"""
        if now - self._last_hist < 3600:
            return
        self._last_hist = now
        try:
            path = self.base / "energy_history.jsonl"
            view = mood_view(self.mood.data, now)
            import json

            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": now,
                    "energy": round(float(view["energy"]), 1),
                    "valence": round(float(view["valence"]), 1),
                }, ensure_ascii=False) + "\n")
            cutoff = now - 7 * 86400
            lines = [ln for ln in path.read_text(encoding="utf-8").splitlines()
                     if ln.strip() and json.loads(ln).get("ts", 0) >= cutoff]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception:
            logger.debug("[state] 精力曲线写入失败", exc_info=True)

    def _maybe_record(
        self, now: float, text: str, sender: str, name: str, delta: float
    ) -> None:
        if not self.emotional_enabled:
            return
        # 只看负向波动：一次开心不该抵消一次委屈
        self._neg_window = [
            (t, d) for (t, d) in self._neg_window if now - t < emotional.WINDOW_SECONDS
        ]
        if delta < 0:
            self._neg_window.append((now, delta))
        window_sum = sum(d for _, d in self._neg_window)
        if delta > -emotional.SINGLE_EVENT_DELTA and window_sum > -emotional.WINDOW_EVENT_DELTA:
            return
        saved = emotional.add_event(
            self.emotional.data,
            emotional.make_event(
                user_id=sender,
                user_name=name,
                trigger_text=text,
                delta=min(delta, window_sum),
                source="规则",
                now=now,
            ),
        )
        if saved:
            self._neg_window.clear()
            logger.info(
                f"[state] 记下一件「{saved['emotion']}」：{saved['trigger_text'][:40]}"
                f"（{saved['delta']:+.1f}）"
            )

    def _maybe_recall(self, event: AstrMessageEvent, now: float) -> str:
        if not self.emotional_enabled or not self.emotional.data.get("events"):
            return ""
        sender = str(event.get_sender_id() or "")
        view = mood_view(self.mood.data, now)
        hit = emotional.pick(
            self.emotional.data,
            user_id=sender,
            current_text=event.get_message_str() or "",
            current_delta=self._last_delta,
            current_valence=view["valence"],
            grudge=self.grudge,
            now=now,
        )
        if not hit:
            return ""
        emotional.mark_recalled(self.emotional.data, int(hit["id"]), now)
        self._save()
        logger.info(f"[state] 翻出旧账 #{hit['id']}（{hit['date']} {hit['emotion']}）")
        return emotional.format_block(hit)

    def _save(self) -> None:
        try:
            self.mood.save()
            self.affinity.save()
            self.emotional.save()
            self.core.save()
            self._last_save = time.time()
        except Exception:
            logger.exception("[state] 落盘失败")

    # ---------------------------------------------------------------- 注入

    @filter.on_llm_request(priority=-8)
    async def inject(self, event: AstrMessageEvent, req) -> None:
        try:
            now = time.time()
            if self._update_state(event, now):
                self._recall_block = self._maybe_recall(event, now)
            if not self.inject_enabled:
                return
            is_group = bool(event.get_group_id())
            if is_group and not self.inject_in_group:
                return

            blocks: list[str] = []
            core_block = self.core.format_block()
            if core_block:
                blocks.append(core_block)
            blocks.append(self._state_block(event, now, is_group))
            if self._recall_block:
                blocks.append(self._recall_block)

            body = "\n\n".join(b for b in blocks if b)
            base = req.system_prompt or ""
            cuts = [base.find(m) for m in (CORE_MARKER, MARKER_STATE, MARKER_GRUDGE)]
            cuts = [c for c in cuts if c != -1]
            if cuts:
                base = base[: min(cuts)].rstrip()
            req.system_prompt = (base + "\n\n" + body).strip()
        except Exception:
            logger.exception("[state] 注入失败")

    def _state_block(self, event: AstrMessageEvent, now: float, is_group: bool) -> str:
        view = mood_view(self.mood.data, now)
        labels = mood_labels(view)
        sender = str(event.get_sender_id() or "")
        av = affinity_view(self.affinity.data.get("users", {}).get(sender))

        lines = [
            MARKER_STATE,
            f"心情 {_fmt(view['valence'])}/100（{labels['valence']}）、"
            f"精力 {_fmt(view['energy'])}/100（{labels['energy']}）、"
            f"黏人 {_fmt(view['clinginess'])}/100（{labels['clinginess']}）",
        ]
        if av:
            lines.append(
                f"对「{av['name']}」的好感 {_fmt(av['score'])}/100（{av['level']}），"
                f"累计聊过 {av['msgs']} 句；今天聊了 {view['day_msgs']} 句"
            )
        prev = float(getattr(self, "_prev_last_msg", 0) or 0)
        if prev > 0:
            gap_h = max(0.0, (now - prev) / 3600.0)
            when = time.strftime("%m-%d %H:%M", time.localtime(prev))
            if gap_h >= 1:
                if gap_h < 24:
                    lines.append(f"距上次聊天 {gap_h:.0f} 小时（上次是 {when}）")
                else:
                    lines.append(f"距上次聊天 {gap_h / 24:.0f} 天（上次是 {when}）")
        try:
            mood_line = emotion.describe(self.mood.data)
            mood_hint = emotion.hint(self.mood.data)
        except Exception:
            mood_line, mood_hint = "", ""
        if mood_line:
            lines.append(f"现在的心情：{mood_line}")
        hint = self._hint(view, av)
        if mood_hint:
            hint = (hint + "；" + mood_hint) if hint else mood_hint
        if hint:
            lines.append(hint)
        try:
            reg = emotion.regulation(self.mood.data, now)
        except Exception:
            reg = ""
        if reg:
            lines.append(reg)
        pending = self._pending_confirm
        if pending and time.time() - float(pending.get("ts", 0)) < 180:
            if str(event.get_sender_id() or "") == pending.get("by_id"):
                lines.append(
                    "（刚刚让你把「" + str(pending.get("text", ""))[:60] + "」永远记住，已经写进识海。"
                    "这轮自然确认一句，不要复述原文。）"
                )
                self._pending_confirm = None
        lines.append("这些是内部状态：不报数字，也不解释这套机制。")
        if is_group:
            lines.append("群里不要说出自己的状态，也不要提好感度。")
        return "\n".join(lines)

    @staticmethod
    def _hint(view: dict, av: dict | None) -> str:
        parts: list[str] = []
        if view["energy"] < 30:
            parts.append("精力低：句子更短，别硬撑热情")
        elif view["energy"] > 80:
            parts.append("精力足：可以多接一句")
        if view["valence"] <= -20:
            parts.append("心情差：语气低一点，别装开心")
        elif view["valence"] >= 40:
            parts.append("心情好：可以闹一点")
        if view["clinginess"] > 70:
            parts.append("很黏：可以主动一点，但别刷屏")
        elif view["clinginess"] < 25:
            parts.append("想安静：不用每句都接")
        if av and av["level"] in ("亲近", "重要的人"):
            parts.append("对这人可以更放松、更亲")
        return "；".join(parts) + "。" if parts else ""

    # ---------------------------------------------------------------- 工具

    @filter.llm_tool(name="adjust_mood")
    async def adjust_mood(
        self,
        event: AstrMessageEvent,
        valence: int = 0,
        energy: int = 0,
        clinginess: int = 0,
        reason: str = "",
    ):
        """微调自己当前的情绪（心情/精力/黏人），每次最多 ±20。

        Args:
            valence(number): 心情变化，-20~20
            energy(number): 精力变化，-20~20
            clinginess(number): 黏人变化，-20~20
            reason(string): 一句话说明为什么变
        """
        if not self.enable_tools:
            return "情绪工具已关闭。"
        try:
            v = max(-20, min(20, int(valence)))
            e = max(-20, min(20, int(energy)))
            c = max(-20, min(20, int(clinginess)))
            now = time.time()
            data = self.mood.data
            data["valence"] = clamp(float(data.get("valence", 10)) + v, -100, 100)
            data["energy"] = clamp(float(data.get("energy", 65)) + e, 0, 100)
            data["cling_adj"] = clamp(float(data.get("cling_adj", 0)) + c, -20, 60)
            data["updated_at"] = now
            if self.emotional_enabled and v <= -10:
                saved = emotional.add_event(
                    self.emotional.data,
                    emotional.make_event(
                        user_id=str(event.get_sender_id() or ""),
                        user_name=(event.get_sender_name() or "").strip()[:24],
                        trigger_text=(event.get_message_str() or reason)[:200],
                        delta=float(v),
                        reason=reason,
                        source="自评",
                        now=now,
                    ),
                )
                if saved:
                    logger.info(f"[state] 记下一件「{saved['emotion']}」：{reason[:40]}")
            self._save()
            return f"心情{v:+d}，精力{e:+d}，黏人{c:+d}。"
        except Exception as exc:
            logger.exception("[state] 调整情绪失败")
            return f"调整失败：{exc}"

    @filter.llm_tool(name="adjust_affinity")
    async def adjust_affinity(self, event: AstrMessageEvent, delta: int = 0, reason: str = ""):
        """微调自己对当前说话人的好感度，每次最多 ±10。

        Args:
            delta(number): 好感变化，-10~10
            reason(string): 一句话说明为什么变
        """
        if not self.enable_tools:
            return "好感工具已关闭。"
        try:
            d = max(-10, min(10, int(delta)))
            sender = str(event.get_sender_id() or "")
            rec = self.affinity.data.get("users", {}).get(sender)
            if not isinstance(rec, dict):
                return "还没记住这个人，先正常聊。"
            floor = float(self.floors.get(sender, 0.0))
            rec["score"] = round(clamp(max(float(rec.get("score", 30)) + d, floor)), 2)
            rec["last_seen"] = time.time()
            self._save()
            return f"好感 {d:+d}。"
        except Exception as exc:
            logger.exception("[state] 调整好感失败")
            return f"调整失败：{exc}"

    # ---------------------------------------------------------------- 命令

    def _is_allowed(self, event: AstrMessageEvent) -> bool:
        return str(event.get_sender_id() or "") in self.allowed

    @filter.command("心情", alias={"状态"})
    async def cmd_mood(self, event: AstrMessageEvent):
        """看当前情绪。"""
        now = time.time()
        view = mood_view(self.mood.data, now)
        labels = mood_labels(view)
        yield event.plain_result(
            f"心情 {_fmt(view['valence'])}/100（{labels['valence']}）｜"
            f"精力 {_fmt(view['energy'])}/100（{labels['energy']}）｜"
            f"黏人 {_fmt(view['clinginess'])}/100（{labels['clinginess']}）"
        )

    @filter.command("好感", alias={"好感度"})
    async def cmd_affinity(self, event: AstrMessageEvent):
        """看好感榜。"""
        if not event.is_private_chat():
            yield event.plain_result("好感度私聊看。")
            return
        users = self.affinity.data.get("users", {})
        if not users:
            yield event.plain_result("还没跟谁聊过。")
            return
        rows = sorted(users.values(), key=lambda r: float(r.get("score", 0)), reverse=True)[:8]
        lines = ["好感度："]
        for r in rows:
            score = float(r.get("score", 0))
            lines.append(
                f"{r.get('name', '?')}　{_fmt(score)}/100（{affinity_level(score)}）　聊过 {int(r.get('msgs', 0))} 句"
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("好感改", alias={"改好感"})
    async def cmd_set_affinity(self, event: AstrMessageEvent):
        """手动校准好感。用法：/好感改 QQ号 分数"""
        if not self._is_allowed(event):
            yield event.plain_result("只有被授权的人能改。")
            return
        parts = (event.message_str or "").strip().split()
        if len(parts) < 3:
            yield event.plain_result("用法：/好感改 QQ号 分数")
            return
        uid = "".join(ch for ch in parts[1] if ch.isdigit())
        try:
            score = clamp(float(parts[2]))
        except ValueError:
            yield event.plain_result("分数要填 0-100。")
            return
        users = self.affinity.data.setdefault("users", {})
        rec = users.get(uid)
        if not isinstance(rec, dict):
            rec = {
                "name": uid,
                "score": score,
                "first_seen": time.time(),
                "last_seen": time.time(),
                "msgs": 0,
                "gain_day": "",
                "gain_today": 0.0,
            }
            users[uid] = rec
        rec["score"] = round(score, 2)
        self._save()
        yield event.plain_result(f"{rec.get('name', uid)} 的好感度改成 {_fmt(score)}。")

    @filter.command("旧账", alias={"翻旧账"})
    async def cmd_grudges(self, event: AstrMessageEvent):
        """看压在心里的旧事。用法：/旧账；/旧账 原谅 编号"""
        if not event.is_private_chat():
            yield event.plain_result("私聊看。")
            return
        parts = (event.message_str or "").strip().split()
        events = self.emotional.data.get("events", [])
        if len(parts) >= 3 and parts[1] in {"原谅", "算了", "放下"}:
            if not self._is_allowed(event):
                yield event.plain_result("只有被授权的人能动。")
                return
            digits = "".join(ch for ch in parts[2] if ch.isdigit())
            if not digits:
                yield event.plain_result("用法：/旧账 原谅 编号")
                return
            target = int(digits)
            for e in events:
                if int(e.get("id", -1)) == target:
                    e["resolved"] = True
                    self._save()
                    yield event.plain_result(f"#{target} 放下了。")
                    return
            yield event.plain_result(f"没有 #{target} 这条。")
            return

        if not events:
            yield event.plain_result("没什么压着的事。")
            return
        rows = sorted(events, key=lambda e: float(e.get("created_at", 0)), reverse=True)[:10]
        lines = [f"旧账（{len(events)} 条，平时不提）："]
        for e in rows:
            mark = "已放下" if e.get("resolved") else f"翻过 {int(e.get('recalls', 0))} 次"
            lines.append(
                f"{e.get('id')}. [{e.get('date')} {e.get('user_name')}] {e.get('emotion')} "
                f"{float(e.get('delta', 0)):+.0f}｜{str(e.get('trigger_text', ''))[:36]}｜{mark}"
            )
        yield event.plain_result("\n".join(lines)[:3500])

    # ---------------- 识海

    @filter.command("识海", alias={"核心记忆"})
    async def cmd_core(self, event: AstrMessageEvent):
        """看识海。"""
        if not self._is_allowed(event):
            return
        if not event.is_private_chat():
            yield event.plain_result("这些内容偏私密，私聊问。")
            return
        entries = self.core.active()
        if not entries:
            yield event.plain_result("识海是空的。说「永远记住……」或 /记住 内容。")
            return
        lines = [f"识海（{len(entries)} 条，永不遗忘）："]
        for e in entries:
            lines.append(f"{e.get('id')}. [{e.get('created_date')} {e.get('by')}] {e.get('text')}")
        yield event.plain_result("\n".join(lines)[:3500])

    @filter.command("记住", alias={"识海加"})
    async def cmd_remember(self, event: AstrMessageEvent):
        """手动写一条。用法：/记住 内容"""
        sender = str(event.get_sender_id() or "")
        if sender not in self.allowed:
            yield event.plain_result("只有被授权的人能写。")
            return
        raw = (event.message_str or "").strip()
        _, _, tail = raw.partition(" ")
        text = tail.strip()
        if not text:
            text = extract_trigger(raw, TRIGGERS) or ""
        if not text:
            yield event.plain_result("用法：/记住 要永远记住的事")
            return
        entry, created = self.core.add(text, self.allowed[sender], sender, "命令")
        if not created:
            yield event.plain_result("这条已经在识海里了。")
            return
        yield event.plain_result(f"记下了（#{entry['id']}）。这个不会忘。")

    @filter.command("识海删", alias={"忘掉"})
    async def cmd_forget(self, event: AstrMessageEvent):
        """删除一条。用法：/识海删 编号"""
        if not self._is_allowed(event):
            yield event.plain_result("只有被授权的人能改。")
            return
        parts = (event.message_str or "").strip().split()
        digits = "".join(ch for ch in (parts[-1] if parts else "") if ch.isdigit())
        if not digits:
            yield event.plain_result("用法：/识海删 编号")
            return
        entry_id = int(digits)
        if not self.core.delete(entry_id):
            yield event.plain_result(f"没有 #{entry_id} 这条。")
            return
        yield event.plain_result(f"#{entry_id} 删掉了。")
