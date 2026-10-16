"""Apply backend results to notification and automatic-action policy."""

from dataclasses import dataclass

from .categories import CATEGORIES
from .classification import actionable_categories, matched_categories


@dataclass(frozen=True)
class Decision:
    flagged: bool
    recall: bool
    warn: bool
    notify: bool
    reason: str


def decide(result, settings):
    if settings.backend == "qwen3guard":
        flagged = bool(matched_categories(result, settings))
        notify = bool(actionable_categories(result, settings))
        automatic = notify and (result.safety == "Unsafe" or settings.qwen.act_on_controversial)
        reason = ""
        if flagged and not notify:
            reason = "仅命中设为本地记录的有争议类别，不通知、不自动撤回或@"
        elif flagged and not automatic:
            reason = "模型判为有争议，仅供管理员复核，不自动撤回或@"
        return Decision(
            flagged,
            automatic,
            automatic,
            notify,
            reason,
        )
    flagged = any(result.scores[c.key] >= c.review_at for c in settings.checks)
    notify = any(result.scores[c.key] >= c.notify_at for c in settings.checks)
    punishable = [c for c in settings.checks if CATEGORIES[c.name].punishable]
    return Decision(
        flagged,
        any(result.scores[c.key] >= c.recall_at for c in punishable),
        any(result.scores[c.key] >= c.warn_at for c in punishable),
        notify,
        "未达到管理员通知门槛，不发送管理员通知" if flagged and not notify else "",
    )
