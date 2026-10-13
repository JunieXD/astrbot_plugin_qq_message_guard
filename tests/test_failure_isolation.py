import json
from dataclasses import replace

import httpx
import pytest
from conftest import ADMIN, A

from qq_message_guard.client import OneJev
from qq_message_guard.config import Deferred


@pytest.mark.parametrize("status", [413, 422])
async def test_rejected_input_does_not_skip_other_groups(env, status):
    other = replace(env.box.settings.groups[0], group_id="100003")
    env.box.settings = replace(env.box.settings, groups=env.box.settings.groups + (other,))
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        if body["state"]["当前待审核消息"]["内容"].startswith("长篇招生通知"):
            return httpx.Response(status, json={"detail": "request exceeds the token limit"})
        return httpx.Response(
            200,
            json={
                "model": "OneJev-4B",
                "answers": {
                    "rules": {"type": "noul", "noul": 0.98},
                    "intent": {
                        "type": "choice",
                        "choice": "participating",
                        "probabilities": {"participating": 0.95, "report_or_stop": 0.03, "normal": 0.02},
                        "confidence": 0.925,
                    },
                },
                "usage": {"input_tokens": 500, "output_tokens": 1},
            },
        )

    client = OneJev(httpx.MockTransport(respond))
    env.s.model = client
    try:
        await env.speak("长篇招生通知" * 500)
        rejected = await env.process()
        assert rejected["state"] == "incomplete"
        await env.speak("新群内的可疑发言", group_id=other.group_id)
        following = await env.process()
        assert following["state"] == "review"
        assert len(requests) == 2
        assert env.s.model_cooldown == 0
        assert not await env.store.call("get", "model-cooldown", 0)
        calls = await env.store.call("_rows", "SELECT ok FROM calls ORDER BY id")
        assert [row["ok"] for row in calls] == [0, 1]
        assert not env.bot.reads and not env.bot.writes
    finally:
        await client.close()


async def test_notification_reconnect_before_transport_retries_only_unsent_target(env):
    env.configure(notify_private=True, notify_group=True, admin_group="123456")
    env.box.settings = replace(env.box.settings, notify_users=(ADMIN,))
    await env.speak()
    case = await env.process()
    original = env.store.operation
    submissions = 0

    async def reconnect(oid, state, detail=""):
        nonlocal submissions
        await original(oid, state, detail)
        if state == "submitted":
            submissions += 1
            if submissions == 2:
                # Real reconnect while the persisted intention is awaiting its commit.
                env.bot._wsr_api_clients[A] = object()

    env.store.operation = reconnect
    await env.s.send_alerts()
    env.store.operation = original
    assert [action for action, _ in env.bot.writes] == ["send_private_msg"]
    operations = await env.store.call("operations", case["id"])
    assert {op["kind"]: op["state"] for op in operations} == {
        "notice:private:" + ADMIN: "confirmed",
        "notice:group:123456": "not_sent",
    }
    assert not (await env.store.call("case", case["id"]))["notified"]

    # Establish the replacement connection and pass both pacing deadlines.
    with pytest.raises(Deferred, match="连接恢复"):
        await env.platform.binding("qq", A)
    env.clock.now += 601
    await env.s.send_alerts()
    assert [action for action, _ in env.bot.writes] == ["send_private_msg", "send_group_msg"]
    assert (await env.store.call("case", case["id"]))["notified"]
    assert all(op["state"] == "confirmed" for op in await env.store.call("operations", case["id"]))
    assert not await env.store.call("get", "pause:qq:" + A)
    env.clock.now += 601
    await env.s.send_alerts()
    assert len(env.bot.writes) == 2


@pytest.mark.parametrize("state", ["reserved", "submitted", "confirmed", "failed", "unknown"])
async def test_only_known_unsent_intentions_can_be_retried(env, state):
    await env.speak()
    case = await env.process()
    kind = "notice:private:" + ADMIN
    account = "qq:" + A
    oid = await env.store.call("reserve", case["id"], kind, account, env.clock(), 30, 0)
    await env.store.call("operation", oid, state)
    with pytest.raises(Deferred, match="不重复"):
        await env.store.call("reserve", case["id"], kind, account, env.clock(), 30, 0)
    assert len(await env.store.call("operations", case["id"])) == 1
    assert not env.bot.writes


async def test_undeliverable_busy_group_does_not_hide_other_groups_alerts(env):
    env.configure(notify_group=True, admin_group="123456")
    other = replace(env.box.settings.groups[0], group_id="100003", admin_group="123457")
    env.box.settings = replace(env.box.settings, groups=env.box.settings.groups + (other,))
    await env.speak("等待管理员群加入")
    blocked = await env.process()
    # A busy group can accumulate over 1000 undelivered records while its admin
    # group has not admitted the bot. Use actual persisted audit data for them.
    await env.store.db.executemany(
        "INSERT INTO cases(event_key,scope,created,updated,state,flagged,notify_enabled,settings_revision,data)"
        " VALUES(?,?,?,?,?,1,1,?,?)",
        [
            (
                f"pending-{i}",
                blocked["scope"],
                blocked["created"],
                blocked["updated"],
                "review",
                blocked["settings_revision"],
                json.dumps(blocked["data"]),
            )
            for i in range(1005)
        ],
    )
    await env.store.db.commit()
    await env.speak("另一个群的新违规消息", group_id=other.group_id)
    urgent = await env.process()
    original = env.platform.member

    async def membership(pid, account, group, user):
        if group == "123456":
            raise Deferred("尚未加入第一个管理员群")
        return await original(pid, account, group, user)

    env.platform.member = membership
    await env.s.send_alerts()
    assert [params["group_id"] for _, params in env.bot.writes] == ["123457"]
    assert (await env.store.call("case", urgent["id"]))["notified"]
    assert not (await env.store.call("case", blocked["id"]))["notified"]
