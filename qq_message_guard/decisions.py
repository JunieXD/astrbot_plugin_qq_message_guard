"""Model evidence, keyword review and attribution have separate responsibilities."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Decision:
    flagged: bool
    recall: bool
    warn: bool
    notify: bool
    reason: str


def decide(result, settings, policy, current_matches):
    model_hit = any(result.scores[c.key] >= c.review_at for c in settings.checks)
    keyword_hit = bool(policy.keyword_review and current_matches)
    intent = result.intent
    # A confident attribution may dismiss a keyword-only match. A conflicting model
    # violation score stays reviewable, but must never automatically punish a reporter.
    if settings.intent_enabled and intent and intent.dismisses_keyword(settings.intent_confidence):
        keyword_hit = False
    flagged = model_hit or keyword_hit
    action_ok = not settings.intent_enabled or bool(
        intent and intent.permits_action(settings.intent_confidence)
    )
    reasons = []
    if keyword_hit:
        reasons.append("当前消息命中词义词库，仅提示复核，不直接处罚")
    if flagged and not action_ok:
        reasons.append("发言意图为举报制止、正常或不明确，仅留存复核，不自动操作或通知")
    notify = action_ok and any(result.scores[c.key] >= c.notify_at for c in settings.checks)
    if flagged and action_ok and not notify:
        reasons.append("未达到管理员通知门槛，不发送管理员通知")
    return Decision(
        flagged,
        action_ok and any(result.scores[c.key] >= c.recall_at for c in settings.checks),
        action_ok and any(result.scores[c.key] >= c.warn_at for c in settings.checks),
        notify,
        "；".join(reasons),
    )
