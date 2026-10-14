import asyncio
from dataclasses import replace

import httpx
import pytest

from qq_message_guard.batching import build_state, eligible, screen_request
from qq_message_guard.client import OneJev, Result
from qq_message_guard.commands import Commands
from qq_message_guard.config import Batch, GuardError, InputRejected, Settings, parse_settings
from qq_message_guard.store import Store


@pytest.fixture
async def batch_env(env):
    env.box.settings = replace(env.box.settings, checks=Settings().checks, batch=Batch(spot_check_percent=0))
    env.model.batch_calls = []
    env.model.screen_score = 0.1
    env.model.locate_score = 0.1
    env.model.batch_hook = None
    env.model.batch_error = None

    async def evaluate_request(body, settings):
        env.model.batch_calls.append(body)
        if env.model.batch_hook:
            await env.model.batch_hook()
        if env.model.batch_error:
            raise env.model.batch_error
        locate = any("__" in key for key in body["questions"])
        scores = {
            key: (env.model.locate_score if locate else env.model.screen_score) for key in body["questions"]
        }
        raw = {
            "model": "fake-batch",
            "answers": {k: {"type": "noul", "noul": v} for k, v in scores.items()},
            "usage": {"input_tokens": 200, "output_tokens": len(scores)},
        }
        return Result("fake-batch", scores, 200, len(scores), 4, raw)

    env.model.evaluate_request = evaluate_request
    return env


async def claim(env):
    cases, wait = await env.store.call("next_work", env.s.served, env.box.settings, env.clock())
    if cases:
        await env.s.dispatch(cases)
        if len(cases) > 1:
            for _ in range(50):
                pending = [await env.store.call("case", c["id"]) for c in cases]
                if all(c["state"] not in ("queued", "auditing") for c in pending):
                    break
                next_cases, _ = await env.store.call("next_work", {}, env.box.settings, env.clock())
                assert next_cases
                await env.s.dispatch(next_cases)
    return cases, wait


async def fill(env, count=8):
    for i in range(count):
        await env.speak(f"正常的升学交流{i}", user_id=str(200010 + i))


async def test_batch_default_validation():
    assert parse_settings({}).batch == Batch()
    for key, value in [
        ("max_messages", 21),
        ("wait_seconds", 0),
        ("threshold", 0.9),
        ("threshold", float("nan")),
        ("spot_check_percent", 101),
        ("enabled", 1),
    ]:
        with pytest.raises(GuardError):
            parse_settings({"batch": {key: value}})


async def test_count_or_timer_and_singleton_detail(batch_env):
    env = batch_env
    await fill(env, 3)
    assert await claim(env) == ([], 20)
    env.clock.now += 20
    cases, _ = await claim(env)
    assert len(cases) == 3 and len(env.model.batch_calls) == 1 and not env.model.requests
    for c in cases:
        done = await env.store.call("case", c["id"])
        assert done["state"] == "allowed" and done["data"]["audit_path"] == "batch_clear"
        assert "scores" not in done["data"] and not done["data"]["auto_ready"]
    await fill(env, 1)
    env.clock.now += 20
    cases, _ = await claim(env)
    assert len(cases) == 1 and len(env.model.requests) == 1


async def test_full_batch_immediate_one_physical_call_and_exportable_record(batch_env):
    env = batch_env
    env.configure(auto_recall=True, warn_member=True, notify_group=True, admin_group="123456")
    await fill(env)
    cases, _ = await claim(env)
    assert len(cases) == 8 and len(env.model.batch_calls) == 1
    stats = await env.store.call("stats", env.clock())
    assert stats["calls"]["n"] == 1 and stats["calls"]["input"] == 200 and stats["screened_clear"] == 8
    assert stats["stages"] == {"screen": 1}
    await env.s.send_alerts()
    assert not env.bot.writes and await env.store.call("next_recall") is None
    first = await env.store.call("case", cases[0]["id"])
    sid = first["data"]["batch"]["screening_ids"][0]
    shared = await env.store.call("screening", sid)
    assert shared["case_ids"] == [c["id"] for c in cases]
    assert shared["data"]["request"] and shared["data"]["response"]
    assert "intent" not in shared["data"]["request"]["questions"]
    text = await Commands(env.s).run("/消息审核 状态", "200003", "qq", "100001")
    assert "初筛1次" in text and "初筛结束8条" in text


async def test_risk_word_image_missing_and_own_continuation_bypass(batch_env):
    env = batch_env
    await fill(env, 1)
    raw, risk = await env.speak("beijing baoluan")
    cases, _ = await claim(env)
    assert [c["id"] for c in cases] == [risk["id"]]
    assert len(env.model.requests) == 1 and not env.model.batch_calls
    _, continuation = await env.speak("我也是这个意思")
    assert not eligible(continuation, env.box.settings)
    _, image = await env.speak(
        segments=[{"type": "image", "data": {"file": "a", "url": "https://gchat.qpic.cn/a"}}]
    )
    assert not eligible(image, env.box.settings)
    _, opaque = await env.speak(
        segments=[{"type": "text", "data": {"text": "文字"}}, {"type": "record", "data": {"file": "a"}}]
    )
    assert not eligible(opaque, env.box.settings)


async def test_overlap_is_context_only_and_group_isolation(batch_env):
    env = batch_env
    for i in range(12):
        await env.speak("早先背景" + str(i), user_id="200099")
        await env.process()
    await fill(env)
    cases, _ = await env.store.call("next_work", {}, env.box.settings, env.clock())
    state = build_state(cases, env.box.settings, {})
    assert len(state["参考前文"]) == 10 and len(state["待初筛消息"]) == 8
    assert "背景0" not in str(state["参考前文"]) and "背景11" in str(state["参考前文"])
    assert all(row[0].startswith("C") for row in state["参考前文"])
    body = screen_request(state, env.box.settings)
    assert all("背景只消歧，不计入当前违规" in q["instructions"] for q in body["questions"].values())
    assert "群规" not in state and "200099" not in str(state)


async def test_ready_other_group_not_blocked_by_young_group(batch_env):
    env = batch_env
    env.box.settings = replace(
        env.box.settings,
        groups=env.box.settings.groups + (replace(env.box.settings.groups[0], group_id="100003"),),
    )
    await fill(env, 1)
    for i in range(8):
        await env.speak("另一群" + str(i), group_id="100003")
    cases, _ = await claim(env)
    assert len(cases) == 8 and {c["data"]["message"]["group"] for c in cases} == {"100003"}


async def test_locate_only_hit_categories_then_original_detail(batch_env):
    env = batch_env
    await fill(env)
    original = env.model.evaluate_request

    async def scoring(body, settings):
        result = await original(body, settings)
        scores = dict(result.scores)
        if "sensitive_topics" in scores:
            scores["sensitive_topics"] = 0.8
        else:
            scores["M03__sensitive_topics"] = 0.8
        return replace(result, scores=scores)

    env.model.evaluate_request = scoring
    cases, _ = await claim(env)
    assert len(env.model.batch_calls) == 2 and len(env.model.requests) == 1
    location = env.model.batch_calls[1]
    assert len(location["questions"]) == 8
    assert all(key.endswith("__sensitive_topics") for key in location["questions"])
    assert location["questions"]["M03__sensitive_topics"]["instructions"]["当前目标"][0] == "M03"
    target = await env.store.call("case", cases[2]["id"])
    assert target["state"] == "review" and len(target["data"]["scores"]) == 6
    stats = await env.store.call("stats", env.clock())
    assert stats["calls"]["n"] == 3 and stats["stages"] == {"screen": 1, "locate": 1, "detail": 1}
    assert stats["screened_clear"] == 7


async def test_spot_checks_detect_risk_and_extend_shared_record_retention(batch_env):
    env = batch_env
    env.box.settings = replace(
        env.box.settings, batch=replace(env.box.settings.batch, spot_check_percent=100)
    )
    await fill(env)
    cases, _ = await claim(env)
    assert len(env.model.requests) == 8
    for c in cases:
        case = await env.store.call("case", c["id"])
        assert case["data"]["batch"]["spot_check"] and case["data"]["audit_path"] == "detail"
    sid = case["data"]["batch"]["screening_ids"][0]
    assert (await env.store.call("screening", sid))["flagged"]
    stats = await env.store.call("stats", env.clock())
    assert stats["calls"]["n"] == 9 and stats["spot_checks"] == 8


async def test_timeout_is_incomplete_no_immediate_retry(batch_env):
    env = batch_env
    env.model.batch_error = GuardError("模拟超时")
    await fill(env)
    cases, _ = await claim(env)
    assert len(env.model.batch_calls) == 1 and not env.model.requests
    assert all([(await env.store.call("case", c["id"]))["state"] == "incomplete" for c in cases])
    assert (await env.store.call("stats", env.clock()))["calls"]["ok"] == 0
    assert env.s.model_cooldown > env.clock()


async def test_rejected_batch_falls_back_without_global_cooldown(batch_env):
    env = batch_env
    env.model.batch_error = InputRejected("输入过大")
    await fill(env)
    cases, _ = await claim(env)
    assert len(env.model.requests) == 8 and not env.s.model_cooldown
    assert all([(await env.store.call("case", c["id"]))["state"] == "review" for c in cases])


@pytest.mark.parametrize("change", ["recall", "config", "cancel"])
async def test_inflight_changes_do_not_allow_or_punish_old_targets(batch_env, change):
    env = batch_env
    await fill(env)

    async def hook():
        if change == "cancel":
            raise asyncio.CancelledError()
        if change == "config":
            env.configure(enabled=False)
        else:
            await env.s.observe(
                {
                    "post_type": "notice",
                    "notice_type": "group_recall",
                    "self_id": "100001",
                    "group_id": "100002",
                    "message_id": 1,
                },
                "qq",
            )

    env.model.batch_hook = hook
    if change == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await claim(env)
    else:
        await claim(env)
    cases = await env.store.call("recent", "", False, 20)
    assert not env.model.requests and not env.bot.writes
    assert not any(c["state"] == "auditing" for c in cases)
    if change == "recall":
        assert next(c for c in cases if c["data"]["message"]["mid"] == "1")["state"] == "recalled"
    else:
        assert all(c["state"] == "incomplete" for c in cases)


async def test_large_category_hit_set_skips_location_instead_of_truncating(batch_env):
    env = batch_env
    env.model.screen_score = 0.8
    await fill(env)
    await claim(env)
    assert len(env.model.batch_calls) == 1 and len(env.model.requests) == 8


async def test_generic_client_validates_all_location_answers():
    body = {"model": "jev-latest", "state": {}, "questions": {"M01__sensitive_topics": {"type": "noul"}}}
    value = {"model": "test", "answers": {}, "usage": {"input_tokens": 10, "output_tokens": 1}}
    client = OneJev(httpx.MockTransport(lambda _: httpx.Response(200, json=value)))
    try:
        with pytest.raises(GuardError):
            await client.evaluate_request(body, Settings())
        value["answers"]["M01__sensitive_topics"] = {"type": "noul", "noul": 0.2}
        result = await client.evaluate_request(body, Settings())
        assert result.scores == {"M01__sensitive_topics": 0.2}
    finally:
        await client.close()


async def test_shared_call_retention_survives_normal_anchor_removal(batch_env):
    env = batch_env
    await fill(env)
    cases, _ = await claim(env)
    first = await env.store.call("case", cases[0]["id"])
    sid = first["data"]["batch"]["screening_ids"][0]
    await env.store.call("flag_screenings", [sid])
    env.clock.now += 15 * 86400
    await env.store.call("maintain", env.clock(), 14, 60)
    assert not await env.store.call("case", first["id"])
    assert await env.store.call("screening", sid)
    assert (await env.store.call("_row", "SELECT count(*) n FROM calls"))["n"] == 1
    env.clock.now += 46 * 86400
    await env.store.call("maintain", env.clock(), 14, 60)
    assert not await env.store.call("screening", sid)
    assert (await env.store.call("_row", "SELECT count(*) n FROM calls"))["n"] == 0


async def test_database_migration_is_idempotent_and_reload_cancels_claimed_batch(batch_env):
    env = batch_env
    await fill(env)
    cases, _ = await env.store.call("next_work", {}, env.box.settings, env.clock())
    request = screen_request(build_state(cases, env.box.settings, {}), env.box.settings)
    sid = await env.store.call("begin_screening", cases, "screen", request, env.clock())
    await env.store.close()
    store = Store(env.path / "state.sqlite3")
    try:
        await store.open()
        assert (await store.call("screening", sid))["state"] == "incomplete"
        assert all([(await store.call("case", c["id"]))["state"] == "incomplete" for c in cases])
        cols = await store.call("_rows", "PRAGMA table_info(calls)")
        assert {"stage", "screening_id"} <= {c["name"] for c in cols}
    finally:
        await store.close()


async def test_interleaved_direct_message_remains_context_for_following_targets(batch_env):
    env = batch_env
    await fill(env, 2)
    _, risk = await env.speak("法沦功", user_id="200099")
    for i in range(6):
        await env.speak("接着说" + str(i), user_id=str(200020 + i))
    urgent, _ = await claim(env)
    assert urgent[0]["id"] == risk["id"]
    cases, _ = await env.store.call("next_work", {}, env.box.settings, env.clock())
    state = build_state(cases, env.box.settings, {})
    assert len(cases) == 8
    assert any("法沦功" in row[3] for row in state["参考前文"])
    assert not any("法沦功" in row[3] for row in state["待初筛消息"])
    assert state["词义提示"][0]["来源"].startswith("C")


async def test_recalled_quote_cannot_be_cleared_by_negative_screen(batch_env):
    env = batch_env
    raw, _ = await env.speak("被引用的背景", user_id="200099")
    await env.process()
    await env.speak(
        segments=[
            {"type": "reply", "data": {"id": raw["message_id"]}},
            {"type": "text", "data": {"text": "同意这条"}},
        ],
        user_id="200010",
    )
    for i in range(7):
        await env.speak("正常发言" + str(i), user_id=str(200020 + i))

    async def hook():
        await env.s.observe(
            {
                "post_type": "notice",
                "notice_type": "group_recall",
                "self_id": "100001",
                "group_id": "100002",
                "message_id": raw["message_id"],
            },
            "qq",
        )

    env.model.batch_hook = hook
    cases, _ = await claim(env)
    quote_case = await env.store.call("case", cases[0]["id"])
    assert quote_case["state"] == "incomplete" and "引用原文" in quote_case["data"]["reason"]


async def test_expiry_between_detail_calls_never_leaves_claimed_cases(batch_env):
    env = batch_env
    env.model.screen_score = 0.8
    await fill(env)

    async def slow():
        env.clock.now += 100

    env.model.hook = slow
    cases, _ = await claim(env)
    done = [await env.store.call("case", c["id"]) for c in cases]
    assert len(env.model.requests) == 1
    assert all(c["state"] == "incomplete" for c in done[1:])
    assert not any(c["state"] == "auditing" for c in done)


async def test_export_includes_shared_calls_without_double_counting(batch_env):
    from tools.export_case import export

    env = batch_env
    await fill(env)
    cases, _ = await claim(env)
    data = export(env.path / "state.sqlite3", cases[3]["id"])
    assert len(data["screenings"]) == 1 and len(data["calls"]) == 1
    assert data["calls"][0]["stage"] == "screen"


async def test_legacy_call_table_migrates_without_losing_usage(tmp_path):
    import sqlite3

    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE calls(id INTEGER PRIMARY KEY,case_id INTEGER,at REAL NOT NULL,ok INTEGER NOT NULL,model TEXT NOT NULL,input_tokens INTEGER NOT NULL,output_tokens INTEGER NOT NULL,elapsed_ms REAL NOT NULL,error TEXT NOT NULL)"
        )
        db.execute("INSERT INTO calls VALUES(1,NULL,1,1,'old',123,1,200,'')")
    store = Store(path)
    try:
        await store.open()
        row = await store.call("_row", "SELECT * FROM calls WHERE id=1")
        assert row["input_tokens"] == 123 and row["stage"] == "detail" and row["screening_id"] is None
        await store.close()
        await store.open()
        assert (await store.call("_row", "SELECT count(*) n FROM calls"))["n"] == 1
    finally:
        await store.close()


async def test_phases_yield_fairly_between_groups_and_do_not_reset_action_age(batch_env):
    env = batch_env
    env.configure(auto_recall=True)
    env.box.settings = replace(
        env.box.settings,
        groups=env.box.settings.groups + (replace(env.box.settings.groups[0], group_id="100003"),),
    )
    await fill(env)
    first, _ = await env.store.call("next_work", {}, env.box.settings, env.clock())
    env.model.screen_score = 0.8
    await env.s.dispatch(first)
    queued = await env.store.call("case", first[0]["id"])
    assert queued["state"] == "queued" and queued["data"]["batch"]["phase"] == "detail"
    env.s.served[first[0]["scope"]] = env.clock()
    for i in range(8):
        await env.speak("其他群" + str(i), group_id="100003")
    next_cases, _ = await env.store.call("next_work", env.s.served, env.box.settings, env.clock())
    assert {c["data"]["message"]["group"] for c in next_cases} == {"100003"}
    old_at = first[0]["data"]["message"]["at"]
    env.clock.now += 121
    assert queued["data"]["message"]["at"] == old_at
    from qq_message_guard.config import Deferred

    with pytest.raises(Deferred, match="时间"):
        env.s.gate(queued)


async def test_batch_disabled_restores_immediate_single_message(batch_env):
    env = batch_env
    env.box.settings = replace(env.box.settings, batch=replace(env.box.settings.batch, enabled=False))
    await fill(env, 1)
    cases, wait = await claim(env)
    assert len(cases) == 1 and wait == 0 and len(env.model.requests) == 1 and not env.model.batch_calls


async def test_lower_review_threshold_is_not_masked_by_screen_setting(batch_env):
    env = batch_env
    env.box.settings = replace(env.box.settings, checks=(replace(Settings().checks[0], review_at=0.10),))
    env.model.screen_score = 0.2
    env.model.locate_score = 0.2
    await fill(env)
    cases, _ = await claim(env)
    assert env.box.settings.screen_threshold == 0.1
    assert len(env.model.requests) == 8
    assert all([(await env.store.call("case", c["id"]))["state"] == "review" for c in cases])
