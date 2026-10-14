from __future__ import annotations

import hashlib
import json
import math
import string
from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit

from .categories import CATEGORIES, CATEGORY_ALIASES, DEFAULT_NAMES, QUESTIONS


class GuardError(Exception):
    """Safe, administrator-facing error without transport credentials."""


class InputRejected(GuardError):
    """The model rejected this input; it is not a service-wide outage."""


class Deferred(GuardError):
    """No write submitted; a later explicit action may be possible."""


class Uncertain(GuardError):
    """A write may have reached QQ. Never repeat it automatically."""


DEFAULT_WARNING = "请遵守群规。你刚才的发言可能违反了本群规定，请停止相关内容；如有异议，请联系管理员复核。"
DEFAULT_RULES = (
    "允许普通升学、政策、旅游、医学和健康教育，以及正规服务和合理批评。"
    "禁止政治敏感事件或邪教组织的提及、影射、谐音和拼音表达，举报、劝阻或引用中出现也不豁免。"
    "禁止色情资源、露骨性内容及未成年人性化表达、诈骗和违法招揽、"
    "现实暴力威胁与群体仇恨。按当前内容和实际引用判断，其他人的背景消息仅帮助消歧。"
)
QUESTION_VERSION = "qq-message-guard-content-v5"


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def integer(value, label, low, high):
    if type(value) is bool:
        raise GuardError(f"{label}应为 {low}～{high} 的整数。")
    text = str(value)
    if len(text) > 20 or not text.isascii() or not text.isdecimal() or not low <= int(text) <= high:
        raise GuardError(f"{label}应为 {low}～{high} 的整数。")
    return int(text)


def qq(value, label="QQ号", optional=False):
    text = str(value).strip()
    if optional and not text:
        return ""
    return str(integer(text, label, 10000, 9999999999999))


def number(value, label, low, high):
    if isinstance(value, bool):
        raise GuardError(f"{label}应为 {low}～{high} 的数值。")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise GuardError(f"{label}应为 {low}～{high} 的数值。") from exc
    if not math.isfinite(parsed) or not low <= parsed <= high:
        raise GuardError(f"{label}应为 {low}～{high} 的数值。")
    return parsed


def switch(raw, key, default):
    value = raw.get(key, default)
    if type(value) is not bool:
        raise GuardError(f"{key}请使用开关。")
    return value


@dataclass(frozen=True)
class Check:
    name: str = "群规"
    review_at: float = 0.55
    recall_at: float = 0.90
    warn_at: float = 0.90
    notify_at: float = 0.80

    def __post_init__(self):
        object.__setattr__(self, "name", CATEGORY_ALIASES.get(self.name, self.name))

    @property
    def key(self):
        return QUESTIONS[self.name][0]


@dataclass(frozen=True)
class Policy:
    group_id: str
    enabled: bool = True
    auto_recall: bool = False
    warn_member: bool = False
    notify_private: bool = False
    notify_group: bool = False
    admin_group: str = ""
    warning_text: str = DEFAULT_WARNING
    rules: str = DEFAULT_RULES
    bot_qq: str = ""
    exempt_users: tuple[str, ...] = ()
    keyword_review: bool = True
    display_name: str = ""

    @property
    def revision(self):
        return fingerprint(asdict(self))


@dataclass(frozen=True)
class Advanced:
    queue_size: int = 100
    max_queue_seconds: int = 90
    context_messages: int = 24
    sender_context_messages: int = 6
    context_cache_messages: int = 512
    context_chars: int = 4000
    max_text_chars: int = 4000
    max_images: int = 2
    image_mb: int = 8
    image_side: int = 1280
    image_timeout: int = 12
    max_action_seconds: int = 120
    action_min_seconds: float = 0
    action_max_seconds: float = 0.3
    recall_gap_min: float = 0.2
    recall_gap_max: float = 0.6
    account_gap_min: float = 2
    account_gap_max: float = 5
    writes_per_hour: int = 30
    recalls_per_hour: int = 180
    reads_per_hour: int = 600
    notify_minutes: int = 1
    notify_cooldown_minutes: int = 60
    recovery_seconds: int = 30
    retention_days: int = 14
    harmful_retention_days: int = 60
    warn_cooldown_minutes: int = 60
    group_warnings_per_hour: int = 10
    log_mb: int = 10


@dataclass(frozen=True)
class Batch:
    enabled: bool = True
    max_messages: int = 8
    wait_seconds: int = 20
    overlap_messages: int = 10
    threshold: float = 0.30
    spot_check_percent: int = 0


@dataclass(frozen=True)
class Evidence:
    notify_images: bool = False
    review_images: bool = True
    line_width: int = 6
    context_messages: int = 3
    font_path: str = ""


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    groups: tuple[Policy, ...] = ()
    checks: tuple[Check, ...] = tuple(Check(name) for name in DEFAULT_NAMES)
    base_url: str = "http://127.0.0.1:8000"
    model: str = "jev-latest"
    api_key: str = field(default="", repr=False)
    timeout_seconds: int = 90
    images_enabled: bool = True
    notify_users: tuple[str, ...] = ()
    advanced: Advanced = field(default_factory=Advanced)
    ocr_enabled: bool = True
    ocr_timeout: int = 15
    # Immutable (label, aliases, meaning) entries, merged with built-ins.
    terms: tuple[tuple[str, tuple[str, ...], str], ...] = ()
    batch: Batch = field(default_factory=Batch)
    evidence: Evidence = field(default_factory=Evidence)

    def group(self, gid):
        return next((p for p in self.groups if p.group_id == gid), None)

    @property
    def screen_threshold(self):
        return min(self.batch.threshold, *(check.review_at for check in self.checks))

    @property
    def revision(self):
        return fingerprint(
            {
                **asdict(self),
                "question_version": QUESTION_VERSION,
                "definitions": {check.name: asdict(CATEGORIES[check.name]) for check in self.checks},
            }
        )


def parse_settings(raw):
    if not isinstance(raw, dict):
        raise GuardError("插件配置应为对象。")
    evidence = raw.get("evidence", {})
    if not isinstance(evidence, dict):
        raise GuardError("证据图片配置应为对象。")
    font_path = evidence.get("font_path", "")
    if not isinstance(font_path, str) or len(font_path) > 1000 or any(c in font_path for c in "\r\n\x00"):
        raise GuardError("证据字体路径应为本机字体文件路径。")
    pictures = Evidence(
        notify_images=switch(evidence, "notify_images", False),
        review_images=switch(evidence, "review_images", True),
        line_width=integer(evidence.get("line_width", 6), "证据横线粗细", 0, 10),
        context_messages=integer(evidence.get("context_messages", 3), "证据前文条数", 0, 5),
        font_path=font_path.strip(),
    )
    batch = raw.get("batch", {})
    if not isinstance(batch, dict):
        raise GuardError("批量初筛配置应为对象。")
    batching = Batch(
        enabled=switch(batch, "enabled", True),
        max_messages=integer(batch.get("max_messages", 8), "每批最多新消息", 2, 20),
        wait_seconds=integer(batch.get("wait_seconds", 20), "初筛最长等待秒数", 1, 30),
        overlap_messages=integer(batch.get("overlap_messages", 10), "初筛重叠背景条数", 0, 24),
        threshold=number(batch.get("threshold", 0.30), "初筛继续审核门槛", 0.01, 0.5),
        spot_check_percent=integer(batch.get("spot_check_percent", 0), "初筛抽查百分比", 0, 100),
    )
    groups = raw.get("groups", [])
    if not isinstance(groups, list) or len(groups) > 50:
        raise GuardError("最多配置50个群。")
    policies = []
    for item in groups:
        if not isinstance(item, dict):
            raise GuardError("群配置应为对象。")
        rules = item.get("rules", DEFAULT_RULES)
        if not isinstance(rules, str) or not 1 <= len(rules.strip()) <= 3000:
            raise GuardError("群规应为1～3000字。")
        warning = item.get("warning_text", DEFAULT_WARNING)
        if not isinstance(warning, str) or not 1 <= len(warning.strip()) <= 1000:
            raise GuardError("群内警告内容应为1～1000字。")
        try:
            for _, name, spec, conversion in string.Formatter().parse(warning):
                if name is not None and (name not in ("群号", "QQ号", "审核项") or spec or conversion):
                    raise ValueError()
        except ValueError as exc:
            raise GuardError("警告占位符只支持 {群号}、{QQ号}、{审核项}。") from exc
        group_id = qq(item.get("group_id", ""), "群号")
        display_name = item.get("display_name", "")
        if not isinstance(display_name, str) or len(display_name.strip()) > 40:
            raise GuardError("通知中显示的群名称最多40字。")
        admin_group = qq(item.get("admin_group", ""), "管理员群号", optional=True)
        notify_group = switch(item, "notify_group", False)
        if notify_group and (not admin_group or admin_group == group_id):
            raise GuardError("开启管理员群通知时请填写不同于被审核群的管理员群号。")
        users = item.get("exempt_users", [])
        if not isinstance(users, list) or len(users) > 100:
            raise GuardError("忽略名单最多100项。")
        policies.append(
            Policy(
                group_id=group_id,
                enabled=switch(item, "enabled", True),
                auto_recall=switch(item, "auto_recall", False),
                warn_member=switch(item, "warn_member", False),
                notify_private=switch(item, "notify_private", False),
                notify_group=notify_group,
                admin_group=admin_group,
                warning_text=warning,
                rules=rules.strip(),
                bot_qq=qq(item.get("bot_qq", ""), optional=True),
                exempt_users=tuple(sorted({qq(x) for x in users})),
                keyword_review=switch(item, "keyword_review", True),
                display_name=display_name.strip(),
            )
        )
    if len({p.group_id for p in policies}) != len(policies):
        raise GuardError("群号重复，请每群仅配置一条。")
    checks = raw.get("checks", [asdict(check) for check in Settings.checks])
    if not isinstance(checks, list) or not 1 <= len(checks) <= len(QUESTIONS):
        raise GuardError(f"审核项目应为1～{len(QUESTIONS)}项。")
    parsed = []
    for check in checks:
        if not isinstance(check, dict):
            raise GuardError("审核项目名称不正确。")
        name = check.get("name")
        if not isinstance(name, str) or (name := CATEGORY_ALIASES.get(name, name)) not in QUESTIONS:
            raise GuardError("审核项目名称不正确。")
        review = number(check.get("review_at", 0.55), "复核门槛", 0.01, 1)
        recall = number(check.get("recall_at", 0.90), "自动撤回门槛", review, 1)
        warn = number(check.get("warn_at", 0.90), "群内警告门槛", review, 1)
        notify = number(check.get("notify_at", max(0.80, review)), "管理员通知门槛", review, 1)
        parsed.append(Check(name, review, recall, warn, notify))
    if len({c.name for c in parsed}) != len(parsed):
        raise GuardError("审核项目不能重复。")
    more = raw.get("advanced", {})
    if not isinstance(more, dict):
        raise GuardError("高级配置应为对象。")
    ranges = {
        "queue_size": (1, 1000),
        "max_queue_seconds": (5, 600),
        "context_messages": (0, 64),
        "sender_context_messages": (0, 16),
        "context_cache_messages": (128, 4096),
        "context_chars": (500, 12000),
        "max_text_chars": (200, 8000),
        "max_images": (1, 4),
        "image_mb": (1, 20),
        "image_side": (256, 2048),
        "image_timeout": (3, 30),
        "max_action_seconds": (10, 600),
        "writes_per_hour": (1, 300),
        "recalls_per_hour": (1, 1000),
        "reads_per_hour": (30, 3600),
        "notify_minutes": (1, 1440),
        "notify_cooldown_minutes": (1, 1440),
        "recovery_seconds": (30, 3600),
        "retention_days": (1, 90),
        "harmful_retention_days": (1, 180),
        "warn_cooldown_minutes": (1, 1440),
        "group_warnings_per_hour": (1, 100),
        "log_mb": (1, 100),
    }
    values = {k: integer(more.get(k, getattr(Advanced, k)), k, *bounds) for k, bounds in ranges.items()}
    if values["harmful_retention_days"] < values["retention_days"]:
        raise GuardError("可疑记录保留天数不能短于普通记录。")
    for name in (
        "action_min_seconds",
        "action_max_seconds",
        "account_gap_min",
        "account_gap_max",
        "recall_gap_min",
        "recall_gap_max",
    ):
        values[name] = number(more.get(name, getattr(Advanced, name)), name, 0, 30)
    if (
        values["action_max_seconds"] < values["action_min_seconds"]
        or values["account_gap_max"] < values["account_gap_min"]
        or values["recall_gap_max"] < values["recall_gap_min"]
    ):
        raise GuardError("最长操作等待不能小于最短等待。")
    url = raw.get("base_url", "http://127.0.0.1:8000")
    if not isinstance(url, str):
        raise GuardError("模型服务地址应为HTTP地址。")
    try:
        parts = urlsplit(url)
        parts.port
    except ValueError as exc:
        raise GuardError("模型服务地址不正确。") from exc
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise GuardError("模型地址不能包含用户名、密码、查询参数或片段。")
    model = raw.get("model", "jev-latest")
    key = raw.get("api_key", "")
    if not isinstance(model, str) or not 1 <= len(model.strip()) <= 200:
        raise GuardError("模型名称不正确。")
    if not isinstance(key, str) or len(key) > 1000 or any(x in key for x in "\r\n"):
        raise GuardError("API密钥不正确。")
    users = raw.get("notify_users", [])
    if not isinstance(users, list) or len(users) > 5:
        raise GuardError("通知接收人最多5位。")
    if any(p.notify_private for p in policies) and not users:
        raise GuardError("开启私聊通知时请填写通知接收人QQ。")
    terms = raw.get("terms", [])
    if not isinstance(terms, list) or len(terms) > 100:
        raise GuardError("自定义词义最多100项。")
    parsed_terms = []
    for term in terms:
        if not isinstance(term, dict):
            raise GuardError("自定义词义应为对象。")
        label, aliases, meaning = term.get("name"), term.get("aliases"), term.get("meaning")
        if (
            not isinstance(label, str)
            or not 1 <= len(label.strip()) <= 40
            or not isinstance(meaning, str)
            or not 1 <= len(meaning.strip()) <= 200
            or not isinstance(aliases, list)
            or not 1 <= len(aliases) <= 20
            or any(not isinstance(a, str) or not 1 <= len(a.strip()) <= 80 for a in aliases)
        ):
            raise GuardError("词义需填写名称、1～20个写法和简短含义。")
        parsed_terms.append((label.strip(), tuple(a.strip() for a in aliases), meaning.strip()))
    from .lexicon import compiled

    compiled(tuple(parsed_terms))  # Reject symbol-only aliases before receiving any messages.
    return Settings(
        enabled=switch(raw, "enabled", False),
        groups=tuple(policies),
        checks=tuple(parsed),
        base_url=url.rstrip("/"),
        model=model.strip(),
        api_key=key,
        timeout_seconds=integer(raw.get("timeout_seconds", 90), "审核超时秒数", 5, 120),
        images_enabled=switch(raw, "images_enabled", True),
        notify_users=tuple(sorted({qq(x) for x in users})),
        advanced=Advanced(**values),
        ocr_enabled=switch(raw, "ocr_enabled", True),
        ocr_timeout=integer(raw.get("ocr_timeout", 15), "OCR超时秒数", 3, 60),
        terms=tuple(parsed_terms),
        batch=batching,
        evidence=pictures,
    )
