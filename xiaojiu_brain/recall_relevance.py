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
_SENTENCE_BREAK = re.compile(r"[。!?;\r\n]+|\.(?=\s|$)")
_TECHNICAL_C = re.compile(r"(?<![a-z0-9_])c(?:\+\+|#)(?![a-z0-9_])")
_CJK_POLITE_PREFIX = re.compile(
    r"^(?:(?:我)?想(?:请问|请教|问)|请问|请教|劳驾|麻烦(?:你|您)?告诉我|问一下)"
    r"(?:一?下)?(?:(?:你|您)(?:知道|记得))?"
)
_MIXED_POSSESSIVE = re.compile(r"(?<=[a-z0-9+#])\s*的(?=[\u3400-\u9fff\U00020000-\U0003134f])")
_LOCATIVE_PREFIX = re.compile(
    r"^(?:please[, ]+)?where\s+(?:(?:did|do|does|can|could|would)\s+(?:i|we|you)\s+"
    r"(?:leave|put|find|see|keep|check)\b)?"
)
_LOCATIVE_SUFFIX = re.compile(r"\s+(?:located|stored|kept|listed|saved|recorded)[.!?]*\s*$")
_CJK_QUERY_FILLERS = re.compile(
    r"(?:(?:存放|保存|存|放)(?:到|在|于)|位于|在)?(?:哪里|哪儿|何处)(?:能找到|可以找到|有)?|"
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
    text = _MIXED_POSSESSIVE.sub(" ", text)
    for run in _CJK_RUN.findall(_CJK_QUERY_FILLERS.sub(" ", text)):
        run = _CJK_POLITE_PREFIX.sub("", run).rstrip("了吗呢呀啊")
        if len(run) >= 2 and run not in _STOP_TERMS and not low_information(run):
            yield run


class LexicalSignal:
    """Shared topic terms constrain FTS retrieval and corroborate its results."""

    def __init__(self, query):
        query = _normalize(query)
        self.phrases = tuple(_query_phrases(query))
        words_query, located = _LOCATIVE_PREFIX.subn("", query)
        if located:
            words_query = _LOCATIVE_SUFFIX.sub("", words_query)
        self.words = {token for token in _lexical_tokens(words_query) if len(token) > 1
                      and not _CJK_RUN.fullmatch(token) and token not in _STOP_TERMS}
        self.technical = frozenset(_TECHNICAL_C.findall(query))
        bigrams = {phrase[i:i + 2] for phrase in self.phrases for i in range(len(phrase) - 1)}
        self.tokens = tuple(sorted(self.words | bigrams | ({"c"} if self.technical else set())))

    def matches(self, text):
        return bool(self.tokens) and any(self._matches_sentence(sentence)
                                         for sentence in _SENTENCE_BREAK.split(_normalize(text)))

    def _matches_sentence(self, sentence):
        if self.technical and not self.technical.intersection(_TECHNICAL_C.findall(sentence)):
            return False
        if self.words and not self.words <= set(_lexical_tokens(sentence)):
            return False
        runs = _CJK_RUN.findall(sentence)
        return all(_phrase_matches(phrase, runs) for phrase in self.phrases)


def _phrase_matches(phrase, runs):
    if len(phrase) <= 3:
        return any(phrase in run for run in runs)
    pairs = [phrase[i:i + 2] for i in range(len(phrase) - 1)]
    # Both the subject and qualifier must appear in the same local phrase.
    # Allow intervening function words without joining separate clauses.
    for run in runs:
        if phrase in run:
            return True
        start = run.find(pairs[0])
        end = run.rfind(pairs[-1])
        if 0 <= start < end:
            span_pairs = {run[i:i + 2] for i in range(start, end + 1)}
            matches = sum(pair in span_pairs for pair in pairs)
            if matches * 2 >= len(pairs):
                return True
    return False


def semantic_floor(model):
    name = model.casefold().rsplit("/", 1)[-1].split(":", 1)[0]
    return BGE_M3_SEMANTIC_FLOOR if name == "bge-m3" else DEFAULT_SEMANTIC_FLOOR


def evidence_identity(reference):
    text = " ".join(_normalize(reference["text"]).split())
    time_depends_on_source = has_anchored_date_reference(text) or _VAGUE_RELATIVE_TIME.search(text)
    timestamp = reference["recorded_at"] if time_depends_on_source else None
    return reference["role"], reference["source_kind"], text, timestamp
