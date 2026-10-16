"""Native Qwen guard labels; these are decisions, never probabilities."""

QWEN_CATEGORIES = {
    "Violent": "暴力内容",
    "Non-violent Illegal Acts": "违法行为",
    "Sexual Content or Sexual Acts": "色情",
    "PII": "隐私信息",
    "Suicide & Self-Harm": "自伤内容",
    "Unethical Acts": "不当行为",
    "Politically Sensitive Topics": "敏感话题",
    "Copyright Violation": "版权侵权",
    "Jailbreak": "越狱指令",
}
QWEN_DEFAULT_NAMES = ("敏感话题", "暴力内容", "不当行为", "违法行为", "色情")
SAFETY_NAMES = {"Safe": "安全", "Controversial": "有争议", "Unsafe": "不安全"}


def matched_categories(result, settings):
    labels = ("Unsafe", "Controversial") if settings.qwen.include_controversial else ("Unsafe",)
    if result.safety not in labels:
        return ()
    enabled = set(settings.qwen.categories)
    return tuple(c for c in result.categories if QWEN_CATEGORIES[c] in enabled)


def classification_text(data):
    value = data.get("classification", {})
    categories = value.get("matched") or value.get("categories") or ()
    names = "、".join(QWEN_CATEGORIES.get(c, c) for c in categories)
    label = SAFETY_NAMES.get(value.get("safety"), "结果未知")
    return f"{names} · {label}" if names else label
