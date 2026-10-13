import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from conftest import ADMIN, A, G

from qq_message_guard.config import Advanced, Deferred, GuardError
from qq_message_guard.resources import InstanceLock, Journal


async def test_normal_14_days_flagged_60_days_and_human_override(env):
    env.model.score = 0.1
    await env.speak("无害正文")
    normal = await env.process()
    env.model.score = 0.99
    await env.speak("模型认为可疑")
    harmful = await env.process()
    await env.speak("模型误判")
    misjudged = await env.process()
    await env.s.mark(misjudged["id"], "误判", ADMIN)
    env.model.score = 0.1
    await env.speak("模型漏报，人工确认")
    manually = await env.process()
    await env.s.mark(manually["id"], "违规", ADMIN)
    env.clock.now += 15 * 86400
    await env.store.call("maintain", env.clock(), 14, 60)
    assert await env.store.call("case", normal["id"]) is None
    assert await env.store.call("case", misjudged["id"]) is None
    assert await env.store.call("case", harmful["id"])
    assert await env.store.call("case", manually["id"])
    # Detailed call records follow the case's retention, not a blanket 14-day delete.
    calls = await env.store.call("_rows", "SELECT case_id FROM calls ORDER BY case_id")
    assert [row[0] for row in calls] == [harmful["id"], manually["id"]]
    env.clock.now += 46 * 86400
    await env.store.call("maintain", env.clock(), 14, 60)
    assert await env.store.call("case", harmful["id"]) is None
    assert await env.store.call("case", manually["id"]) is None


async def test_evidence_deleted_on_same_retention(env):
    env.model.score = 0.1
    await env.speak(segments=[{"type": "image", "data": {"file": "a.jpg", "url": "https://gchat.qpic.cn/a"}}])
    normal = await env.process()
    image = normal["data"]["media_records"][0]["file"]
    assert (env.path / image).exists()
    files = await env.store.call("maintain", env.clock() + 15 * 86400, 14, 60)
    assert files == [image]


async def test_restart_cancels_queued_audits_and_auto_actions(env):
    env.configure(auto_recall=True, warn_member=True)
    await env.speak()
    ready = await env.process()
    _, queued = await env.speak()
    await env.store.call("recover", env.clock())
    assert (await env.store.call("case", queued["id"]))["state"] == "incomplete"
    assert not (await env.store.call("case", ready["id"]))["data"]["auto_ready"]
    assert await env.store.call("next_recall") is None and not env.bot.writes


async def test_crash_submitted_intent_becomes_unknown_and_blocks_account(env):
    await env.speak()
    case = await env.process()
    oid = await env.store.call("reserve", case["id"], "recall", "qq:" + A, env.clock(), 30, 0)
    await env.store.call("operation", oid, "submitted")
    await env.store.call("recover", env.clock())
    assert (await env.store.call("operations", case["id"]))[0]["state"] == "unknown"
    assert await env.store.call("get", "pause:qq:" + A)
    with pytest.raises(Deferred):
        await env.store.call("reserve", case["id"], "warn", "qq:" + A, env.clock(), 30, 0)


async def test_cancel_during_real_write_preserves_unknown_and_no_retry(env):
    env.configure(auto_recall=True)
    await env.speak()
    case = await env.process()
    submitted = asyncio.Event()

    async def after(action):
        submitted.set()
        await asyncio.Event().wait()

    env.bot.after = after
    task = asyncio.create_task(env.s.recall(case))
    await asyncio.wait_for(submitted.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    ops = await env.store.call("operations", case["id"])
    assert ops[0]["state"] == "unknown" and await env.store.call("get", "pause:qq:" + A)
    assert (await env.store.call("case", case["id"]))["state"] == "unknown"


async def test_read_and_write_caps_count_failed_attempts(env):
    await env.store.call("read", "qq:" + A, env.clock(), 1)
    with pytest.raises(Deferred):
        await env.store.call("read", "qq:" + A, env.clock(), 1)
    oid = await env.store.call("reserve", None, "notice:x", "qq:" + A, env.clock(), 1, 0)
    await env.store.call("operation", oid, "failed")
    with pytest.raises(Deferred, match="额度"):
        await env.store.call("reserve", None, "notice:y", "qq:" + A, env.clock(), 1, 0)


async def test_fresh_role_checked_when_resuming(env):
    await env.store.call("put", f"group-pause:qq:{A}:{G}", "权限丢失")
    env.bot.role = "member"
    with pytest.raises(GuardError):
        await env.s.resume(G, "qq", A)
    assert await env.store.call("get", f"group-pause:qq:{A}:{G}")
    env.bot.role = "admin"
    await env.s.resume(G, "qq", A)
    assert not await env.store.call("get", f"group-pause:qq:{A}:{G}")


def test_instance_lock_excludes_second_copy(tmp_path):
    first = InstanceLock(tmp_path / "instance.lock")
    try:
        with pytest.raises(GuardError):
            InstanceLock(tmp_path / "instance.lock")
    finally:
        first.close()
    another = InstanceLock(tmp_path / "instance.lock")
    another.close()


def test_logs_json_redaction_separate_retention_and_segmentation(tmp_path):
    journal = Journal(tmp_path, Advanced(log_mb=1))
    try:
        journal.record("normal", detail="https://example.com?key=SECRET\nBearer SECRET abk_PRIVATE")
        journal.record("flagged", harmful=True, scores={"rules": 0.95})
        files = sorted((tmp_path / "logs").glob("*.jsonl"))
        assert len(files) == 2
        event = next(p for p in files if p.name.startswith("events"))
        payload = json.loads(event.read_text(encoding="utf-8"))
        assert "SECRET" not in payload["detail"] and "abk_PRIVATE" not in payload["detail"]
        journal.handler.max_bytes = 10
        journal.record("second")
        assert len(list((tmp_path / "logs").glob("events-*.jsonl"))) == 2
        today = datetime.now(timezone(timedelta(hours=8))).date()
        old = today - timedelta(days=30)
        very_old = today - timedelta(days=61)
        normal = tmp_path / "logs" / f"events-{old}-00001.jsonl"
        harmful = tmp_path / "logs" / f"risk-{old}-00001.jsonl"
        expired = tmp_path / "logs" / f"risk-{very_old}-00001.jsonl"
        for p in (normal, harmful, expired):
            p.write_text("{}\n", encoding="utf-8")
        journal.maintain(14, 60)
        assert not normal.exists() and harmful.exists() and not expired.exists()
    finally:
        journal.close()


def test_broken_log_blocks_operations(tmp_path):
    journal = Journal(tmp_path, Advanced())
    try:
        journal.handler.healthy = False
        with pytest.raises(GuardError):
            journal.check()
    finally:
        journal.close()


async def test_expired_record_ids_are_never_reused(env):
    env.model.score = 0.1
    await env.speak()
    first = await env.process()
    env.clock.now += 15 * 86400
    await env.store.call("maintain", env.clock(), 14, 60)
    await env.speak()
    second = await env.process()
    assert second["id"] > first["id"]


async def test_expired_image_garbage_survives_until_successful_cleanup(env):
    env.model.score = 0.1
    await env.speak(segments=[{"type": "image", "data": {"file": "a.jpg", "url": "https://gchat.qpic.cn/a"}}])
    await env.process()
    first = await env.store.call("maintain", env.clock() + 15 * 86400, 14, 60)
    second = await env.store.call("maintain", env.clock() + 16 * 86400, 14, 60)
    assert first == second and first
    await env.store.call("purge_files", first)
    assert await env.store.call("maintain", env.clock() + 17 * 86400, 14, 60) == []
