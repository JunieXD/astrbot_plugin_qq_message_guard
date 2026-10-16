import asyncio
from dataclasses import replace

import pytest
from conftest import A, G, U

from qq_message_guard.config import GuardError, MutePolicy, Policy, parse_settings
from qq_message_guard.muting import EscalatingMute, member_key
from qq_message_guard.notifications import notification_text


def enable(env, **changes):
    env.configure(auto_recall=True, mute=MutePolicy(enabled=True, **changes))


async def recalled(env, **fields):
    await env.speak(**fields)
    case = await env.process()
    await env.s.recall(case)
    return await env.store.call("case", case["id"])


def mute_writes(env):
    return [p for action, p in env.bot.writes if action == "set_group_ban"]


async def first_mute(env):
    for _ in range(4):
        case = await recalled(env)
    return case


async def unmute_naturally(env):
    state = await env.store.call("get", member_key(f"qq:{A}:{G}", U))
    env.clock.now = state["until"] + 1


async def test_fourth_confirmed_recall_triggers_one_hour_and_notification_reports_it(env):
    enable(env)
    for _ in range(3):
        await recalled(env)
    assert not mute_writes(env)
    case = await recalled(env)
    assert mute_writes(env) == [{"group_id": G, "user_id": U, "duration": 3600}]
    assert case["state"] == "recalled" and case["data"]["mute_status"] == "confirmed"
    text = notification_text([case], env.box.settings.groups[0], env.box.settings)
    assert "已撤回 · 禁言1小时" in text
    strikes = await env.store.call("mute_strikes", case["scope"], U, 0, env.clock())
    assert len(strikes) == 4
    await env.s.mutes.after_recall(case)
    assert len(mute_writes(env)) == 1


async def test_repeat_after_expiry_doubles_on_single_new_recall_and_survives_reload(env):
    enable(env)
    await first_mute(env)
    await env.store.close()
    await env.store.open()
    env.s.mutes = EscalatingMute(env.s)
    await unmute_naturally(env)
    await recalled(env)
    await unmute_naturally(env)
    await recalled(env)
    assert [p["duration"] for p in mute_writes(env)] == [3600, 7200, 14400]


async def test_restart_before_first_mute_keeps_confirmed_recall_count(env):
    enable(env)
    for _ in range(3):
        await recalled(env)
    await env.store.close()
    await env.store.open()
    env.s.mutes = EscalatingMute(env.s)
    assert await env.store.call("next_recall") is None
    assert not mute_writes(env)
    await recalled(env)
    assert [p["duration"] for p in mute_writes(env)] == [3600]


async def test_repeat_window_expired_requires_new_threshold_and_restarts_initial_duration(env):
    enable(env)
    await first_mute(env)
    state = await env.store.call("get", member_key(f"qq:{A}:{G}", U))
    env.clock.now = state["until"] + 48 * 3600 + 2
    for _ in range(3):
        await recalled(env)
    assert len(mute_writes(env)) == 1
    await recalled(env)
    assert [p["duration"] for p in mute_writes(env)] == [3600, 3600]


async def test_sliding_window_excludes_old_recalls(env):
    enable(env)
    for _ in range(3):
        await recalled(env)
    env.clock.now += 601
    case = await recalled(env)
    assert not mute_writes(env)
    recent = await env.store.call("mute_strikes", case["scope"], U, env.clock() - 600, env.clock())
    assert len(recent) == 1


@pytest.mark.parametrize("partition", ["user", "group"])
async def test_counters_are_isolated_between_members_and_groups(env, partition):
    enable(env)
    if partition == "group":
        env.box.settings = replace(
            env.box.settings,
            groups=(
                *env.box.settings.groups,
                Policy("100003", auto_recall=True, mute=MutePolicy(enabled=True)),
            ),
        )
    for i in range(4):
        fields = {"user_id": "200002"} if partition == "user" else {"group_id": "100003"}
        if partition == "user":
            fields["sender"] = {"user_id": "200002", "role": "member"}
        await recalled(env, **(fields if i % 2 else {}))
    assert not mute_writes(env)


async def test_external_recall_and_duplicate_confirmation_do_not_add_strikes(env):
    enable(env)
    raw, _ = await env.speak()
    await env.process()
    await env.s.observe(
        {
            "post_type": "notice",
            "notice_type": "group_recall",
            "self_id": A,
            "group_id": G,
            "message_id": raw["message_id"],
        },
        "qq",
    )
    for _ in range(3):
        case = await recalled(env)
    ops = await env.store.call("operations", case["id"])
    op = next(op for op in ops if op["kind"] == "recall")
    await env.store.call("operation", op["id"], "confirmed", "", env.clock())
    assert len(await env.store.call("mute_strikes", case["scope"], U, 0, env.clock())) == 3
    assert not mute_writes(env)


async def test_manual_bot_command_recall_does_not_count(env):
    enable(env)
    for _ in range(4):
        await env.speak()
        case = await env.process()
        await env.s.recall(case, manual=True)
    assert not await env.store.call("mute_strikes", case["scope"], U, 0, env.clock())
    assert not mute_writes(env)


async def test_failed_or_unknown_recall_does_not_count(env):
    enable(env)
    env.bot.fail = "delete_msg"
    await env.speak()
    case = await env.process()
    with pytest.raises(GuardError):
        await env.s.recall(case)
    assert not await env.store.call("mute_strikes", case["scope"], U, 0, env.clock())
    assert not mute_writes(env)


async def test_existing_mute_is_never_shortened_or_extended(env):
    enable(env)
    existing = env.clock() + 7200
    env.bot.mutes[(G, U)] = existing
    case = await first_mute(env)
    assert not mute_writes(env)
    assert env.bot.mutes[(G, U)] == existing
    assert "不覆盖" in case["data"]["mute_reason"]


@pytest.mark.parametrize("notice", [False, True])
async def test_early_manual_unmute_starts_repeat_window_without_automatic_unmute(env, notice):
    enable(env)
    await first_mute(env)
    env.clock.now += 60
    env.bot.mutes[(G, U)] = 0
    if notice:
        await env.s.observe(
            {
                "post_type": "notice",
                "notice_type": "group_ban",
                "self_id": A,
                "group_id": G,
                "user_id": U,
                "sub_type": "lift_ban",
                "duration": 0,
                "time": int(env.clock()),
            },
            "qq",
        )
    await recalled(env)
    assert [p["duration"] for p in mute_writes(env)] == [3600, 7200]


async def test_multiplier_is_capped_at_configured_maximum(env):
    enable(env, max_minutes=90)
    await first_mute(env)
    for _ in range(2):
        await unmute_naturally(env)
        await recalled(env)
    assert [p["duration"] for p in mute_writes(env)] == [3600, 5400, 5400]


async def test_old_queued_messages_do_not_count_towards_next_initial_mute(env):
    enable(env, initial_minutes=1, repeat_hours=0)
    await first_mute(env)
    history = await env.store.call("get", member_key(f"qq:{A}:{G}", U))
    # These messages were published before the ban took effect, but their
    # confirmed recalls finish later. They are not new post-mute offences.
    for _ in range(3):
        await recalled(env, time=int(history["muted_at"]))
    await unmute_naturally(env)
    await recalled(env)
    assert len(mute_writes(env)) == 1


async def test_late_admin_promotion_prevents_mute(env):
    enable(env)
    for _ in range(3):
        await recalled(env)

    async def promote(action):
        if action == "delete_msg":
            env.bot.target_role = "admin"

    env.bot.after = promote
    case = await recalled(env)
    assert not mute_writes(env)
    assert "群主或管理员" in case["data"]["mute_reason"]


async def test_group_ban_arriving_during_preparation_is_not_overwritten(env):
    enable(env)
    for _ in range(3):
        await recalled(env)
    reads = 0

    async def during_read(action):
        nonlocal reads
        if action == "get_group_member_info":
            reads += 1
            if reads == 4:  # recall's two member reads, then mute's two reads
                await env.s.observe(
                    {
                        "post_type": "notice",
                        "notice_type": "group_ban",
                        "self_id": A,
                        "group_id": G,
                        "user_id": U,
                        "sub_type": "ban",
                        "duration": 7200,
                        "time": int(env.clock()),
                    },
                    "qq",
                )

    env.bot.before = during_read
    await recalled(env)
    assert not mute_writes(env)


async def test_unknown_mute_is_not_repeated_and_pauses_account(env):
    enable(env)
    for _ in range(3):
        await recalled(env)
    env.bot.fail = "set_group_ban"
    case = await recalled(env)
    assert case["data"]["mute_status"] == "unknown"
    assert await env.store.call("get", "pause:qq:" + A)
    assert await env.store.call("get", member_key(case["scope"], U)) is None
    await env.s.mutes.after_recall(case)
    assert len(mute_writes(env)) == 1


async def test_misjudgment_removes_strikes_and_invalidates_future_escalation(env):
    enable(env)
    first = await recalled(env)
    for _ in range(2):
        await recalled(env)
    await env.s.mark(first["id"], "误判", "300001")
    await recalled(env)
    assert not mute_writes(env)
    case = await recalled(env)
    assert len(mute_writes(env)) == 1
    await env.s.mark(case["id"], "误判", "300001")
    await unmute_naturally(env)
    await recalled(env)
    assert len(mute_writes(env)) == 1
    history = await env.store.call("get", member_key(case["scope"], U))
    assert history["duration"] == 0


async def test_mute_quota_is_separate_from_recalls_and_reminder_quota(env):
    enable(env)
    env.box.settings = replace(
        env.box.settings, advanced=replace(env.box.settings.advanced, mutes_per_hour=1)
    )
    oid = await env.store.call("reserve", None, "mute", "qq:" + A, env.clock(), 1, 0)
    await env.store.call("operation", oid, "confirmed")
    await first_mute(env)
    assert not mute_writes(env)
    assert len([a for a, _ in env.bot.writes if a == "delete_msg"]) == 4
    env.clock.now += 1
    await env.store.call("can_write", None, "notice", "qq:" + A, env.clock(), 1)


@pytest.mark.parametrize(
    "patch",
    [
        {"trigger_recalls": 0},
        {"initial_minutes": 60, "max_minutes": 59},
        {"max_minutes": 43201},
        {"repeat_hours": -1},
        {"enabled": "yes"},
    ],
)
def test_mute_configuration_rejects_invalid_limits(patch):
    with pytest.raises(GuardError):
        parse_settings({"groups": [{"group_id": G, "mute": patch}]})


def test_mute_configuration_defaults_and_disabled_escalation():
    cfg = parse_settings({"groups": [{"group_id": G, "mute": {"enabled": True, "repeat_hours": 0}}]})
    assert cfg.groups[0].mute.trigger_recalls == 4
    assert cfg.groups[0].mute.repeat_hours == 0


async def test_action_worker_runs_recall_then_mute_then_warning(env):
    enable(env)
    env.configure(warn_member=True)
    for _ in range(3):
        await recalled(env)
    await env.speak()
    case = await env.process()
    task = asyncio.create_task(env.s.action_worker())
    env.s.tasks.add(task)
    for _ in range(3000):
        current = await env.store.call("case", case["id"])
        if not current["data"].get("actions_pending"):
            break
        await asyncio.sleep(0)
    assert not current["data"].get("actions_pending")
    assert [a for a, _ in env.bot.writes[-3:]] == ["delete_msg", "set_group_ban", "send_group_msg"]
