from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from .config import GuardError, fingerprint, qq


def message_id(value):
    if isinstance(value, bool):
        raise GuardError("消息ID无效。")
    text = str(value)
    try:
        valid = len(text) <= 20 and text.lstrip("-").isascii() and text.lstrip("-").isdecimal()
        if not valid or not 0 < abs(int(text)) < 2**53:
            raise ValueError()
    except (ValueError, TypeError) as exc:
        raise GuardError("消息ID无效。") from exc
    return str(int(text))


def signature(segments):
    """Stable content identity; do not compare expiring image download URLs."""
    if not isinstance(segments, list):
        raise GuardError("需要OneBot数组消息格式。")
    parts = []
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get("data"), dict):
            raise GuardError("消息段格式无效。")
        kind, data = segment.get("type"), segment["data"]
        if kind == "text":
            value = data.get("text")
        elif kind == "image":
            value = data.get("file")
            if not isinstance(value, str) or not value or len(value) > 500:
                raise GuardError("图片没有可核对的文件标识。")
        elif kind in ("reply", "at", "face"):
            value = str(data.get({"reply": "id", "at": "qq", "face": "id"}[kind], ""))
        else:
            value = data
        parts.append([kind, value])
    return fingerprint(parts)


@dataclass(frozen=True)
class Message:
    platform: str
    account: str
    group: str
    user: str
    mid: str
    at: int
    role: str
    text: str
    images: tuple[str, ...]
    reply: str
    unsupported: tuple[str, ...]
    content_hash: str
    sender_name: str = ""

    @property
    def event_key(self):
        return fingerprint([self.platform, self.account, self.group, self.mid, self.at])

    @property
    def scope(self):
        return f"{self.platform}:{self.account}:{self.group}"

    def data(self):
        return asdict(self)

    @classmethod
    def parse(cls, raw, platform, limit=8000):
        if raw.get("post_type") != "message" or raw.get("message_type") != "group":
            return None
        account, gid, uid = (qq(raw.get(k, ""), k) for k in ("self_id", "group_id", "user_id"))
        if account == uid:
            return None
        stamp = raw.get("time")
        if type(stamp) is not int or not 0 < stamp < 2**40:
            raise GuardError("群消息时间无效。")
        if not isinstance(platform, str) or not 1 <= len(platform) <= 200:
            raise GuardError("平台标识无效。")
        mid = message_id(raw.get("message_id"))
        segments = raw.get("message")
        identity = signature(segments)
        texts, images, unknown, reply = [], [], [], ""
        for segment in segments:
            kind, data = segment.get("type"), segment["data"]
            if kind == "text":
                if isinstance(data.get("text"), str):
                    texts.append(data["text"])
                else:
                    unknown.append("文字格式无效")
            elif kind == "image":
                url = data.get("url")
                if isinstance(url, str) and len(url) <= 4000:
                    images.append(url)
                else:
                    unknown.append("图片没有下载地址")
            elif kind == "reply":
                try:
                    reply = message_id(data.get("id"))
                except GuardError:
                    unknown.append("引用消息ID无效")
            elif kind == "at":
                texts.append("@" + str(data.get("qq", ""))[:20])
            elif kind == "face":
                texts.append("[QQ表情]")
            else:
                unknown.append(str(kind)[:40] or "未知消息段")
        text = "".join(texts).strip()
        if len(text) > limit:
            text = text[:limit]
            unknown.append("文字过长，仅保留开头")
        sender = raw.get("sender")
        sender = sender if isinstance(sender, dict) else {}
        name = sender.get("card") or sender.get("nickname") or ""
        name = name[:80] if isinstance(name, str) else ""
        return cls(
            platform,
            account,
            gid,
            uid,
            mid,
            stamp,
            str(sender.get("role", "unknown")),
            text,
            tuple(images[:5]),
            reply,
            tuple(dict.fromkeys(unknown)),
            identity,
            name,
        )


def prompt_text(text):
    # Reserve media placeholders and state delimiters for the application.
    return (
        str(text)
        .replace("<image:", "〈image:")
        .replace("<video:", "〈video:")
        .replace("<state>", "〈state〉")
        .replace("</state>", "〈/state〉")
    )


def audit_text(message, quote=None):
    """Actual reply content is part of this publication, unlike ordinary context."""
    text = prompt_text(message["text"])
    if quote is not None:
        return f"正文：{text}\n实际引用内容：{prompt_text(quote['text'])}"
    return text


def bounded_context(context, budget):
    """Prefer quotes and newer messages; preserve receive order in the final prompt."""
    selected, remaining = set(), budget
    priority = sorted(range(len(context)), key=lambda i: (bool(context[i].get("quoted")), i), reverse=True)
    for i in priority:
        size = len(context[i]["text"])
        if size <= remaining:
            selected.add(i)
            remaining -= size
    return [row for i, row in enumerate(context) if i in selected]


def model_state(message, policy, context, image_count, definitions=(), ocr_texts=()):
    authors = {}

    def author(uid):
        if uid not in authors:
            authors[uid] = f"U{len(authors) + 1}"
        return authors[uid]

    def stamp(at):
        if not at:
            return ""
        return datetime.fromtimestamp(at, timezone(timedelta(hours=8))).strftime("%m-%d %H:%M")

    quote = next((row for row in context if row.get("quoted")), None)
    rows = [[stamp(c.get("at")), author(c["user"]), prompt_text(c["text"])] for c in context]
    state = {
        "群规": policy.rules,
        "背景字段": ["北京时间", "作者", "内容"],
        "背景": rows,
        "当前待审核消息": {
            "北京时间": stamp(message.get("at")),
            "作者": author(message["user"]),
            "内容": audit_text(message, quote),
        },
    }
    if quote:
        state["当前待审核消息"]["引用作者"] = author(quote["user"])
    if definitions:
        state["词义提示"] = {
            "说明": "只解释词义，不是违规结论；前文仅消歧，实际引用按类别定义审核",
            "候选": list(definitions),
        }
    if ocr_texts:
        state["当前待审核消息"]["图片文字"] = [prompt_text(text) for text in ocr_texts]
    if image_count:
        state["当前待审核消息"]["图片"] = " ".join(f"<image:{i + 1}>" for i in range(image_count))
    return state
