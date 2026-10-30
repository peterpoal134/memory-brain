# -*- coding: utf-8 -*-
"""最后一层聊天风格护栏：压住客服腔和助手腔。"""
from __future__ import annotations

import asyncio
import difflib
import json
import random
import re
import time
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import At, Plain

MARKER = "【聊天风格·最后检查】"
EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\u200d]")
# 两段合成一条的概率：真人约四成只发一条，她如果总拆成两条，就靠这个兜底
MERGE_TWO_PROB = 0.38
MOOD_FILE = (Path(__file__).resolve().parents[2] / "plugin_data/astrbot_plugin_persona_state/mood.json")


def mood_state() -> tuple[float, str]:
    """读心情（-1~1）和最明显的情绪标签；读不到就当平静。"""
    try:
        data = json.loads(MOOD_FILE.read_text(encoding="utf-8"))
        valence = max(-1.0, min(1.0, float(data.get("valence", 0)) / 100.0))
        labels = (data.get("pad") or {}).get("labels") or {}
        top = max(labels, key=lambda k: labels[k]) if labels else ""
        return valence, top
    except Exception:
        return 0.0, ""
FACE_CODE_RE = re.compile(r"\[[^\[\]\n]{1,6}\]")
FACE_POOL = ["[微笑]", "[偷笑]", "[捂脸]", "[doge]", "[白眼]", "[撇嘴]", "[得意]", "[可爱]", "[亲亲]", "[哈欠]"]
TECH_HINTS = ("代码", "报错", "表格", "文档", "公式", "步骤", "教程", "命令", "配置", "安装", "部署")

STYLE_BLOCK = f"""{MARKER}
你是在跟熟人聊天，不是客服，也不是在完成任务清单。
绝对不要问“有什么可以帮你”“有什么我能帮”“需要我做什么”“要不要我帮忙”，不要主动提供帮助，不要说“随时告诉我”“有事叫我”“尽管吩咐”。
对方没求助，就不要给方案；先接住他这句话。
日常一到两句，短句，不列点、不总结、不写“首先/其次/总之”。认真任务可以展开，但不要客服腔。
你在群里也能聊读过的书，不需要让人私聊；想翻一页就用 read_book。
记忆块或上下文里没有的事，不要编；直接说“我记不清了”“这个没查到”。日期、书名、账号这类细节尤其不要猜。
消息里出现 [引用消息(…)] 时，先读懂被引用的那句话，再回；不要装没看见。
要找人办事时，像真人一样：先@对方，再喊一声（昵称、外号或名字），然后说事。
例：@朋友 朋友，帮我安排个听歌的呗。／@小王 小王，那个文件你看了吗。
@ 就用 @名字 的写法，系统会转成真的@，屏幕上显示的是他的群名片，不是你打的字。
回答“某某是谁”时，只说人和关系（“管理员啊”“就是朋友”），不要报 QQ、账号、群昵称，也不要把记忆原文念出来。
对方在哄你、说软话时，别只回一个“嗯”，接住那句话。
对方打招呼时，回一句自然的，比如“在，咋了”“醒啦”“嗯，在呢”；不要追问需求，也不要问“是想我了还是有事”。
像微信打字，不像写作文：日常短句结尾**不要加句号**，少用完整语法；可以用“啊/吧/呢/算了/行吧”。短可以，但别让话掉地上；想多说就带半句反应，不想说就短着。
不写括号里的动作或旁白（不要“（笑）”“（尾巴一摆）”这种），不每句都抖一个机灵，也不每句都收得很漂亮。
偶尔可以慢半拍、先丢一句短的，再补一句；拿不准就“啊？”“啥意思”“没听懂”。
例：在吗 → 在。咋了。／今天好累 → 累成这样啊，先瘫着。／你能帮我个忙吗 → 说呗。
"""

# 先把“想我了还是有事”这类准客服问句改掉，再删整句
REPLACEMENTS = [
    (re.compile(r"在?呢?[，, ]*(?:是)?想我了?还是有事(?:呀|吗|么)?[？?]?"), "，咋了。"),
    (re.compile(r"有什么事(?:呀|吗|么)?[？?]?"), "咋了？"),
    (re.compile(r"说吧[，, ]*什么事[。？?]?"), "咋了？"),
    (re.compile(r"有啥事(?:呀|吗|么)?[？?]?"), "咋了？"),
]

# 命中这些句子的整句删除（不是替换成另一句客服话）
BANNED = [
    re.compile(p)
    for p in (
        r"有什么(?:可以|能|需要)帮",
        r"需要我(?:做|帮|处理)",
        r"要不要我帮",
        r"我可以帮你",
        r"随时(?:告诉|叫|找)我",
        r"有事(?:就)?叫我",
        r"尽管(?:告诉|吩咐|说)",
        r"希望(?:对你|能)有(?:所)?帮助",
        r"作为(?:一个)?(?:AI|人工智能|助手)",
        r"我是(?:一个)?(?:AI|人工智能|助手)",
        r"很(?:高兴|荣幸)(?:能)?(?:为|帮)你",
    )
]


def clean_text(text: str) -> str:
    """删掉整句客服腔；代码块原样保留。"""
    if not text or "```" in text:
        return text
    for pattern, replacement in REPLACEMENTS:
        text = pattern.sub(replacement, text)
    parts = re.split(r"(?<=[。！？!?~…\n])", text)
    kept = []
    for part in parts:
        if not part.strip():
            continue
        if any(p.search(part) for p in BANNED):
            continue
        kept.append(part)
    cleaned = "".join(kept).strip().lstrip("，,。 ")
    cleaned = re.sub(r"([。！？!?~…])[。！？!?~…]+", r"\1", cleaned)
    # 去掉模型留下的孤立括号/空括号，避免句尾挂一个“）”
    cleaned = re.sub(r"[（(]\s*[）)]", "", cleaned)
    cleaned = re.sub(r"(?<![（(])[）)]\s*$", "", cleaned)
    cleaned = re.sub(r"^\s*[（(](?![^（(]*[）)])", "", cleaned)
    # 微信式：很短的一句日常回复，结尾不挂句号
    if len(cleaned) <= 30 and cleaned.endswith("。"):
        cleaned = cleaned[:-1]
    cleaned = cleaned.strip()
    return cleaned or random.choice(["嗯？", "咋了？", "你说。", "在。"])


@register(
    "astrbot_plugin_human_style_guard",
    "community",
    "最后一层压住客服腔、助手腔和主动提供帮助的习惯",
    "1.0.0",
)
class HumanStyleGuardPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        # 每个会话最近一次用的表情 + 距离上次隔了几条
        self._face_state: dict[str, dict] = {}
        raw = str((config or {}).get("at_map") or "")
        if not raw:
            try:
                cfg = Path(__file__).resolve().parents[2] / "config" / "astrbot_plugin_human_style_guard_config.json"
                raw = str(json.loads(cfg.read_text(encoding="utf-8-sig")).get("at_map") or "")
            except Exception:
                raw = ""
        try:
            self.at_map = {str(k): str(v) for k, v in json.loads(raw or "{}").items()}
        except Exception:
            self.at_map = {}
        self._member_cache: dict[str, tuple[float, dict[str, str]]] = {}
        self._last_reply: dict[str, tuple[str, float]] = {}
        logger.info(f"[human_style] @ 映射 {len(self.at_map)} 人")

    @filter.on_llm_request()
    async def inject_style(self, event: AstrMessageEvent, req) -> None:
        try:
            base = req.system_prompt or ""
            cut = base.find(MARKER)
            if cut != -1:
                base = base[:cut].rstrip()
            req.system_prompt = (base + "\n\n" + STYLE_BLOCK).strip()
        except Exception:
            logger.exception("[human_style] 注入风格护栏失败")

    async def _members(self, event: AstrMessageEvent) -> dict[str, str]:
        """群成员 名字→QQ，缓存 5 分钟。"""
        gid = str(event.get_group_id() or "")
        if not gid:
            return {}
        now = time.time()
        cached = self._member_cache.get(gid)
        if cached and now - cached[0] < 300:
            return cached[1]
        mapping: dict[str, str] = {}
        try:
            members = await event.bot.get_group_member_list(group_id=int(gid))
            for m in members or []:
                uid = str(m.get("user_id") or "")
                if not uid:
                    continue
                for key in ("card", "nickname", "remark"):
                    name = str(m.get(key) or "").strip()
                    if name:
                        mapping.setdefault(name, uid)
        except Exception:
            logger.debug("[human_style] 获取群成员失败", exc_info=True)
        self._member_cache[gid] = (now, mapping)
        return mapping

    @filter.on_decorating_result()
    async def render_at(self, event: AstrMessageEvent) -> None:
        """把模型写的 [At:123456] 或 @名字 变成真正的 @。"""
        try:
            result = event.get_result()
            if result is None or not getattr(result, "chain", None):
                return
            new_chain = []
            changed = False
            for seg in result.chain:
                if isinstance(seg, Plain):
                    text = seg.text
                    # @名字 → 真 @：[At:QQ] 优先，其次静态映射，最后查群成员
                    members = None
                    for m in re.finditer(r"@([\w\u4e00-\u9fa5]{1,12})", text):
                        name = m.group(1)
                        qq = self.at_map.get(name)
                        if not qq:
                            if members is None:
                                members = await self._members(event)
                            qq = members.get(name)
                        if qq:
                            text = text.replace("@" + name, f"[At:{qq}]", 1)
                            changed = True
                    parts = re.split(r"(\[At:\d+\])", text)
                    for part in parts:
                        m = re.fullmatch(r"\[At:(\d+)\]", part or "")
                        if m:
                            new_chain.append(At(qq=m.group(1)))
                            changed = True
                        elif part:
                            new_chain.append(Plain(part))
                else:
                    new_chain.append(seg)
            if changed:
                result.chain = new_chain
        except Exception:
            logger.exception("[human_style] @ 转换失败")

    @filter.on_decorating_result()
    async def human_delay(self, event: AstrMessageEvent) -> None:
        """发之前停一下：真人不会秒回，也不会每条都卡着同一秒。"""
        try:
            text = (event.get_message_str() or "").strip()
            if text.startswith("/"):
                return
            await asyncio.sleep(random.uniform(0.8, 2.6))
        except Exception:
            logger.exception("[human_style] 打字延迟失败")

    @filter.on_llm_response()
    async def clean_response(self, event: AstrMessageEvent, resp) -> None:
        try:
            if getattr(resp, "is_chunk", False):
                return
            text = getattr(resp, "completion_text", "") or ""
            if not text.strip():
                return
            if any(h in (event.get_message_str() or "") for h in TECH_HINTS):
                return
            # 整条回复只有一个表情/一个词：直接发，不补成句子
            probe = EMOJI_RE.sub("", FACE_CODE_RE.sub("", text)).strip()
            if not probe:
                faces = FACE_CODE_RE.findall(text) or EMOJI_RE.findall(text)
                if faces:
                    face = faces[0]
                    if event.get_group_id() and not FACE_CODE_RE.fullmatch(face):
                        face = random.choice(FACE_POOL)
                    resp.completion_text = face
                    return
            cleaned = clean_text(text)
            # 两段式/空行是模型常见的腔调，先压成一行
            cleaned = re.sub(r"\n\s*\n+", "\n", cleaned).strip()

            # 表情统一由这里控制：先全部摘掉，再按节奏偶尔补一个 QQ 表情
            session = getattr(event, "unified_msg_origin", "") or str(event.get_sender_id() or "")
            state = self._face_state.setdefault(session, {"last": "", "gap": 99})
            cleaned = FACE_CODE_RE.sub("", cleaned)
            cleaned = EMOJI_RE.sub("", cleaned)
            cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
            cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
            if not cleaned:
                cleaned = "嗯"

            # 一轮最多 3 条：多出来的并进最后一条，避免一次刷 4-5 条
            lines = [ln for ln in cleaned.split("\n") if ln.strip()]
            if len(lines) > 3:
                lines = lines[:2] + ["，".join(lines[2:])]
                cleaned = "\n".join(lines)
                lines = [ln for ln in cleaned.split("\n") if ln.strip()]
            # 一部分两段合成一条，补上“只发一条”的分布
            if len(lines) == 2 and random.random() < MERGE_TWO_PROB:
                cleaned = "，".join(ln.rstrip("，,。. ") for ln in lines)
                lines = [cleaned]

            state["gap"] = int(state.get("gap", 0)) + 1
            prob = 0.22 if event.get_group_id() else 0.30
            # 情绪影响表达：低落/生气时少发表情、收掉感叹号；开心时多带一点
            try:
                valence, label = mood_state()
            except Exception:
                valence, label = 0.0, ""
            if label in ("生气", "难过", "委屈") or valence < -0.2:
                cleaned = cleaned.replace("！", "。").replace("!", "。")
                prob *= 0.35
            elif label in ("开心", "感动", "害羞", "惊讶") or valence > 0.3:
                prob *= 1.4
            if label in ("生气", "难过"):
                prob = 0.0
            if state["gap"] >= 2 and random.random() < prob:
                choices = [f for f in FACE_POOL if f != state.get("last")]
                face = random.choice(choices)
                cleaned = cleaned.rstrip() + " " + face
                state["last"] = face
                state["gap"] = 0
            # 复读检测：3 分钟内和上一条几乎一样，就收成一句短的
            try:
                now = time.time()
                last = self._last_reply.get(session)
                cmp_now = re.sub(r"\s*\[[^\[\]]{1,8}\]$", "", cleaned)
                if last and len(cmp_now) >= 8 and now - last[1] < 180:
                    if difflib.SequenceMatcher(None, cmp_now, last[0]).ratio() >= 0.85:
                        logger.info("[human_style] 复读，收短")
                        cleaned = "嗯"
                self._last_reply[session] = (last[0] if cleaned == "嗯" and last else cmp_now, now)
            except Exception:
                pass
            if cleaned != text:
                resp.completion_text = cleaned
        except Exception:
            logger.exception("[human_style] 清理回复失败")
