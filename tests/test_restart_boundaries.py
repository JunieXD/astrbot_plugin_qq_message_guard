"""Interrupt real persistence boundaries without issuing any QQ requests."""

import asyncio
import sys
from pathlib import Path

import pytest
from conftest import A, G, U

from qq_message_guard.config import MutePolicy
from qq_message_guard.muting import member_key


class SharedDeferred(Exception):
    pass


class AccountGuard:
    """The installed ActionGuard pauses on BaseException, except deferred_error."""

    deferred_error = SharedDeferred
    scheduling_version = 5

    def __init__(self):
        self.failures = 0

    async def run(self, **options):
        assert await options["online"]()
        try:
            return await options["action"]()
        except self.deferred_error:
            raise
        except BaseException:
            self.failures += 1
            raise


CRASH = """
import asyncio, os, sys
from pathlib import Path
from qq_message_guard.store import Store

async def crash():
    path, cid, kind, phase, now = sys.argv[1:]
    store = Store(Path(path))
    await store.open()
    oid = await store.call('reserve', int(cid), kind, 'qq:100001', float(now), 180, 0)
    if phase != 'reserved':
        await store.call('operation', oid, 'submitted')
    if phase == 'confirmed':
        await store.call('confirm', oid, float(now), 0.5)
    elif phase == 'unknown':
        await store.call('operation', oid, 'unknown', '模拟请求超时')
    elif phase == 'uncommitted':
        # SQLite's worker has executed the entire success transition, but the
        # process dies before COMMIT; WAL recovery must discard all of it.
        await store.operation(oid, 'confirmed', '', float(now))
    os._exit(23)  # No finally, rollback, close or orderly plugin shutdown.

asyncio.run(crash())
"""


@pytest.mark.parametrize("kind", ["recall", "mute", "warn"])
@pytest.mark.parametrize("boundary", ["before_commit", "after_commit"])
async def test_reload_during_ack_persistence_keeps_confirmed_result(env, kind, boundary):
    env.configure(auto_recall=True, warn_member=True, mute=MutePolicy(enabled=True, trigger_recalls=1))
    await env.speak()
    case = await env.process()
    reached, release = asyncio.Event(), asyncio.Event()
    original = env.store.db.commit

    async def commit():
        row = await env.store._row(
            "SELECT state FROM operations WHERE case_id=? AND kind=?", (case["id"], kind)
        )
        confirming = row is not None and row["state"] == "confirmed" and not reached.is_set()
        if confirming and boundary == "before_commit":
            reached.set()
            await release.wait()
        await original()
        if confirming and boundary == "after_commit":
            reached.set()
            await release.wait()

    env.store.db.commit = commit
    task = asyncio.create_task(env.s.warn(case) if kind == "warn" else env.s.recall(case))
    try:
        await asyncio.wait_for(reached.wait(), 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        env.store.db.commit = original
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    operations = await env.store.call("operations", case["id"])
    assert next(op for op in operations if op["kind"] == kind)["state"] == "confirmed"
    assert not await env.store.call("get", "pause:qq:" + A)
    current = await env.store.call("case", case["id"])
    assert current["state"] == ("review" if kind == "warn" else "recalled")
    if kind == "warn":
        assert current["data"]["warned"]
    if kind == "mute":
        history = await env.store.call("get", member_key(case["scope"], U))
        assert history["duration"] == 3600
    await env.store.close()
    await env.store.open()
    assert (
        next(op for op in await env.store.call("operations", case["id"]) if op["kind"] == kind)["state"]
        == "confirmed"
    )
    assert not await env.store.call("get", "pause:qq:" + A)
    assert len([a for a, _ in env.bot.writes if a == "delete_msg"]) == (kind != "warn")
    assert len([a for a, _ in env.bot.writes if a == "set_group_ban"]) == (kind == "mute")


async def test_notification_ack_during_reload_is_not_delivered_twice(env):
    env.configure(notify_group=True, admin_group="100003")
    await env.speak()
    case = await env.process()
    reached, release = asyncio.Event(), asyncio.Event()
    original = env.store.db.commit

    async def commit():
        await original()
        row = await env.store._row(
            "SELECT 1 FROM operations WHERE case_id=? AND kind LIKE 'notice:%' AND state='confirmed'",
            (case["id"],),
        )
        if row and not reached.is_set():
            reached.set()
            await release.wait()

    env.store.db.commit = commit
    task = asyncio.create_task(env.s.send_alerts())
    try:
        await asyncio.wait_for(reached.wait(), 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        env.store.db.commit = original
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    await env.store.close()
    await env.store.open()
    assert not await env.store.call("get", "pause:qq:" + A)
    env.clock.now += 61
    await env.s.send_alerts()
    assert (await env.store.call("case", case["id"]))["notified"]
    assert len(env.bot.writes) == 1


@pytest.mark.parametrize("boundary", ["preparing", "submitted", "confirmed"])
async def test_reload_only_trips_shared_account_guard_for_uncertain_write(env, boundary):
    guard = AccountGuard()
    env.context.platform_manager._qq_automation_guard_v1 = guard
    env.configure(auto_recall=True)
    await env.speak()
    case = await env.process()
    reached, release = asyncio.Event(), asyncio.Event()
    original_commit = env.store.db.commit

    async def before(action):
        if boundary == "preparing" and action == "get_group_member_info":
            reached.set()
            await release.wait()

    async def after(action):
        if boundary == "submitted" and action == "delete_msg":
            reached.set()
            await release.wait()

    async def commit():
        await original_commit()
        row = await env.store._row(
            "SELECT 1 FROM operations WHERE case_id=? AND state='confirmed'", (case["id"],)
        )
        if boundary == "confirmed" and row and not reached.is_set():
            reached.set()
            await release.wait()

    env.bot.before, env.bot.after = before, after
    env.store.db.commit = commit
    task = asyncio.create_task(env.s.recall(case))
    try:
        await asyncio.wait_for(reached.wait(), 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        env.store.db.commit = original_commit
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert guard.failures == (boundary == "submitted")
    assert bool(await env.store.call("get", "pause:qq:" + A)) == (boundary == "submitted")


async def test_confirmed_recall_survives_crash_before_service_bookkeeping(env):
    env.configure(auto_recall=True)
    await env.speak()
    case = await env.process()
    oid = await env.store.call("reserve", case["id"], "recall", "qq:" + A, env.clock(), 180, 0)
    await env.store.call("operation", oid, "submitted")
    await env.store.call("operation", oid, "confirmed", "", env.clock())
    # Stop before Service.recall's subsequent finish() can run.
    await env.store.close()
    await env.store.open()
    current = await env.store.call("case", case["id"])
    assert current["state"] == "recalled"
    assert not current["data"]["actions_pending"]
    assert await env.store.call("next_recall") is None


async def test_reload_during_submitted_mute_records_unknown_in_case(env):
    env.configure(auto_recall=True, mute=MutePolicy(enabled=True, trigger_recalls=1))
    await env.speak()
    case = await env.process()
    submitted = asyncio.Event()

    async def after(action):
        if action == "set_group_ban":
            submitted.set()
            await asyncio.Event().wait()

    env.bot.after = after
    task = asyncio.create_task(env.s.recall(case))
    await asyncio.wait_for(submitted.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await env.store.close()
    await env.store.open()
    current = await env.store.call("case", case["id"])
    assert current["state"] == "recalled"
    assert current["data"]["mute_status"] == "unknown"
    assert await env.store.call("get", "pause:qq:" + A)
    assert await env.store.call("get", member_key(f"qq:{A}:{G}", U)) is None
    assert len([a for a, _ in env.bot.writes if a == "set_group_ban"]) == 1


@pytest.mark.parametrize("kind", ["recall", "mute"])
@pytest.mark.parametrize("phase", ["reserved", "submitted", "uncommitted", "confirmed", "unknown"])
async def test_abrupt_process_death_recovers_wal_without_replaying(env, kind, phase):
    env.configure(auto_recall=True, mute=MutePolicy(enabled=True, trigger_recalls=1))
    await env.speak()
    case = await env.process()
    await env.store.call(
        "patch",
        case["id"],
        {
            "recall_automatic": True,
            "mute_plan": {"duration": 3600, "source_ids": [case["id"]], "reason": "测试已达撤回次数"},
            "mute_status": "planned",
        },
    )
    if kind == "mute":
        recall = await env.store.call("reserve", case["id"], "recall", "qq:" + A, env.clock(), 180, 0)
        await env.store.call("confirm", recall, env.clock(), 0)
    await env.store.close()
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        CRASH,
        str(env.store.path),
        str(case["id"]),
        kind,
        phase,
        str(env.clock()),
        cwd=Path(__file__).resolve().parents[1],
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(child.communicate(), 10)
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
        await env.store.open()
    assert child.returncode == 23, (stdout, stderr)
    assert tuple(await env.store.call("_row", "PRAGMA integrity_check")) == ("ok",)
    operations = await env.store.call("operations", case["id"])
    expected = "confirmed" if phase == "confirmed" else "not_sent" if phase == "reserved" else "unknown"
    assert next(op for op in operations if op["kind"] == kind)["state"] == expected
    current = await env.store.call("case", case["id"])
    assert current["state"] == (
        "recalled"
        if kind == "mute" or phase == "confirmed"
        else "unknown"
        if expected == "unknown"
        else "review"
    )
    assert not current["data"]["auto_ready"] and not current["data"]["actions_pending"]
    assert bool(await env.store.call("get", "pause:qq:" + A)) == (expected == "unknown")
    strikes = await env.store.call("mute_strikes", case["scope"], U, 0, env.clock())
    assert len(strikes) == (kind == "mute" or phase == "confirmed")
    history = await env.store.call("get", member_key(case["scope"], U))
    if kind == "mute":
        assert current["data"]["mute_status"] == expected
        assert bool(history) == (phase == "confirmed")
        if history:
            assert history["duration"] == 3600
    assert await env.store.call("next_recall") is None
    assert not env.bot.writes


async def test_startup_repairs_legacy_split_outcomes_without_backfilling_strikes(env):
    env.configure(auto_recall=True, mute=MutePolicy(enabled=True))
    await env.speak()
    case = await env.process()
    await env.store.call("patch", case["id"], {"recall_automatic": True, "mute_status": "planned"})
    # Match an older release's durable state after a crash or cancelled ban.
    await env.store.db.executemany(
        "INSERT INTO operations(case_id,kind,account,at,state) VALUES(?,?,?,?,?)",
        [
            (case["id"], kind, "qq:" + A, env.clock(), state)
            for kind, state in [("recall", "confirmed"), ("mute", "unknown")]
        ],
    )
    await env.store.db.commit()
    await env.store.close()
    await env.store.open()
    current = await env.store.call("case", case["id"])
    assert current["state"] == "recalled" and current["data"]["mute_status"] == "unknown"
    assert not await env.store.call("mute_strikes", case["scope"], U, 0, env.clock())
    assert await env.store.call("get", member_key(case["scope"], U)) is None
    assert not env.bot.writes
