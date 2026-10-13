import copy
from dataclasses import replace

import pytest
from conftest import ADMIN, A, G, U

from qq_message_guard.commands import Commands
from qq_message_guard.config import Deferred, GuardError, Uncertain


async def test_default_records_full_audit_without_any_qq_api(env):
    await env.speak("来咨询学校招生信息")
    case = await env.process()
    assert case["state"] == "review" and not env.bot.reads and not env.bot.writes
    data = case["data"]
    assert data["audit_request"]["state"]["当前待审核消息"]["内容"] == "来咨询学校招生信息"
    assert data["audit_response"]["qev"]["backend"] == "llama.cpp"
    assert data["usage"]["input_tokens"] == 123 and data["checks"][0]["review_at"] == 0.55
    assert data["actual_model"] == "fake-4b"


async def test_normal_messages_are_preserved_for_two_weeks(env):
    env.model.score = 0.1
    await env.speak("正常交流原文")
    case = await env.process()
    assert case["state"] == "allowed" and case["data"]["message"]["text"] == "正常交流原文"
    assert not env.bot.writes


async def test_duplicate_event_one_call(env):
    raw, _ = await env.speak()
    await env.s.observe(copy.deepcopy(raw), "qq")
    await env.process()
    assert len(env.model.requests) == 1 and await env.store.call("next_case", {}) is None


async def test_bounded_queue_failure_is_not_allow(env):
    env.box.settings = replace(env.box.settings, advanced=replace(env.box.settings.advanced, queue_size=1))
    await env.speak("first")
    _, case = await env.speak("second")
    assert case["state"] == "incomplete" and "满" in case["data"]["reason"]


@pytest.mark.parametrize(
    "segments",
    [
        [{"type": "text", "data": {"text": "x" * 4001}}],
        [{"type": "text", "data": {"text": "危险文本"}}, {"type": "record", "data": {"file": "a"}}],
        [{"type": "reply", "data": {"id": "999"}}, {"type": "text", "data": {"text": "他说得对"}}],
        [{"type": "text", "data": {"text": "文字"}}, {"type": "image", "data": {"file": "x.jpg"}}],
    ],
)
async def test_partial_coverage_never_auto_punishes(env, segments):
    env.configure(auto_recall=True, warn_member=True)
    await env.speak(segments=segments)
    case = await env.process()
    assert case["state"] == "incomplete" and not case["data"]["auto_ready"]
    assert not env.bot.writes


async def test_image_evidence_and_actual_payload(env):
    await env.speak(segments=[{"type": "image", "data": {"file": "a.jpg", "url": "https://gchat.qpic.cn/a"}}])
    case = await env.process()
    records = case["data"]["media_records"]
    assert len(records) == 1 and (env.path / records[0]["file"]).is_file()
    assert case["data"]["message"]["images"] == []
    assert env.model.requests[0][0]["当前待审核消息"]["图片"] == "<image:1>"
    assert len(env.model.requests[0][2]) == 1 and "base64" not in str(case["data"]["audit_request"])


async def test_image_failure_marks_partial_and_preserves_text(env):
    env.configure(auto_recall=True, warn_member=True)
    env.media.failure = True
    await env.speak(
        segments=[
            {"type": "text", "data": {"text": "文字仍审核"}},
            {"type": "image", "data": {"file": "a.jpg", "url": "https://gchat.qpic.cn/a"}},
        ]
    )
    case = await env.process()
    assert case["state"] == "incomplete" and len(env.model.requests) == 1 and not env.bot.writes


async def test_model_failure_circuit_and_stats(env):
    env.model.failure = True
    await env.speak()
    failed = await env.process()
    await env.speak()
    cooldown = await env.process()
    assert failed["state"] == cooldown["state"] == "incomplete" and len(env.model.requests) == 1
    stats = await env.store.call("stats", env.clock())
    assert stats["calls"]["n"] == 1 and stats["calls"]["ok"] == 0 and not env.bot.writes


async def test_context_is_background_and_quote_cache_not_network(env):
    env.model.score = 0.1
    first, _ = await env.speak("原发言")
    await env.process()
    await env.speak(
        segments=[
            {"type": "reply", "data": {"id": first["message_id"]}},
            {"type": "text", "data": {"text": "我是在反对这句话"}},
        ]
    )
    case = await env.process()
    state = env.model.requests[-1][0]
    assert state["背景"][0]["内容"] == "原发言"
    assert state["当前待审核消息"]["内容"] == "我是在反对这句话"
    assert case["state"] == "allowed" and not env.bot.reads


async def test_auto_recall_complete_chain(env):
    env.configure(auto_recall=True)
    await env.speak()
    case = await env.process()
    assert not env.bot.writes  # Audit worker does not wait on the action worker.
    ready = await env.store.call("next_recall")
    assert ready["id"] == case["id"]
    await env.s.recall(ready)
    assert [a for a, _ in env.bot.writes] == ["delete_msg"]
    assert (await env.store.call("case", case["id"]))["state"] == "recalled"
    assert [op["state"] for op in await env.store.call("operations", case["id"])] == ["confirmed"]


async def test_warning_independent_and_has_real_at_segment(env):
    env.configure(warn_member=True, warning_text="{QQ号} 请遵守 {审核项}")
    await env.speak()
    await env.process()
    case = await env.store.call("next_recall")
    await env.s.warn(case)
    assert [a for a, _ in env.bot.writes] == ["send_group_msg"]
    sent = env.bot.writes[0][1]
    assert sent["group_id"] == G and sent["message"][0] == {"type": "at", "data": {"qq": U}}
    assert U in sent["message"][1]["data"]["text"] and "群规" in sent["message"][1]["data"]["text"]


async def test_recall_then_warning_order(env):
    env.configure(auto_recall=True, warn_member=True)
    await env.speak()
    await env.process()
    ready = await env.store.call("next_recall")
    await env.s.recall(ready)
    current = await env.store.call("case", ready["id"])
    await env.s.warn(current)
    assert [a for a, _ in env.bot.writes] == ["delete_msg", "send_group_msg"]


async def test_warning_cooldown_and_duplicate(env):
    env.configure(warn_member=True)
    await env.speak()
    case = await env.process()
    await env.s.warn(case)
    with pytest.raises(Deferred):
        await env.s.warn(case)
    await env.speak()
    another = await env.process()
    with pytest.raises(Deferred, match="冷却"):
        await env.s.warn(another)
    assert len(env.bot.writes) == 1


async def test_group_warning_quota(env):
    env.configure(warn_member=True)
    env.box.settings = replace(
        env.box.settings, advanced=replace(env.box.settings.advanced, group_warnings_per_hour=1)
    )
    await env.speak()
    await env.s.warn(await env.process())
    await env.speak(user_id="200002", sender={"user_id": "200002", "role": "member"})
    with pytest.raises(Deferred, match="每小时警告"):
        await env.s.warn(await env.process())
    assert len(env.bot.writes) == 1


@pytest.mark.parametrize(
    "condition",
    [
        "owner",
        "admin",
        "no_permission",
        "offline",
        "wrong_login",
        "wrong_group",
        "wrong_sender",
        "wrong_content",
        "wrong_time",
        "missing",
    ],
)
async def test_no_unverified_recall(env, condition):
    env.configure(auto_recall=True)
    raw, _ = await env.speak()
    case = await env.process()
    if condition in ("owner", "admin"):
        env.bot.target_role = condition
    elif condition == "no_permission":
        env.bot.role = "member"
    elif condition == "offline":
        env.bot.online = False
    elif condition == "wrong_login":
        env.bot.login = "100099"
    elif condition == "missing":
        env.bot.messages.clear()
    else:
        found = env.bot.messages[str(raw["message_id"])]
        if condition == "wrong_group":
            found["group_id"] = "100099"
        elif condition == "wrong_sender":
            found["sender"]["user_id"] = "200099"
        elif condition == "wrong_content":
            found["message"][0]["data"]["text"] = "替换内容"
        else:
            found["time"] += 1
    with pytest.raises(GuardError):
        await env.s.recall(case)
    assert not env.bot.writes


async def test_recall_notice_during_inference(env):
    env.configure(auto_recall=True, warn_member=True)
    raw, _ = await env.speak()

    async def recalled():
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

    env.model.hook = recalled
    case = await env.process()
    assert case["state"] == "recalled" and not env.bot.writes
    assert case["data"]["actual_model"] == "fake-4b" and case["data"]["audit_response"]
    assert await env.store.call("next_recall") is None


async def test_role_notice_immediately_before_write(env):
    env.configure(auto_recall=True)
    await env.speak()
    case = await env.process()

    async def revoke(action):
        if action == "get_msg":
            await env.s.observe(
                {
                    "post_type": "notice",
                    "notice_type": "group_admin",
                    "sub_type": "unset",
                    "self_id": A,
                    "group_id": G,
                    "user_id": A,
                },
                "qq",
            )

    env.bot.before = revoke
    with pytest.raises(Deferred):
        await env.s.recall(case)
    assert not env.bot.writes


async def test_human_misjudgment_cancels_before_submit(env):
    env.configure(auto_recall=True)
    await env.speak()
    case = await env.process()

    async def mark(action):
        if action == "get_msg":
            await env.s.mark(case["id"], "误判", ADMIN)

    env.bot.before = mark
    with pytest.raises(Deferred):
        await env.s.recall(case)
    assert not env.bot.writes


async def test_config_change_during_model_call(env):
    env.configure(auto_recall=True)
    await env.speak()

    async def change():
        env.configure(auto_recall=False)

    env.model.hook = change
    case = await env.process()
    assert case["state"] == "incomplete" and not env.bot.writes


async def test_connection_replacement_old_message_not_punished(env):
    env.configure(auto_recall=True)
    await env.speak()
    case = await env.process()
    env.bot._wsr_api_clients[A] = object()
    with pytest.raises(Deferred, match="连接"):
        await env.s.recall(case)
    assert not env.bot.writes


async def test_unknown_write_no_blind_retry_and_account_pause(env):
    env.configure(auto_recall=True)
    await env.speak()
    case = await env.process()
    env.bot.fail = "delete_msg"
    with pytest.raises(Uncertain):
        await env.s.recall(case)
    case = await env.store.call("case", case["id"])
    assert case["state"] == "unknown" and await env.store.call("get", "pause:qq:" + A)
    with pytest.raises(GuardError):
        await env.s.recall(case)
    assert len(env.bot.writes) == 1


async def test_notifications_private_and_admin_group_independent(env):
    env.configure(notify_private=True, notify_group=True, admin_group="123456")
    env.box.settings = replace(env.box.settings, notify_users=(ADMIN,))
    await env.speak("NEVER_REPEAT_THIS_CONTENT")
    await env.process()
    await env.s.send_alerts()
    assert [action for action, _ in env.bot.writes] == ["send_private_msg", "send_group_msg"]
    assert env.bot.writes[1][1]["group_id"] == "123456"
    assert all("NEVER_REPEAT" not in str(params) for _, params in env.bot.writes)
    await env.s.send_alerts()
    assert len(env.bot.writes) == 2


async def test_notice_off_means_no_messages_even_with_recipients(env):
    env.box.settings = replace(env.box.settings, notify_users=(ADMIN,))
    await env.speak()
    await env.process()
    await env.s.send_alerts()
    assert not env.bot.writes


async def test_bot_generated_message_not_reaudited(env):
    raw, _ = await env.speak(user_id=A, sender={"user_id": A, "role": "admin"})
    assert not env.model.requests and await env.store.call("recent") == []


async def test_commands_show_usage_and_mark_without_writes(env):
    await env.speak()
    case = await env.process()
    c = Commands(env.s)
    status = await c.run("/消息审核 状态", ADMIN, "qq", A)
    assert "输入123 tokens" in status and "模型调用1次" in status
    result = await c.run(f"/消息审核 标记 {case['id']} 误判", ADMIN, "qq", A)
    assert "误判" in result and not env.bot.writes
    view = await c.run(f"/消息审核 查看 {case['id']}", ADMIN, "qq", A)
    assert "fake-4b" in view and "普通群聊消息" in view
    assert "时间：1月15日 16:00:00" in view


async def test_per_group_queue_fairness(env):
    policies = env.box.settings.groups + (replace(env.box.settings.groups[0], group_id="100003"),)
    env.box.settings = replace(env.box.settings, groups=policies)
    await env.speak()
    await env.speak()
    await env.speak(group_id="100003")
    first = await env.store.call("next_case", {})
    second = await env.store.call("next_case", {first["scope"]: env.clock()})
    assert first["data"]["message"]["group"] == G and second["data"]["message"]["group"] == "100003"


async def test_expired_queue_and_action(env):
    env.configure(auto_recall=True)
    await env.speak()
    env.clock.now += 91
    assert (await env.process())["state"] == "incomplete" and not env.model.requests
    await env.speak()
    case = await env.process()
    env.clock.now += 121
    with pytest.raises(Deferred, match="时间"):
        await env.s.recall(case)
    assert not env.bot.writes


async def test_read_budget_ignores_local_probes(env):
    for _ in range(5):
        assert await env.platform.online("qq", A)
    reads = await env.store.call("_row", "SELECT count(*) FROM reads")
    assert reads[0] == 0


async def test_admin_delivery_deferred_is_retried_without_resending_private(env):
    env.configure(notify_private=True, notify_group=True, admin_group="123456")
    env.box.settings = replace(env.box.settings, notify_users=(ADMIN,))
    await env.speak("first")
    first = await env.process()
    original = env.platform.member

    async def missing_group(pid, account, group, user):
        if group == "123456":
            raise Deferred("bot尚未加入管理员群")
        return await original(pid, account, group, user)

    env.platform.member = missing_group
    await env.s.send_alerts()
    assert [a for a, _ in env.bot.writes] == ["send_private_msg"]
    assert not (await env.store.call("case", first["id"]))["notified"]
    await env.speak("new message after frozen batch")
    second = await env.process()
    env.platform.member = original
    env.clock.now += 601
    await env.s.send_alerts()
    assert [a for a, _ in env.bot.writes] == ["send_private_msg", "send_group_msg"]
    assert (await env.store.call("case", first["id"]))["notified"]
    assert not (await env.store.call("case", second["id"]))["notified"]
    env.clock.now += 601
    await env.s.send_alerts()
    assert [a for a, _ in env.bot.writes] == [
        "send_private_msg",
        "send_group_msg",
        "send_private_msg",
        "send_group_msg",
    ]


async def test_human_confirm_does_not_trigger_or_continue_automatic_action(env):
    env.configure(auto_recall=True)
    await env.speak()
    case = await env.process()
    await env.s.mark(case["id"], "违规", ADMIN)
    assert await env.store.call("next_recall") is None
    with pytest.raises(Deferred):
        await env.s.recall(case)
    assert not env.bot.writes
    current = await env.store.call("case", case["id"])
    await env.s.recall(current, manual=True)
    assert [a for a, _ in env.bot.writes] == ["delete_msg"]
