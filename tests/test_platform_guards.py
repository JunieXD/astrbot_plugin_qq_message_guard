import pytest
from conftest import A

from qq_message_guard.config import Deferred, Uncertain


@pytest.mark.parametrize("ack", [{"status": "async", "retcode": 1}, {"unexpected": "object"}, "ok"])
async def test_recall_requires_native_confirmed_ack(env, ack):
    env.configure(auto_recall=True)
    await env.speak()
    case = await env.process()
    original = env.bot.call_action

    async def unexpected(action, **params):
        if action == "delete_msg":
            env.bot.writes.append((action, params))
            return ack
        return await original(action, **params)

    env.bot.call_action = unexpected
    with pytest.raises(Uncertain):
        await env.s.recall(case)
    assert (await env.store.call("operations", case["id"]))[0]["state"] == "unknown"
    assert await env.store.call("get", "pause:qq:" + A)


async def test_exhausted_write_cap_does_not_query_members_or_messages(env):
    env.configure(auto_recall=True)
    await env.speak()
    case = await env.process()
    limit = env.box.settings.advanced.recalls_per_hour
    for _ in range(limit):
        oid = await env.store.call("reserve", None, "recall", "qq:" + A, env.clock(), limit, 0)
        await env.store.call("operation", oid, "confirmed")
    with pytest.raises(Deferred, match="额度"):
        await env.s.recall(case)
    assert all(action in ("get_login_info", "get_status") for action, _ in env.bot.reads)
    assert not env.bot.writes


class SharedDeferred(Exception):
    pass


class SharedGuard:
    deferred_error = SharedDeferred
    scheduling_version = 5

    def __init__(self, skip=False):
        self.skip, self.calls = skip, []

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        assert await kwargs["online"]()
        if not self.skip:
            return await kwargs["action"]()


async def test_existing_shared_guard_used_with_fresh_preparation(env):
    guard = SharedGuard()
    env.context.platform_manager._qq_automation_guard_v1 = guard
    env.configure(auto_recall=True)
    await env.speak()
    await env.s.recall(await env.process())
    assert len(guard.calls) == 1 and guard.calls[0]["account"] == "qq"
    assert guard.calls[0]["key"] is None and env.bot.writes[0][0] == "delete_msg"
    assert guard.calls[0]["priority"] == 0
    assert guard.calls[0]["gap"] == (0.2, 0.6)


async def test_shared_guard_skipping_action_is_not_recorded_as_success(env):
    env.context.platform_manager._qq_automation_guard_v1 = SharedGuard(skip=True)
    env.configure(auto_recall=True)
    await env.speak()
    case = await env.process()
    with pytest.raises(Deferred):
        await env.s.recall(case)
    assert not env.bot.writes and (await env.store.call("case", case["id"]))["state"] == "review"


async def test_read_business_error_does_not_trip_shared_write_failure(env):
    env.context.platform_manager._qq_automation_guard_v1 = SharedGuard()
    env.configure(auto_recall=True)
    await env.speak()
    case = await env.process()
    original = env.bot.call_action

    async def missing(action, **params):
        if action == "get_group_member_info":
            return {"status": "failed", "retcode": 100}
        return await original(action, **params)

    env.bot.call_action = missing
    with pytest.raises(Deferred):
        await env.s.recall(case)
    assert not env.bot.writes and not await env.store.call("get", "pause:qq:" + A)
