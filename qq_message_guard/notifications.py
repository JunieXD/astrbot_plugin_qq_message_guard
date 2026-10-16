"""Readable administrator alerts; transport failures stay in logs and status."""

import unicodedata
from datetime import datetime, timedelta, timezone

from .classification import classification_text
from .config import QUESTIONS
from .lexicon import matches


def is_notifiable(case):
    return bool(
        case["flagged"]
        and (case["data"].get("scores") or case["data"].get("classification"))
        and case["data"].get("notification_ready")
        and not case["human"]
        and not case["data"].get("actions_pending")
        and not case["data"].get("cache_invalidated")
    )


def notification_text(cases, policy, settings):
    names = {v[0]: k for k, v in QUESTIONS.items()}
    topic_names = {"政治事件表达": "敏感话题", "宗教政治话题": "敏感话题", "粗俗性俚语": "粗俗性玩笑"}
    title = policy.display_name or policy.group_id
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
        # Old composite rules can only provide a topic hint and an overall score.
        legacy_topics = list(
            dict.fromkeys(
                topic_names.get(hit["类别"], hit["类别"]) for hit in data.get("keyword_matches", [])
            )
        )
        legacy_label = "/".join(legacy_topics[:3]) or "群规"
        evidence = (
            "、".join(
                f"{legacy_label if category == '群规' else category} · {score:.1%}"
                for category, score in hits[:3]
            )
            or "可能违反群规"
        )
        if data.get("classification"):
            evidence = classification_text(data)
        if case["state"] == "recalled":
            handled = "已撤回"
        elif case["state"] in ("unknown", "failed"):
            handled = "处理结果待核对"
        elif data.get("warned"):
            handled = "未撤回（已提醒）"
        else:
            handled = "未撤回"
        local = datetime.fromtimestamp(message["at"], timezone(timedelta(hours=8)))
        stamp = f"{local.month}月{local.day}日 {local.hour:02d}:{local.minute:02d}:{local.second:02d}"
        reference = f" · #{case['id']}" if settings.evidence.notify_images else ""
        parts.append(f"{name}（{message['user']}）{reference}\n{stamp}\n{evidence} · {handled}")
    return f"🛡️ {title} · 审核提醒\n\n" + "\n\n".join(parts)
