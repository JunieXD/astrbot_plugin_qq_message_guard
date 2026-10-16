import asyncio
import copy
from dataclasses import replace

import pytest
from conftest import ADMIN, A, G, U
from test_restart_boundaries import AccountGuard

from qq_message_guard.client import Result
from qq_message_guard.config import Controversy, Deferred, GuardError, MutePolicy, Policy, parse_settings
from qq_message_guard.notifications import notification_text


@pytest.fixture
def gentle(env):
    env.box.settings = replace(env.box.settings, backend="qwen3guard", images_enabled=False)
    env.configure(
        auto_recall=True,
        warn_member=True,
        mute=MutePolicy(enabled=True),
        controversy=Controversy(enabled=True),
    )
    env.model.categories = ("Politically Sensitive Topics",)
    env.model.safety = "Controversial"

    async def evaluate(state, settings, media=()):
        env.model.requests.append(copy.deepcopy(state))
        return Result(
            settings.model,
            {},
            20,
            10,
            1,
            {"content": "Safety: " + env.model.safety},
            safety=env.model.safety,
            categories=env.model.categories,
        )

    env.model.evaluate = evaluate
    original = env.bot.call_action

    async def record(action, self_id, **params):
        result = await original(action, self_id, **params)
        if action == "send_group_msg" and isinstance(result, dict) and "message_id" in result:
            env.bot.messages[str(result["message_id"])] = {
                "message_id": result["message_id"],
                "group_id": params["group_id"],
                "sender": {"user_id": A},
                "message": copy.deepcopy(params["message"]),
                "time": int(env.clock()),
            }
        return result

    env.bot.call_action = record
    return env


async def warned(env, **fields):
    await env.speak(**fields)
    case = await env.process()
    queued = await env.store.call("next_recall")
    assert queued["id"] == case["id"]
    try:
        await env.s.controversies.warn(queued)
    finally:
        await env.store.call("patch", case["id"], {"actions_pending": False})
    return await env.store.call("case", case["id"])


async def retract(env, case, *, operator=U, mid=None, stamp=None, user=U):
    await env.s.observe(
        {
            "post_type": "notice",
            "notice_type": "group_recall",
            "self_id": A,
            "group_id": case["data"]["message"]["group"],
            "user_id": user,
            "operator_id": operator,
            "message_id": mid or case["data"]["message"]["mid"],
            "time": int(env.clock()) if stamp is None else stamp,
        },
        "qq",
    )


async def clean(env):
    warning = await env.store.call("pending_controversy_cleanup", env.clock())
    assert warning is not None
    await env.s.controversies.cleanup(await env.store.call("case", warning["case_id"]), warning)
    return warning


def actions(env, action):
    return [p for a, p in env.bot.writes if a == action]


async def test_third_confirmed_reminder_mutes_ten_minutes_without_original_recall(gentle):
    env = gentle
    for _ in range(2):
        await warned(env)
    assert not actions(env, "set_group_ban")
    case = await warned(env)
    assert actions(env, "set_group_ban") == [{"group_id": G, "user_id": U, "duration": 600}]
    assert len(actions(env, "send_group_msg")) == 3
    assert not actions(env, "delete_msg")
    assert case["data"]["controversy_mute_status"] == "confirmed"
    assert await env.store.call("get", f"mute-member:{case['scope']}:{U}") is None
    assert not await env.store.call("mute_strikes", case["scope"], U, 0, env.clock())
    assert case["data"]["warned"] and not case["data"]["recall_ready"] and not case["data"]["warn_ready"]


@pytest.mark.parametrize(
    "category,text",
    [
        ("Politically Sensitive Topics", "这类话题容易引起争议"),
        ("Violent", "可能被理解为威胁"),
        ("Non-violent Illegal Acts", "可能涉及违规活动"),
        ("Sexual Content or Sexual Acts", "请注意聊天尺度"),
    ],
)
async def test_every_enabled_category_uses_its_own_short_message(gentle, category, text):
    env = gentle
    env.model.categories = (category,)
    await warned(env)
    assert text in actions(env, "send_group_msg")[0]["message"][-1]["data"]["text"]


async def test_reminder_replies_to_triggering_message_and_mentions_its_author(gentle):
    env = gentle
    env.model.safety = "Safe"
    original, _ = await env.speak(text="较早的另一条发言")
    await env.process()
    env.model.safety = "Controversial"
    case = await warned(
        env,
        segments=[
            {"type": "reply", "data": {"id": original["message_id"]}},
            {"type": "text", "data": {"text": "触发本次审核的发言"}},
        ],
    )
    message = case["data"]["message"]
    sent = actions(env, "send_group_msg")[0]
    assert sent["group_id"] == message["group"]
    assert sent["message"] == [
        {"type": "reply", "data": {"id": message["mid"]}},
        {"type": "at", "data": {"qq": message["user"]}},
        {
            "type": "text",
            "data": {"text": " " + dict(env.box.settings.groups[0].controversy.messages)["敏感话题"]},
        },
    ]
    assert message["mid"] != str(original["message_id"])


async def test_self_recall_after_mute_only_removes_reminder(gentle):
    env = gentle
    for _ in range(3):
        case = await warned(env)
    until = env.bot.mutes[(G, U)]
    await retract(env, case)
    await clean(env)
    assert env.bot.mutes[(G, U)] == until
    assert [p["duration"] for p in actions(env, "set_group_ban")] == [600]


async def test_pending_cleanup_can_accept_delayed_but_timely_recall_notice(gentle):
    env = gentle
    case = await warned(env)
    warning = await env.store.call("controversy_warning", case["id"])
    env.clock.now = warning["deadline"] + 2
    assert await env.store.call("pending_controversy_cleanup", env.clock()) is None
    await retract(env, case, stamp=int(warning["deadline"]) - 1)
    await clean(env)
    assert len(actions(env, "delete_msg")) == 1


async def test_mute_can_be_disabled_while_reminders_continue(gentle):
    env = gentle
    env.configure(controversy=replace(env.box.settings.groups[0].controversy, mute_enabled=False))
    for _ in range(3):
        await warned(env)
    assert len(actions(env, "send_group_msg")) == 3 and not actions(env, "set_group_ban")


async def test_short_mute_notices_do_not_extend_unsafe_escalation_window(gentle):
    env = gentle
    key = f"mute-member:qq:{A}:{G}:{U}"
    history = {
        "duration": 3600,
        "until": env.clock() - 60,
        "muted_at": env.clock() - 3660,
        "case_id": 0,
        "source_ids": [],
    }
    await env.store.call("put", key, history)

    async def ban_notice(action):
        if action == "set_group_ban":
            await env.s.observe(
                {
                    "post_type": "notice",
                    "notice_type": "group_ban",
                    "self_id": A,
                    "group_id": G,
                    "user_id": U,
                    "operator_id": A,
                    "sub_type": "ban",
                    "duration": 600,
                    "time": int(env.clock()),
                },
                "qq",
            )

    env.bot.after = ban_notice
    for _ in range(3):
        await warned(env)
    assert await env.store.call("get", key) == history
    await ban_notice("set_group_ban")  # The same notice can arrive after the response too.
    assert await env.store.call("get", key) == history


async def test_short_mute_does_not_reactivate_expired_unsafe_multiplier(gentle):
    env = gentle
    await env.store.call(
        "put",
        f"mute-member:qq:{A}:{G}:{U}",
        {
            "duration": 3600,
            "until": env.clock() - 49 * 3600,
            "muted_at": env.clock() - 50 * 3600,
            "case_id": 0,
            "source_ids": [],
        },
    )
    for _ in range(3):
        await warned(env)
    env.clock.now = env.bot.mutes[(G, U)] + 1
    env.model.safety = "Unsafe"
    for _ in range(4):
        await env.speak()
        case = await env.process()
        await env.s.recall(case)
    assert [p["duration"] for p in actions(env, "set_group_ban")] == [600, 3600]


async def test_custom_category_text_and_multicategory_one_reminder_one_count(gentle):
    env = gentle
    env.configure(
        controversy=replace(
            env.box.settings.groups[0].controversy, messages=(("敏感话题", "请留意话题尺度。"),)
        )
    )
    await warned(env)
    assert actions(env, "send_group_msg")[0]["message"][-1]["data"]["text"] == " 请留意话题尺度。"
    env.model.categories = ("Politically Sensitive Topics", "Violent")
    case = await warned(env)
    assert len(actions(env, "send_group_msg")) == 2
    assert actions(env, "send_group_msg")[-1]["message"][-1]["data"]["text"] == " 请留意话题尺度。"
    assert len(await env.store.call("controversy_strikes", case["scope"], U, 0, env.clock())) == 2


async def test_unethical_controversial_stays_local_and_never_counts(gentle):
    env = gentle
    env.model.categories = ("Unethical Acts",)
    await env.speak("普通抱怨")
    case = await env.process()
    assert not case["data"]["controversy_ready"] and not case["data"]["notification_ready"]
    assert await env.store.call("next_recall") is None
    assert not env.bot.writes


async def test_local_admin_rule_does_not_prevent_explicit_category_reminders(gentle):
    env = gentle
    env.model.categories = ("Unethical Acts",)
    env.configure(controversy=replace(env.box.settings.groups[0].controversy, categories=("不当行为",)))
    case = await warned(env)
    assert len(actions(env, "send_group_msg")) == 1 and not case["data"]["notification_ready"]


async def test_feature_independent_of_unsafe_warn_switch_and_ignores_legacy_cooldown(gentle):
    env = gentle
    env.configure(auto_recall=False, warn_member=False)
    await env.store.call("put", f"warn:qq:{A}:{G}:{U}", env.clock() + 3600)
    await warned(env)
    assert len(actions(env, "send_group_msg")) == 1


async def test_unsafe_still_uses_existing_recall_flow(gentle):
    env = gentle
    env.model.safety = "Unsafe"
    await env.speak()
    case = await env.process()
    assert not case["data"]["controversy_ready"] and case["data"]["recall_ready"]
    await env.s.recall(case)
    assert len(actions(env, "delete_msg")) == 1
    assert await env.store.call("controversy_warning", case["id"]) is None


async def test_controversial_never_recalls_even_if_old_automatic_switch_is_on(gentle):
    env = gentle
    env.box.settings = replace(
        env.box.settings, qwen=replace(env.box.settings.qwen, act_on_controversial=True)
    )
    case = await warned(env)
    assert not case["data"]["recall_ready"] and not actions(env, "delete_msg")


async def test_self_recall_cleans_only_correct_bot_reminder_and_keeps_count(gentle):
    env = gentle
    case = await warned(env)
    warning = await env.store.call("controversy_warning", case["id"])
    env.clock.now = warning["at"] + 119
    await retract(env, case)
    await clean(env)
    assert actions(env, "delete_msg") == [{"message_id": warning["warning_mid"]}]
    assert len(await env.store.call("controversy_strikes", case["scope"], U, 0, env.clock())) == 1
    assert (await env.store.call("controversy_warning", case["id"]))["cleanup_state"] == "confirmed"
    assert not await env.store.call("mute_strikes", case["scope"], U, 0, env.clock())


@pytest.mark.parametrize("reason", ["late", "admin", "wrong_sender", "missing_operator"])
async def test_only_timely_confirmed_self_recall_cleans(gentle, reason):
    env = gentle
    case = await warned(env)
    warning = await env.store.call("controversy_warning", case["id"])
    if reason == "late":
        env.clock.now = warning["deadline"] + 2
    await retract(
        env,
        case,
        operator=ADMIN if reason in ("admin", "wrong_sender") else "" if reason == "missing_operator" else U,
        user=ADMIN if reason == "wrong_sender" else U,
    )
    assert await env.store.call("pending_controversy_cleanup", env.clock()) is None
    assert not actions(env, "delete_msg")


async def test_third_source_recalled_during_warning_response_still_mutes_and_cleans(gentle):
    env = gentle
    await warned(env)
    await warned(env)
    await env.speak()
    case = await env.process()
    claimed = await env.store.call("next_recall")

    async def during_response(action):
        if action == "send_group_msg":
            await retract(env, case)

    env.bot.after = during_response
    await env.s.controversies.warn(claimed)
    await clean(env)
    assert [p["duration"] for p in actions(env, "set_group_ban")] == [600]
    assert env.bot.mutes[(G, U)] > env.clock()
    assert len(actions(env, "delete_msg")) == 1
    assert all(p["duration"] > 0 for p in actions(env, "set_group_ban"))


async def test_recall_before_reminder_does_not_send_or_count(gentle):
    env = gentle
    await env.speak()
    case = await env.process()
    await retract(env, case)
    with pytest.raises(Deferred):
        await env.s.controversies.warn(case)
    assert not env.bot.writes


async def test_counters_and_pending_own_message_cleanup_survive_restart(gentle):
    env = gentle
    first = await warned(env)
    await warned(env)
    await retract(env, first)
    await env.store.close()
    await env.store.open()
    await clean(env)
    await warned(env)
    assert [p["duration"] for p in actions(env, "set_group_ban")] == [600]


async def test_mute_starts_new_round_does_not_repeat_or_double(gentle):
    env = gentle
    for _ in range(3):
        case = await warned(env)
    await env.s.controversies.mute(case)
    assert len(actions(env, "set_group_ban")) == 1
    env.bot.mutes[(G, U)] = 0  # Administrator releases it early.
    env.clock.now += 2
    await warned(env)
    await warned(env)
    assert len(actions(env, "set_group_ban")) == 1
    await warned(env)
    assert [p["duration"] for p in actions(env, "set_group_ban")] == [600, 600]


async def test_expired_window_restarts_count_and_false_mark_removes_source(gentle):
    env = gentle
    first = await warned(env)
    await env.s.mark(first["id"], "误判", ADMIN)
    await warned(env)
    await warned(env)
    assert not actions(env, "set_group_ban")
    env.clock.now += 601
    await warned(env)
    assert not actions(env, "set_group_ban")


@pytest.mark.parametrize("failure", ["send_group_msg", "business:send_group_msg"])
async def test_unconfirmed_or_failed_warning_never_counts(gentle, failure):
    env = gentle
    env.bot.fail = failure
    with pytest.raises(GuardError):
        await warned(env)
    assert not await env.store.call("_rows", "SELECT * FROM controversy_warnings")
    assert not actions(env, "set_group_ban")


async def test_existing_mute_and_admin_are_protected(gentle):
    env = gentle
    env.bot.target_role = "admin"
    await env.speak()
    case = await env.process()
    with pytest.raises(Deferred):
        await env.s.controversies.warn(case)
    env.bot.target_role = "member"
    env.bot.mutes[(G, U)] = env.clock() + 7200
    with pytest.raises(Deferred):
        await env.s.controversies.warn(case)
    assert not env.bot.writes


async def test_cleanup_requires_bot_authorship_and_never_repeats_unknown_delete(gentle):
    env = gentle
    case = await warned(env)
    warning = await env.store.call("controversy_warning", case["id"])
    await retract(env, case)
    env.bot.messages[warning["warning_mid"]]["sender"]["user_id"] = U
    with pytest.raises(Deferred):
        await clean(env)
    assert not actions(env, "delete_msg")
    assert (await env.store.call("controversy_warning", case["id"]))["cleanup_state"] == "failed"


async def test_unknown_cleanup_pauses_without_repeat(gentle):
    env = gentle
    case = await warned(env)
    await retract(env, case)
    env.bot.fail = "delete_msg"
    with pytest.raises(GuardError):
        await clean(env)
    assert (await env.store.call("controversy_warning", case["id"]))["cleanup_state"] == "unknown"
    assert await env.store.call("get", "pause:qq:" + A)
    assert await env.store.call("pending_controversy_cleanup", env.clock()) is None
    assert len(actions(env, "delete_msg")) == 1


async def test_expected_failed_cleanup_does_not_pause_other_plugins(gentle):
    env = gentle
    guard = AccountGuard()
    env.context.platform_manager._qq_automation_guard_v1 = guard
    case = await warned(env)
    await retract(env, case)
    env.bot.fail = "business:delete_msg"
    with pytest.raises(GuardError):
        await clean(env)
    assert guard.failures == 0 and not await env.store.call("get", "pause:qq:" + A)
    assert await env.store.call("pending_controversy_cleanup", env.clock()) is None


async def test_quota_and_dedup_apply_but_no_hourly_member_cooldown(gentle):
    env = gentle
    case = await warned(env)
    with pytest.raises(Deferred):
        await env.s.controversies.warn(case)
    assert len(actions(env, "send_group_msg")) == 1
    env.box.settings = replace(
        env.box.settings, advanced=replace(env.box.settings.advanced, group_warnings_per_hour=1)
    )
    with pytest.raises(Deferred, match="上限"):
        await warned(env)
    assert len(actions(env, "send_group_msg")) == 1


async def test_action_worker_completes_before_admin_notification(gentle):
    env = gentle
    env.configure(controversy=replace(env.box.settings.groups[0].controversy, trigger_warnings=1))
    await env.speak()
    case = await env.process()
    task = asyncio.create_task(env.s.action_worker())

    async def completed():
        while (await env.store.call("case", case["id"]))["data"]["actions_pending"]:
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(completed(), 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    current = await env.store.call("case", case["id"])
    assert current["data"]["warned"] and current["data"]["controversy_mute_status"] == "confirmed"
    assert "禁言10分钟" in notification_text([current], env.box.settings.groups[0], env.box.settings)


@pytest.mark.parametrize("kind", ["controversy_warn", "controversy_mute", "controversy_cleanup"])
@pytest.mark.parametrize("boundary", ["before_commit", "after_commit"])
async def test_restart_during_new_action_ack_keeps_receipt_and_counter(gentle, kind, boundary):
    env = gentle
    env.configure(
        controversy=replace(
            env.box.settings.groups[0].controversy,
            trigger_warnings=1,
            mute_enabled=kind == "controversy_mute",
        )
    )
    if kind == "controversy_cleanup":
        case = await warned(env)
        await retract(env, case)
        pending = await env.store.call("pending_controversy_cleanup", env.clock())
        action = env.s.controversies.cleanup(case, pending)
    else:
        await env.speak()
        case = await env.process()
        claimed = await env.store.call("next_recall")
        action = env.s.controversies.warn(claimed)
    reached, release = asyncio.Event(), asyncio.Event()
    commit = env.store.db.commit

    async def interruptible_commit():
        row = await env.store._row(
            "SELECT state FROM operations WHERE case_id=? AND kind=?", (case["id"], kind)
        )
        confirming = row and row["state"] == "confirmed" and not reached.is_set()
        if confirming and boundary == "before_commit":
            reached.set()
            await release.wait()
        await commit()
        if confirming and boundary == "after_commit":
            reached.set()
            await release.wait()

    env.store.db.commit = interruptible_commit
    task = asyncio.create_task(action)
    try:
        await asyncio.wait_for(reached.wait(), 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        env.store.db.commit = commit
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    await env.store.close()
    await env.store.open()
    operations = await env.store.call("operations", case["id"])
    assert next(op for op in operations if op["kind"] == kind)["state"] == "confirmed"
    assert not await env.store.call("get", "pause:qq:" + A)
    assert len(await env.store.call("_rows", "SELECT * FROM controversy_warnings")) == 1
    assert await env.store.call("next_recall") is None
    if kind == "controversy_mute":
        assert (await env.store.call("case", case["id"]))["data"]["controversy_mute_seconds"] == 600
    elif kind == "controversy_cleanup":
        assert (await env.store.call("controversy_warning", case["id"]))["cleanup_state"] == "confirmed"


@pytest.mark.parametrize("partition", ["group", "member"])
async def test_warning_counters_do_not_mix_groups_or_members(gentle, partition):
    env = gentle
    other = replace(env.box.settings.groups[0], group_id="100003")
    env.box.settings = replace(env.box.settings, groups=(*env.box.settings.groups, other))
    for i in range(4):
        fields = (
            {"group_id": other.group_id}
            if partition == "group"
            else {"user_id": "200002", "sender": {"user_id": "200002", "role": "member"}}
        )
        await warned(env, **(fields if i % 2 else {}))
    assert not actions(env, "set_group_ban")


@pytest.mark.parametrize(
    "patch",
    [
        {"enabled": 1},
        {"trigger_warnings": 0},
        {"mute_minutes": 0},
        {"categories": ["不存在"]},
        {"messages": {"敏感话题": ""}},
        {"messages": {"敏感话题": "两行\n文案"}},
        {"retract_seconds": 0},
        {"window_minutes": 0},
    ],
)
def test_configuration_validation(patch):
    with pytest.raises(GuardError):
        parse_settings({"groups": [{"group_id": G, "controversy": patch}]})


def test_configuration_defaults_and_per_group_isolation():
    settings = parse_settings(
        {"groups": [{"group_id": G, "controversy": {"enabled": True}}, {"group_id": "100003"}]}
    )
    assert settings.groups[0].controversy == Controversy(enabled=True)
    assert settings.groups[1] == Policy("100003")
