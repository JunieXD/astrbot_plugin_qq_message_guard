"""Repeated live findings keep recalls independent of reminder cooldowns."""

import asyncio
from dataclasses import replace

import pytest
from conftest import ADMIN, A, G, U

from qq_message_guard.config import Deferred
from qq_message_guard.store import Store


async def wait_actions(env, cid):
    async def finished():
        while (await env.store.call("case", cid))["data"].get("actions_pending"):
            await asyncio.sleep(0)
        return await env.store.call("case", cid)

    return await asyncio.wait_for(finished(), 3)


async def test_repeated_offences_recall_each_but_remind_once_per_hour(env):
    env.configure(auto_recall=True, warn_member=True, notify_group=True, admin_group="123456")
    times = []

    async def timestamp(action):
        times.append((action, env.clock()))

    env.bot.after = timestamp
    env.s.tasks.add(asyncio.create_task(env.s.action_worker()))
    for index in range(3):
        if index:
            env.clock.now += 61
        await env.speak()
        case = await env.process()
        await wait_actions(env, case["id"])
        await env.s.send_alerts()
    assert [action for action, _ in env.bot.writes] == [
        "delete_msg",
        "send_group_msg",
        "send_group_msg",
        "delete_msg",
        "delete_msg",
    ]
    assert env.bot.writes[1][1]["group_id"] == G
    assert env.bot.writes[2][1]["group_id"] == "123456"
    assert 0.2 <= times[1][1] - times[0][1] <= 0.601
    # Repeated offences do not extend the original reminder's window.
    env.clock.now = times[2][1] + 3601
    await env.speak()
    case = await env.process()
    await wait_actions(env, case["id"])
    await env.s.send_alerts()
    assert [a for a, _ in env.bot.writes[-3:]] == ["delete_msg", "send_group_msg", "send_group_msg"]
    assert len(env.model.requests) == 4


async def test_pending_actions_wait_then_notify_final_recall_state(env):
    env.configure(auto_recall=True, warn_member=True, notify_group=True, admin_group="123456")
    await env.speak()
    case = await env.process()
    await env.s.send_alerts()
    assert not env.bot.writes
    assert not (await env.store.call("case", case["id"]))["notified"]
    env.s.tasks.add(asyncio.create_task(env.s.action_worker()))
    current = await wait_actions(env, case["id"])
    assert current["state"] == "recalled" and current["data"]["warned"]
    await env.s.send_alerts()
    assert "已撤回" in str(env.bot.writes[-1])


async def test_slow_recall_still_leaves_gap_before_warning_without_extra_action_delay(env):
    env.configure(auto_recall=True, warn_member=True)
    env.box.settings = replace(
        env.box.settings,
        advanced=replace(env.box.settings.advanced, action_min_seconds=1, action_max_seconds=1),
    )
    completed = []

    async def latency(action):
        if action == "delete_msg":
            env.clock.now += 2
        completed.append((action, env.clock()))

    env.bot.after = latency
    await env.speak()
    case = await env.process()
    await env.s.recall(case)
    await env.s.warn(await env.store.call("case", case["id"]))
    assert 0.2 <= completed[1][1] - completed[0][1] <= 0.601


async def test_reload_after_worker_claim_clears_notification_wait(env):
    env.configure(auto_recall=True, notify_group=True, admin_group="123456")
    await env.speak()
    case = await env.process()
    assert (await env.store.call("next_recall"))["id"] == case["id"]
    await env.store.call("recover", env.clock())
    assert await env.store.call("next_recall") is None
    await env.s.send_alerts()
    assert [a for a, _ in env.bot.writes] == ["send_group_msg"]


async def test_failed_warning_releases_member_timer_for_next_message(env):
    env.configure(warn_member=True)
    await env.speak()
    env.bot.fail = "business:send_group_msg"
    from qq_message_guard.platform import ApiFailure

    with pytest.raises(ApiFailure):
        await env.s.warn(await env.process())
    env.bot.fail = ""
    await env.speak()
    await env.s.warn(await env.process())
    assert len(env.bot.writes) == 2
    assert await env.store.call("get", f"warn:qq:{A}:{G}:{U}") > env.clock()


async def test_external_recall_cancels_queued_actions_without_stranding_notice(env):
    env.configure(auto_recall=True, warn_member=True, notify_group=True, admin_group="123456")
    raw, _ = await env.speak()
    case = await env.process()
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
    assert await env.store.call("next_recall") is None
    await env.s.send_alerts()
    assert len(env.bot.writes) == 1 and "已撤回" in str(env.bot.writes[0])
    assert (await env.store.call("case", case["id"]))["notified"]


async def test_member_cooldowns_survive_close_and_reopen(env):
    env.configure(warn_member=True, notify_group=True, admin_group="123456")
    await env.speak()
    case = await env.process()
    await env.s.warn(case)
    await env.store.call("patch", case["id"], {"actions_pending": False})
    await env.s.send_alerts()
    await env.store.close()
    await env.store.open()
    env.clock.now += 61
    await env.speak()
    case = await env.process()
    reads_before = len(env.bot.reads)
    with pytest.raises(Deferred, match="冷却"):
        await env.s.warn(case)
    await env.store.call("patch", case["id"], {"actions_pending": False})
    await env.s.send_alerts()
    assert len(env.bot.writes) == 2 and len(env.bot.reads) == reads_before


async def test_full_reminder_quota_does_not_prevent_recall(env):
    env.configure(auto_recall=True)
    for _ in range(env.box.settings.advanced.writes_per_hour):
        oid = await env.store.call("reserve", None, "notice", "qq:" + A, env.clock(), 30, 0)
        await env.store.call("operation", oid, "confirmed")
    await env.speak()
    await env.s.recall(await env.process())
    assert [a for a, _ in env.bot.writes] == ["delete_msg"]


async def test_full_recall_quota_does_not_prevent_admin_notice(env):
    env.configure(notify_group=True, admin_group="123456")
    limit = env.box.settings.advanced.recalls_per_hour
    for _ in range(limit):
        oid = await env.store.call("reserve", None, "recall", "qq:" + A, env.clock(), limit, 0)
        await env.store.call("operation", oid, "confirmed")
    await env.speak()
    await env.process()
    await env.s.send_alerts()
    assert [a for a, _ in env.bot.writes] == ["send_group_msg"]


async def test_batch_deduplicates_members_without_dropping_ninth_member(env):
    env.configure(notify_group=True, admin_group="123456")
    ids = []
    for user in [U, U, *[str(200002 + i) for i in range(8)]]:
        await env.speak(user_id=user)
        ids.append((await env.process())["id"])
    await env.s.send_alerts()
    text = env.bot.writes[0][1]["message"][0]["data"]["text"]
    assert text.count("（" + U + "）") == 1 and len(text.splitlines()) == 33
    assert not (await env.store.call("case", ids[-1]))["notified"]
    env.clock.now += 61
    await env.s.send_alerts()
    assert len(env.bot.writes) == 2 and "（200009）" in str(env.bot.writes[-1])
    for cid in ids:
        assert (await env.store.call("case", cid))["notified"]


async def test_channel_cooldown_does_not_suppress_other_recipient(env):
    env.configure(notify_private=True, notify_group=True, admin_group="123456")
    env.box.settings = replace(env.box.settings, notify_users=(ADMIN,))
    await env.store.call("put", f"notice-member:qq:{A}:{G}:private:{ADMIN}:{U}", env.clock() + 3600)
    await env.speak()
    await env.process()
    await env.s.send_alerts()
    assert [a for a, _ in env.bot.writes] == ["send_group_msg"]


async def test_disabled_notice_cooldown_allows_repeat_but_still_limits_member_warning(env):
    env.configure(warn_member=True, notify_group=True, admin_group="123456")
    env.box.settings = replace(
        env.box.settings, advanced=replace(env.box.settings.advanced, notify_cooldown_minutes=0)
    )
    # Existing timers must not suppress notices after the switch to zero.
    await env.store.call("put", f"notice-member:qq:{A}:{G}:group:123456:{U}", env.clock() + 3600)
    for i in range(3):
        if i:
            env.clock.now += 61
        await env.speak()
        case = await env.process()
        if not i:
            await env.s.warn(case)
        else:
            with pytest.raises(Deferred, match="冷却"):
                await env.s.warn(case)
        await env.store.call("patch", case["id"], {"actions_pending": False})
        await env.s.send_alerts()
    assert [p["group_id"] for action, p in env.bot.writes] == [G, "123456", "123456", "123456"]


async def test_disabled_notice_cooldown_reports_every_record_with_bounded_batches(env):
    env.configure(notify_group=True, admin_group="123456")
    env.box.settings = replace(
        env.box.settings, advanced=replace(env.box.settings.advanced, notify_cooldown_minutes=0)
    )
    ids = []
    for _ in range(10):
        await env.speak()
        ids.append((await env.process())["id"])
    await env.s.send_alerts()
    first = env.bot.writes[0][1]["message"][0]["data"]["text"]
    assert first.count("（" + U + "）") == 8
    assert not (await env.store.call("case", ids[-1]))["notified"]
    # The per-source interval still applies when per-member cooldown is off.
    await env.s.send_alerts()
    assert len(env.bot.writes) == 1
    env.clock.now += 61
    await env.s.send_alerts()
    assert len(env.bot.writes) == 2
    second = env.bot.writes[1][1]["message"][0]["data"]["text"]
    assert second.count("（" + U + "）") == 2
    for cid in ids:
        assert (await env.store.call("case", cid))["notified"]


@pytest.mark.parametrize(
    "outcome,kept", [("not_sent", False), ("failed", False), ("confirmed", True), ("unknown", True)]
)
async def test_cooldown_tracks_delivery_result(env, outcome, kept):
    await env.speak()
    case = await env.process()
    key = f"notice-member:qq:{A}:{G}:group:123456:{U}"
    oid = await env.store.call(
        "reserve",
        case["id"],
        "notice:group:123456",
        "qq:" + A,
        env.clock(),
        30,
        0,
        60,
        10,
        [(key, env.clock() + 3600)],
    )
    await env.store.call("operation", oid, outcome)
    assert bool(await env.store.call("get", key)) == kept


@pytest.mark.parametrize("submitted", [False, True])
async def test_recovery_preserves_only_potentially_sent_cooldown(tmp_path, submitted):
    store = Store(tmp_path / "state.sqlite3")
    await store.open()
    try:
        oid = await store.call(
            "reserve", None, "notice", "qq:" + A, 1000, 30, 0, 60, 10, [("notice-member:test", 4600)]
        )
        if submitted:
            await store.call("operation", oid, "submitted")
        await store.call("recover", 1001)
        assert bool(await store.call("get", "notice-member:test")) == submitted
        assert bool(await store.call("get", "pause:qq:" + A)) == submitted
    finally:
        await store.close()
