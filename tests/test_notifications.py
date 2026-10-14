import re
from dataclasses import replace

from conftest import ADMIN, A

from qq_message_guard.notifications import notification_text


async def test_runtime_failures_never_notify_the_admin_group(env):
    env.configure(notify_group=True, admin_group="123456")
    env.media.failure = True
    await env.speak(segments=[{"type": "image", "data": {"file": "a.jpg", "url": "https://gchat.qpic.cn/a"}}])
    failed = await env.process()
    await env.s.send_alerts()
    assert failed["state"] == "incomplete" and not env.bot.writes
    env.model.failure = True
    await env.speak("法沦功")
    failed_model = await env.process()
    assert failed_model["flagged"]  # Preserves evidence, but it was never successfully evaluated.
    await env.s.send_alerts()
    assert not env.bot.writes


async def test_normal_messages_never_notify_but_detected_risk_does(env):
    env.configure(notify_group=True, admin_group="123456", display_name="鼠群")
    env.model.score = 0.1
    await env.speak("正常招生咨询")
    await env.process()
    await env.s.send_alerts()
    assert not env.bot.writes
    env.model.score = 0.95
    await env.speak("法沦功", sender={"card": "测试群名片", "nickname": "昵称", "role": "member"})
    await env.process()
    await env.s.send_alerts()
    assert len(env.bot.writes) == 1
    text = env.bot.writes[0][1]["message"][0]["data"]["text"]
    assert "鼠群" in text and "测试群名片" in text and "敏感话题" in text
    assert text.startswith("🛡️ 鼠群 · 审核提醒\n\n测试群名片（200001）\n")
    assert len(text.splitlines()) == 5 and "查看" not in text
    assert "敏感话题 · 95.0% · 未撤回" in text
    assert "未完整审核" not in text and "tokens" not in text and "法沦功" not in text
    assert re.search(r"\d+月\d+日 \d{2}:\d{2}:\d{2}", text)


async def test_detected_risk_in_partial_audit_can_notify(env):
    env.configure(notify_group=True, admin_group="123456")
    env.media.failure = True
    await env.speak(
        segments=[
            {"type": "text", "data": {"text": "模型发现这部分发言有疑点"}},
            {"type": "image", "data": {"file": "a.jpg", "url": "https://gchat.qpic.cn/a"}},
        ]
    )
    case = await env.process()
    await env.s.send_alerts()
    assert case["state"] == "incomplete" and case["data"]["scores"] and len(env.bot.writes) == 1


async def test_pending_legacy_failure_batch_cannot_leak_runtime_errors(env):
    env.configure(notify_private=True)
    env.box.settings = replace(env.box.settings, notify_users=(ADMIN,))
    env.media.failure = True
    await env.speak(segments=[{"type": "image", "data": {"file": "a.jpg", "url": "https://gchat.qpic.cn/a"}}])
    failed = await env.process()
    await env.speak("模型发现疑点")
    risk = await env.process()
    await env.store.call(
        "put",
        "alert-batch:" + failed["scope"],
        {
            "ids": [failed["id"], risk["id"]],
            "revision": env.box.settings.revision,
            "anchor": failed["id"],
        },
    )
    await env.s.send_alerts()
    text = env.bot.writes[0][1]["message"][0]["data"]["text"]
    assert len(re.findall(r"\d+月\d+日 \d{2}:\d{2}:\d{2}", text)) == 1 and "图片下载" not in text


async def test_offline_replay_is_recorded_but_never_audited_or_notified(env):
    env.configure(notify_group=True, admin_group="123456")
    await env.speak()
    await env.process()
    original_count = len(env.model.requests)
    env.clock.now += 30
    env.bot._wsr_api_clients = {A: object()}  # A new connection identity.
    _, old = await env.speak("断线期间的消息", time=int(env.clock()) - 10)
    assert old["state"] == "incomplete" and "连接前" in old["data"]["reason"]
    assert await env.process() is None and len(env.model.requests) == original_count
    await env.speak("重新在线后的新消息")
    fresh = await env.process()
    assert fresh["data"]["scores"] and len(env.model.requests) == original_count + 1
    assert all(row["text"] != "断线期间的消息" for row in fresh["data"]["context"])


async def test_notification_has_only_readable_evidence_and_limited_length(env):
    await env.speak("可疑发言")
    case = await env.process()
    text = notification_text([case] * 100, env.box.settings.groups[0], env.box.settings)
    assert len(text) < 3800 and len(re.findall(r"\d+月\d+日 \d{2}:\d{2}:\d{2}", text)) == 8


async def test_notification_scores_only_show_categories_that_met_their_own_gate(env):
    from qq_message_guard.config import Check

    env.box.settings = replace(
        env.box.settings,
        checks=(Check("敏感话题", notify_at=0.8), Check("商业推广", notify_at=0.9), Check("色情")),
    )
    await env.speak()
    case = await env.process()
    case["data"]["scores"] = {"sensitive_topics": 0.887177, "advertising": 0.85, "sexual": 0.91}
    text = notification_text([case], env.box.settings.groups[0], env.box.settings)
    assert "色情 · 91.0%、敏感话题 · 88.7% · 未撤回" in text
    assert "商业推广" not in text and "85.0%" not in text
