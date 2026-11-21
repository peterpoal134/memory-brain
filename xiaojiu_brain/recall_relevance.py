"""Conservative, local admission criteria for automatic recollection."""

import re
import unicodedata

from .storage import _lexical_tokens
from .temporal import has_anchored_date_reference


_BACKCHANNEL = re.compile(
    r"(?:好(?:的|滴|吧|呀|啊)?|"
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
    "问题 方法 步骤 操作 安装 装上 是否 可以 已经 能不能 对的 好的 嗯嗯 是的 没错 收到 谢谢"
).split())
_CJK_RUN = re.compile(r"[\u3400-\u9fff\U00020000-\U0003134f]+")
_TECHNICAL_C = re.compile(r"(?<![a-z0-9_])c(?:\+\+|#)(?![a-z0-9_])")
_CJK_QUERY_FILLERS = re.compile(
    r"(?:(?:存放|保存|存|放)(?:到|在|于)|位于|在)?(?:哪里|哪儿|何处)|"
    r"怎么样|怎么|如何|为什么|是什么|是啥|什么|哪个|哪些|是否|有没有|能不能|是不是|多久|来着"
)
_VAGUE_RELATIVE_TIME = re.compile(
    r"[今昨明][早晚]|[本上这下][月年]|刚才|稍后|之后|以前|\b(?:tonight|later|now|ago)\b"
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


def _query_phrases(text):
    for run in _CJK_RUN.findall(_CJK_QUERY_FILLERS.sub(" ", text)):
        run = run.rstrip("了吗呢呀啊")
        if len(run) >= 2 and run not in _STOP_TERMS:
            yield run


class LexicalSignal:
    """Shared topic terms constrain FTS retrieval and corroborate its results."""

    def __init__(self, query):
        query = _normalize(query)
        self.phrases = tuple(_query_phrases(query))
        self.words = {token for token in _lexical_tokens(query) if len(token) > 1
                      and not _CJK_RUN.fullmatch(token) and token not in _STOP_TERMS}
        self.technical = frozenset(_TECHNICAL_C.findall(query))
        bigrams = {phrase[i:i + 2] for phrase in self.phrases for i in range(len(phrase) - 1)}
        self.tokens = tuple(sorted(self.words | bigrams | ({"c"} if self.technical else set())))

    def matches(self, text):
        text = _normalize(text)
        if self.technical and not self.technical.intersection(_TECHNICAL_C.findall(text)):
            return False
        tokens = set(_lexical_tokens(text))
        if self.phrases:
            return self.words <= tokens and all(_phrase_matches(phrase, text, tokens) for phrase in self.phrases)
        matches = len(self.words.intersection(tokens))
        if len(self.words) <= 2:
            return bool(self.tokens) and matches == len(self.words)
        return matches >= 2 and matches * 10 >= len(self.words) * 3


def _phrase_matches(phrase, text, tokens):
    if len(phrase) <= 3:
        return phrase in text
    # A shared top-level topic is insufficient when the query names a
    # different qualifier (for example, the screen versus the battery).
    if phrase[-2:] not in tokens:
        return False
    matches = sum(phrase[i:i + 2] in tokens for i in range(len(phrase) - 1))
    return matches * 2 >= len(phrase) - 1


def semantic_floor(model):
    name = model.casefold().rsplit("/", 1)[-1].split(":", 1)[0]
    return BGE_M3_SEMANTIC_FLOOR if name == "bge-m3" else DEFAULT_SEMANTIC_FLOOR


def evidence_identity(reference):
    text = " ".join(_normalize(reference["text"]).split())
    time_depends_on_source = has_anchored_date_reference(text) or _VAGUE_RELATIVE_TIME.search(text)
    timestamp = reference["recorded_at"] if time_depends_on_source else None
    return reference["role"], reference["source_kind"], text, timestamp
