from __future__ import annotations

from dataclasses import asdict, dataclass

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


def model_state(message, policy, context, image_count):
    state = {
        "群规": policy.rules,
        "背景": [{"发言者": c["user"], "内容": prompt_text(c["text"])} for c in context],
        "当前待审核消息": {"发言者": message["user"], "内容": prompt_text(message["text"])},
    }
    if image_count:
        state["当前待审核消息"]["图片"] = " ".join(f"<image:{i + 1}>" for i in range(image_count))
    return state
