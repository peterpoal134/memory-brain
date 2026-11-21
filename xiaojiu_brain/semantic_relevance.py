"""Check explicit names and artifact kinds before admitting a vector hit."""

import re
import unicodedata


_IDENTIFIER = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9]*(?:[-_.][A-Za-z0-9]+)*(?:\+\+|#)?")
_LOCAL_SUBJECT = re.compile(rf"(?P<name>{_IDENTIFIER.pattern})(?:\s*的|['’]s)?\s*$")
_HAN = re.compile(r"[\u3400-\u9fff\U00020000-\U0003134f]")
_COMMON_CAPITALIZED = frozenset((
    "monday tuesday wednesday thursday friday saturday sunday january february march april may june july august "
    "september october november december please where when which compare show explain find could what why how"
).split())
_GENERIC_ARTIFACT_SUBJECTS = frozenset((
    "a an the its their his her my your our this that those these which what any another new old current "
    "default local global main primary secondary server service client app application host system user "
    "network router gateway switch device model project program test worker security api runtime file"
).split())

# The query uses the more specific forms below. A source may refer to that
# artifact by an alias or a known suffix, and an unclassified source can still
# be matched by semantics. A source about an explicitly different artifact is
# insufficient, even when both texts name the same product.
_ARTIFACT_KINDS = tuple((re.compile(query), re.compile(source)) for query, source in (
    (r"配置|设定文件|\b(?:config(?:uration)?|settings)\b",
     r"配置|设定|\b(?:config(?:uration)?|settings)\b|\.(?:toml|ini|conf)\b"),
    (r"日志|\b(?:log files?|logs|runtime log)\b",
     r"日志|\b(?:logs?|logging)\b|\.log\b"),
    (r"备份文件|备份副本|\bbackup (?:files?|copies|copy)\b",
     r"备份|\bbackups?\b"),
    (r"使用手册|安装指南|\b(?:user manuals?|installation guides?)\b",
     r"文档|手册|指南|\b(?:documentation|manuals?|guides?)\b"),
    (r"测试报告|验收报告|\b(?:test|verification) reports?\b",
     r"测试报告|验收报告|测试结果|\b(?:test|verification) (?:reports?|results?)\b"),
))


def _explicit_names(query):
    mixed = bool(_HAN.search(query))
    for match in _IDENTIFIER.finditer(query):
        value = match.group()
        if len(value) == 1 or value.casefold() in _COMMON_CAPITALIZED:
            continue
        strong_spelling = any(char.isupper() or char.isdigit() for char in value[1:]) or value.endswith(("++", "#"))
        title_inside_sentence = value[0].isupper() and (mixed or match.start() > 0)
        if strong_spelling or title_inside_sentence:
            yield value.casefold()


class SemanticSignal:
    """Reject observable contradictions independent of embedding similarity."""

    def __init__(self, query):
        query = unicodedata.normalize("NFKC", query).strip()
        text = query.casefold()
        names = set(_explicit_names(query))
        kinds = set()
        for index, (pattern, _) in enumerate(_ARTIFACT_KINDS):
            for match in pattern.finditer(text):
                kinds.add(index)
                subject = _LOCAL_SUBJECT.search(text, max(0, match.start() - 128), match.start())
                if subject and subject["name"] not in _GENERIC_ARTIFACT_SUBJECTS:
                    names.add(subject["name"])
        self.names = frozenset(names)
        self.kinds = frozenset(kinds)

    def matches(self, text):
        if not self.names and not self.kinds:
            return True
        text = unicodedata.normalize("NFKC", text).casefold()
        if self.names:
            names = set(_IDENTIFIER.findall(text))
            # A base product can be named as a specific hyphenated version in
            # an original. An explicit version in the query still needs that
            # exact version rather than a different version of the same name.
            aliases = {name.split("-", 1)[0] for name in names}
            if not (self.names & (names | aliases)):
                return False
        if not self.kinds:
            return True
        source_kinds = {index for index, (_, pattern) in enumerate(_ARTIFACT_KINDS) if pattern.search(text)}
        return not source_kinds or bool(self.kinds.intersection(source_kinds))
