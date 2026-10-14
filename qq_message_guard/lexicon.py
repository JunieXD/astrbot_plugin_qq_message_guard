"""A small meaning dictionary. Matches are review candidates, never verdicts."""

from __future__ import annotations

import unicodedata
from functools import lru_cache

from .config import GuardError

BUILTIN_TERMS = (
    ("政治事件表达", ("beijing baoluan", "北京暴乱"), "政治事件相关表达，须按群规和当前语境判断"),
    (
        "宗教政治话题",
        ("法轮功", "法沦功", "fa lun gong"),
        "特定宗教政治运动及近音写法，按当前内容所属类别审核",
    ),
    ("粗俗性俚语", ("牛子",), "男性生殖器的俗称；医学、求助、举报和制止不等于性玩笑"),
)


def normalize(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())


@lru_cache(maxsize=8)
def compiled(terms):
    entries = []
    for label, aliases, meaning in BUILTIN_TERMS + terms:
        for alias in aliases:
            key = normalize(alias)
            if not key:
                raise GuardError("词义写法不能只含符号或空白。")
            entries.append((key, label, alias, meaning))
    return entries


def matches(text, terms=()):
    text = normalize(text)
    found, labels = [], set()
    for key, label, alias, meaning in compiled(terms):
        if key in text and label not in labels:
            found.append({"类别": label, "匹配写法": alias, "释义": meaning})
            labels.add(label)
    return found


def hints(message, context, terms=(), ocr_texts=()):
    quoted = [row["text"] for row in context if row.get("quoted")]
    current = matches("\n".join([message["text"], *quoted, *ocr_texts]), terms)
    definitions, labels = list(current), {hit["类别"] for hit in current}
    for row in context:
        if row["user"] != message["user"]:
            continue
        for hit in matches(row["text"], terms):
            if hit["类别"] not in labels:
                definitions.append({**hit, "来源": "同一作者的前文，仅消歧，不代表当前违规"})
                labels.add(hit["类别"])
    # Keep an administrator's large custom dictionary from consuming the context budget.
    return current, definitions[:12]


def screen_output(text, terms=()):
    if matches(text, terms):
        raise GuardError("警告内容重复了词库中的待复核表达，请修改警告模板。")
