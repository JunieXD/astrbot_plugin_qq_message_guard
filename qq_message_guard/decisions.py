"""Only independent model category scores decide completed audits."""

from dataclasses import dataclass

from .categories import CATEGORIES
from .classification import matched_categories


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
        return Decision(flagged, flagged, flagged, flagged, "")
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
