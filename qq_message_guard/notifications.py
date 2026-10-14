"""Readable administrator alerts; transport failures stay in logs and status."""

import unicodedata
from datetime import datetime, timedelta, timezone

from .config import QUESTIONS
from .lexicon import matches


def is_notifiable(case):
    return bool(
        case["flagged"]
        and case["data"].get("scores")
        and case["data"].get("notification_ready")
        and not case["human"]
        and not case["data"].get("actions_pending")
    )


def notification_text(cases, policy, settings):
    names = {v[0]: k for k, v in QUESTIONS.items()}
    topic_names = {"政治事件表达": "敏感话题", "宗教政治话题": "敏感话题", "粗俗性俚语": "粗俗性玩笑"}
    title = f"{policy.display_name}（{policy.group_id}）" if policy.display_name else policy.group_id
    parts = []
    for case in cases[:8]:
        data, message = case["data"], case["data"]["message"]
        name = "".join(
            c for c in message.get("sender_name", "") if not unicodedata.category(c).startswith("C")
        )[:40]
        if not name.strip() or matches(name, settings.terms):
            name = "群成员"
        hits = sorted(
            (
                (names.get(check.key, check.name), data.get("scores", {}).get(check.key, 0))
                for check in settings.checks
                if data.get("scores", {}).get(check.key, 0) >= check.notify_at
            ),
            key=lambda hit: hit[1],
            reverse=True,
        )
        topics = [name for name, _ in hits if name != "群规"]
        if not topics:  # Legacy composite rules have no independent category score.
            topics = [topic_names.get(hit["类别"], hit["类别"]) for hit in data.get("keyword_matches", [])]
        topics = list(dict.fromkeys(topics))
        suspicion = "、".join(topics[:3]) if topics else "可能违反群规，需要人工判断"
        confidence = "、".join(f"{name} {score:.1%}" for name, score in hits[:3])
        if case["state"] == "recalled":
            handled = "原消息已撤回"
        elif case["state"] in ("unknown", "failed"):
            handled = "处理结果需要人工核对"
        elif data.get("warned"):
            handled = "已提醒成员，仍需人工复核"
        else:
            handled = "当前未自动处理，请人工复核"
        local = datetime.fromtimestamp(message["at"], timezone(timedelta(hours=8)))
        stamp = f"{local.month}月{local.day}日 {local.hour:02d}:{local.minute:02d}:{local.second:02d}"
        parts.append(
            f"{name}（QQ {message['user']}）\n发言时间：{stamp}\n疑点：{suspicion}"
            f"\n置信度：{confidence or '暂无'}（模型评分）\n处理：{handled}"
        )
    return f"🛡️ 发言审核提醒\n来源群：{title}\n" + "\n\n".join(parts)
