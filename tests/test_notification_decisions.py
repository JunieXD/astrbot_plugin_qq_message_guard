"""Saving doubtful evidence must not automatically message administrators."""

import json
from dataclasses import replace

import pytest

from qq_message_guard.client import Intent
from qq_message_guard.config import Check, GuardError, parse_settings


@pytest.mark.parametrize(
    "score,confidence", [(0.602390, 0.310700), (0.558802, 0.376666), (0.591914, 0.426455)]
)
async def test_ambiguous_scores_are_retained_without_qq_calls(env, score, confidence):
    env.configure(notify_group=True, admin_group="123456")
    env.model.score = score
    env.model.intent = Intent("participating", {}, confidence)
    await env.speak("正常话题的讨论")
    case = await env.process()
    await env.s.send_alerts()
    assert case["state"] == "review" and case["flagged"]
    assert not case["data"]["notification_ready"]
    assert await env.store.call("alerts", env.box.settings.revision) == []
    assert not env.bot.reads and not env.bot.writes
    env.clock.now += 15 * 86400
    await env.store.call("maintain", env.clock(), 14, 60)
    assert await env.store.call("case", case["id"])


@pytest.mark.parametrize(
    "score,choice,confidence",
    [
        (0.79, "participating", 0.95),  # Clear attribution, insufficient violation score.
        (0.99, "participating", 0.59),  # High score, uncertain attribution.
        (0.99, "normal", 0.95),
        (0.99, "report_or_stop", 0.95),
    ],
)
async def test_both_notification_score_and_attribution_are_required(env, score, choice, confidence):
    env.configure(notify_private=True, notify_group=True, admin_group="123456")
    env.box.settings = replace(env.box.settings, notify_users=("300001",))
    env.model.score = score
    env.model.intent = Intent(choice, {}, confidence)
    await env.speak()
    case = await env.process()
    await env.s.send_alerts()
    assert case["flagged"] and not case["data"]["notification_ready"]
    assert not env.bot.reads and not env.bot.writes


async def test_keyword_match_alone_never_notifies(env):
    env.configure(notify_group=True, admin_group="123456")
    env.model.score = 0.1
    await env.speak("法沦功")
    case = await env.process()
    await env.s.send_alerts()
    assert case["flagged"] and case["data"]["keyword_matches"]
    assert not case["data"]["notification_ready"] and not env.bot.writes


async def test_notification_threshold_independent_of_punishment(env):
    env.configure(auto_recall=True, warn_member=True, notify_group=True, admin_group="123456")
    env.model.score = 0.8
    env.model.intent = Intent("participating", {}, 0.6)
    await env.speak()
    case = await env.process()
    await env.s.send_alerts()
    assert case["data"]["notification_ready"]
    assert not case["data"]["auto_ready"]
    assert [a for a, _ in env.bot.writes] == ["send_group_msg"]
    assert (await env.store.call("operations", case["id"]))[0]["kind"] == "notice:group:123456"


async def test_multiple_checks_can_have_different_notification_thresholds(env):
    env.configure(notify_group=True, admin_group="123456")
    env.box.settings = replace(
        env.box.settings, checks=(Check(notify_at=0.99), Check("敏感话题", notify_at=0.7))
    )
    env.model.score = 0.75
    await env.speak()
    case = await env.process()
    await env.s.send_alerts()
    assert case["data"]["notification_ready"] and len(env.bot.writes) == 1


async def test_local_only_findings_cannot_hide_a_later_notification(env):
    env.configure(notify_group=True, admin_group="123456")
    env.model.score = 0.6
    await env.speak()
    ambiguous = await env.process()
    await env.store.db.executemany(
        "INSERT INTO cases(event_key,scope,created,updated,state,flagged,notify_enabled,settings_revision,data)"
        " VALUES(?,?,?,?,?,1,1,?,?)",
        [
            (
                f"ambiguous-{i}",
                ambiguous["scope"],
                ambiguous["created"],
                ambiguous["updated"],
                "review",
                ambiguous["settings_revision"],
                json.dumps(ambiguous["data"]),
            )
            for i in range(1001)
        ],
    )
    await env.store.db.commit()
    env.model.score = 0.98
    await env.speak()
    risk = await env.process()
    await env.s.send_alerts()
    assert len(env.bot.writes) == 1
    assert (await env.store.call("operations", risk["id"]))[0]["state"] == "confirmed"
    assert not (await env.store.call("case", ambiguous["id"]))["notified"]


async def test_legacy_frozen_batch_does_not_bypass_new_notification_gate(env):
    env.configure(notify_group=True, admin_group="123456")
    env.model.score = 0.6
    await env.speak()
    ambiguous = await env.process()
    env.model.score = 0.98
    await env.speak(user_id="200002")
    risk = await env.process()
    await env.store.call(
        "put",
        "alert-batch:" + ambiguous["scope"],
        {
            "ids": [ambiguous["id"], risk["id"]],
            "revision": env.box.settings.revision,
            "anchor": ambiguous["id"],
        },
    )
    await env.s.send_alerts()
    text = str(env.bot.writes[0])
    assert text.count("发言时间：") == 1 and "QQ 200002" in text
    assert "QQ 200001" not in text


def test_existing_configs_receive_separate_notification_default():
    assert parse_settings({"checks": [{"name": "群规"}]}).checks[0].notify_at == 0.8
    # Old valid settings with a higher review threshold remain valid.
    assert parse_settings({"checks": [{"name": "群规", "review_at": 0.85}]}).checks[0].notify_at == 0.85
    for value in (0.4, float("nan"), True, 1.1):
        with pytest.raises(GuardError):
            parse_settings({"checks": [{"name": "群规", "notify_at": value}]})
