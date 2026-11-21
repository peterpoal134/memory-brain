"""Conservative, local admission criteria for automatic recollection."""

import re
import unicodedata

from .storage import _lexical_tokens


_BACKCHANNEL = re.compile(
    r"(?:好(?:的|吧|呀|啊)?|"
    r"对(?:的|吧|呀|啊|吗)?|是(?:的|啊|呀)?|行(?:的|吧|啊)?|可以|"
    r"收到|没错|明白(?:了)?|了解(?:了)?|知道了|懂了|谢谢(?:你)?|"
    r"感谢(?:你)?|辛苦了|你好|晚安|[嗯唔哦噢啊呃额哈呵嘿欸的呢吧呀])"
)
_ENGLISH_BACKCHANNELS = frozenset("ok okay yep yeah yes right sure thanks thx uh huh mhm gotcha exactly".split())
_ENGLISH_PHRASES = frozenset({("i", "see"), ("got", "it"), ("of", "course"), ("all", "right"),
                              ("thank", "you"), ("makes", "sense")})
_STOP_TERMS = frozenset((
    "the a an and or is are was were be been to of for in on at as with my your this that it you we "
    "how what where when why which who can could should would please tell me do does did just about "
    "remember again know used use using install installation error problem issue step steps method before after "
    "怎么 如何 什么 哪儿 哪里 哪个 哪些 为什么 这个 那个 的话 一个 一下 今天 昨天 明天 当时 "
    "刚才 之前 以前 上次 说过 记得 知道 想起 时候 是否 有没有 有人 大家 我们 你们 他们 "
    "问题 方法 步骤 操作 安装 是否 可以 已经 能不能 对的 好的 嗯嗯 是的 没错 收到 谢谢"
).split())
_CJK_RUN = re.compile(r"[\u3400-\u9fff\U00020000-\U0003134f]+")
_TECHNICAL_C = re.compile(r"(?<![a-z0-9_])c(?:\+\+|#)(?![a-z0-9_])")
_CJK_PARTICLES = frozenset("的了吗呢吧呀")
_RELATIVE_TIME = re.compile(
    r"今[天早晚]|昨[天早晚]|明[天早晚]|[前后]天|[本上这下](?:周|星期|月|年)|"
    r"刚才|稍后|之后|以前|\b(?:today|yesterday|tomorrow|tonight|later|now|ago)\b"
)

DEFAULT_SEMANTIC_FLOOR = .65
BGE_M3_SEMANTIC_FLOOR = .62
STRONG_SEMANTIC_SCORE = .72
SEMANTIC_TOP_MARGIN = .025


def _normalize(text):
    return unicodedata.normalize("NFKC", text).casefold()


def _acknowledgment_word(word):
    if word in _ENGLISH_BACKCHANNELS:
        return True
    offset = 0
    while offset < len(word):
        matched = _BACKCHANNEL.match(word, offset)
        if matched is None:
            return False
        offset = matched.end()
    return True


def low_information(text):
    """Recognize full utterances only, leaving substantive replies intact."""
    words = re.findall(r"[^\W_]+", _CJK_RUN.sub(r" \g<0> ", _normalize(text)))
    index = 0
    while index < len(words):
        if tuple(words[index:index + 2]) in _ENGLISH_PHRASES:
            index += 2
        elif _acknowledgment_word(words[index]):
            index += 1
        else:
            return False
    return True


def _lexical_terms(text):
    return {token for token in _lexical_tokens(text) if len(token) > 1 and token not in _STOP_TERMS
            and not _CJK_PARTICLES.intersection(token)}


class LexicalSignal:
    """Shared topic terms constrain FTS retrieval and corroborate its results."""

    def __init__(self, query):
        query = _normalize(query)
        self.terms = _lexical_terms(query)
        self.short_phrases = {run for run in _CJK_RUN.findall(query) if len(run) == 2 and run in self.terms}
        self.technical = frozenset(_TECHNICAL_C.findall(query))
        self.tokens = tuple(sorted(self.terms | ({"c"} if self.technical else set())))

    def matches(self, text):
        if self.technical.intersection(_TECHNICAL_C.findall(_normalize(text))):
            return True
        matches = self.terms.intersection(_lexical_terms(text))
        if len(self.terms) <= 2:
            return bool(matches.intersection(self.short_phrases)
                        or (matches and len(matches) == len(self.terms)))
        return len(matches) >= 2 and len(matches) * 10 >= len(self.terms) * 3


def semantic_floor(model):
    name = model.casefold().rsplit("/", 1)[-1].split(":", 1)[0]
    return BGE_M3_SEMANTIC_FLOOR if name == "bge-m3" else DEFAULT_SEMANTIC_FLOOR


def evidence_identity(reference):
    text = " ".join(_normalize(reference["text"]).split())
    timestamp = reference["recorded_at"] if _RELATIVE_TIME.search(text) else None
    return reference["role"], reference["source_kind"], text, timestamp
