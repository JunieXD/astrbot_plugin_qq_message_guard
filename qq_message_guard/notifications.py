"""Readable administrator alerts; transport failures stay in logs and status."""

import unicodedata
from datetime import datetime, timedelta, timezone

from .config import QUESTIONS
from .lexicon import matches


def is_reviewable(case):
    return bool(
        case["flagged"]
        and case["data"].get("scores")
        and not case["human"]
        and not case["data"].get("actions_pending")
    )


def notification_text(cases, policy, settings):
    names = {v[0]: k for k, v in QUESTIONS.items()}
    topic_names = {"政治事件表达": "敏感话题", "宗教政治话题": "敏感话题", "粗俗性俚语": "粗俗性玩笑"}
    title = f"{policy.display_name}（{policy.group_id}）" if policy.display_name else policy.group_id
    parts = [f"🛡️ 发言审核提醒\n来源群：{title}\n\n发现 {len(cases)} 条需要复核的发言"]
    for index, case in enumerate(cases[:8], 1):
        data, message = case["data"], case["data"]["message"]
        name = "".join(
            c for c in message.get("sender_name", "") if not unicodedata.category(c).startswith("C")
        )[:40]
        if not name.strip() or matches(name, settings.terms):
            name = "群成员"
        topics = [topic_names.get(hit["类别"], hit["类别"]) for hit in data.get("keyword_matches", [])]
        topics += [
            names.get(check.key, check.name)
            for check in settings.checks
            if check.key != "rules" and data.get("scores", {}).get(check.key, 0) >= check.review_at
        ]
        topics = list(dict.fromkeys(topics))
        suspicion = "、".join(topics[:3]) if topics else "可能违反群规，需要人工判断"
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
            f"{index}. {name}（QQ {message['user']}）\n"
            f"发言时间：{stamp}\n疑点：{suspicion}\n处理：{handled}\n"
            f"查看：/消息审核 查看 {case['id']}"
        )
    if len(cases) > 8:
        parts.append(f"另有 {len(cases) - 8} 条，私聊 /消息审核 待复核 {policy.group_id} 查看。")
    parts.append("私聊机器人发送查看命令，可查看原文并复核。")
    return "\n\n".join(parts)
