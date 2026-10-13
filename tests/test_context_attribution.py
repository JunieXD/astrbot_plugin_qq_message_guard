from dataclasses import replace

import pytest
from conftest import A, G, U

from qq_message_guard.client import Intent
from qq_message_guard.config import Deferred, GuardError, parse_settings
from qq_message_guard.lexicon import matches
from qq_message_guard.messages import bounded_context


async def test_default_24_group_messages_survive_long_silence_and_reload(env):
    env.model.score = 0.1
    for i in range(30):
        await env.speak(f"历史{i}", user_id=str(400000 + i))
        await env.process()
    env.clock.now += 7 * 3600
    await env.store.close()
    await env.store.open()
    _, case = await env.speak("七小时后继续讨论")
    assert len(case["data"]["context"]) == 24
    assert case["data"]["context"][0]["text"] == "历史6"
    await env.process()
    assert not env.bot.reads and not env.bot.writes


async def test_same_author_context_is_added_and_deduplicated(env):
    env.model.score = 0.1
    for i in range(6):
        await env.speak(f"作者前文{i}")
        await env.process()
    for i in range(25):
        await env.speak(f"别人{i}", user_id="500001")
        await env.process()
    _, case = await env.speak("接我的前文")
    rows = case["data"]["context"]
    assert len(rows) == 30 and len({r["mid"] for r in rows}) == 30
    assert [r["text"] for r in rows if r["user"] == U] == [f"作者前文{i}" for i in range(6)]
    await env.process()
    _, following = await env.speak("下一条")
    assert len({r["mid"] for r in following["data"]["context"]}) == len(following["data"]["context"])


async def test_history_scope_exempt_members_and_capacity(env):
    env.box.settings = replace(
        env.box.settings, advanced=replace(env.box.settings.advanced, context_cache_messages=128)
    )
    env.configure(exempt_users=("500001",))
    for i in range(130):
        await env.speak(f"豁免成员背景{i}", user_id="500001")
    count = await env.store.call("_row", "SELECT count(*) FROM history")
    assert count[0] == 128
    _, case = await env.speak("当前成员")
    assert len(case["data"]["context"]) == 24
    raw, _ = await env.speak("未配置群", group_id="100003")
    assert all(row["scope"] == f"qq:{A}:{G}" for row in case["data"]["context"])
    assert raw["group_id"] == "100003"


async def test_recalled_quote_not_used_by_queued_audit(env):
    env.configure(auto_recall=True, warn_member=True)
    first, _ = await env.speak("前文")
    await env.process()
    await env.speak(
        segments=[
            {"type": "reply", "data": {"id": first["message_id"]}},
            {"type": "text", "data": {"text": "继续"}},
        ]
    )
    await env.s.observe(
        {
            "post_type": "notice",
            "notice_type": "group_recall",
            "self_id": A,
            "group_id": G,
            "message_id": first["message_id"],
        },
        "qq",
    )
    case = await env.process()
    assert case["state"] == "incomplete" and not case["data"]["auto_ready"]
    assert "当前引用" not in env.model.requests[-1][0]


@pytest.mark.parametrize("during_model", [False, True])
async def test_quote_recall_during_model_or_before_action_cancels_punishment(env, during_model):
    env.configure(auto_recall=True, warn_member=True)
    first, _ = await env.speak("被引用的原文")
    await env.process()
    await env.speak(
        segments=[
            {"type": "reply", "data": {"id": first["message_id"]}},
            {"type": "text", "data": {"text": "引用来讨论"}},
        ]
    )

    async def recall_quote():
        await env.s.observe(
            {
                "post_type": "notice",
                "notice_type": "group_recall",
                "self_id": A,
                "group_id": G,
                "message_id": first["message_id"],
            },
            "qq",
        )

    if during_model:
        env.model.hook = recall_quote
    case = await env.process()
    if during_model:
        assert case["state"] == "incomplete" and not case["data"]["auto_ready"]
    else:
        await recall_quote()
        with pytest.raises(Deferred, match="引用"):
            await env.s.recall(case)
        with pytest.raises(Deferred, match="引用"):
            await env.s.warn(case)
    assert not env.bot.writes


def test_context_budget_prefers_quote_without_reordering():
    context = [{"text": "a" * 100, "quoted": True}, {"text": "b" * 200}, {"text": "c" * 100}]
    assert bounded_context(context, 200) == [context[0], context[2]]


@pytest.mark.parametrize(
    "choice,confidence", [("report_or_stop", 0.9), ("normal", 0.9), ("participating", 0.3)]
)
async def test_nonparticipation_or_uncertainty_cannot_auto_punish_even_high_scores(env, choice, confidence):
    env.configure(auto_recall=True, warn_member=True)
    env.model.intent = Intent(choice, {}, confidence)
    await env.speak("引用来举报或边界内容")
    case = await env.process()
    assert case["state"] == "review" and not case["data"]["auto_ready"]
    assert not case["data"]["recall_ready"] and not case["data"]["warn_ready"]
    assert case["data"]["intent"]["choice"] == choice
    with pytest.raises(Deferred):
        await env.s.recall(case)
    with pytest.raises(Deferred):
        await env.s.warn(case)
    assert not env.bot.writes


async def test_keyword_only_review_not_automatic_punishment(env):
    env.configure(auto_recall=True, warn_member=True)
    env.model.score = 0.1
    await env.speak("ＢＥＩＪＩＮＧ　ＢＡＯＬＵＡＮ")
    case = await env.process()
    assert case["state"] == "review" and case["flagged"]
    assert case["data"]["keyword_matches"] and not case["data"]["auto_ready"]
    assert "词义提示" in env.model.requests[-1][0]
    assert not env.bot.writes


async def test_background_hit_is_not_current_hit_and_report_can_dismiss_keyword_only(env):
    env.model.score = 0.1
    await env.speak("法沦功")
    await env.process()
    env.model.intent = Intent("report_or_stop", {}, 0.9)
    await env.speak("请别说法沦功，到此为止")
    report = await env.process()
    assert report["state"] == "allowed" and report["data"]["keyword_matches"]
    await env.speak("小心炸群", user_id="500001")
    ordinary = await env.process()
    assert ordinary["state"] == "allowed" and not ordinary["data"]["keyword_matches"]


async def test_same_author_meanings_are_disambiguation_only(env):
    env.model.score = 0.1
    await env.speak("牛子")
    await env.process()
    await env.speak("如果是，去当保安")
    case = await env.process()
    assert case["state"] == "allowed" and not case["data"]["keyword_matches"]
    hint = env.model.requests[-1][0]["词义提示"]["候选"][0]
    assert "同一作者" in hint["来源"]


async def test_warning_output_gate_does_not_repeat_flagged_words(env):
    env.configure(warn_member=True, warning_text="请不要继续讨论法沦功")
    await env.speak()
    case = await env.process()
    with pytest.raises(GuardError, match="警告内容"):
        await env.s.warn(case)
    assert not env.bot.writes


async def test_incomplete_keyword_evidence_keeps_long_retention(env):
    env.model.failure = True
    await env.speak("法沦功")
    case = await env.process()
    assert case["state"] == "incomplete" and case["flagged"]
    await env.store.call("maintain", env.clock() + 15 * 86400, 14, 60)
    assert await env.store.call("case", case["id"])


async def test_keyword_evidence_is_kept_when_queue_is_full(env):
    env.box.settings = replace(env.box.settings, advanced=replace(env.box.settings.advanced, queue_size=1))
    await env.speak("占满队列")
    _, case = await env.speak("法沦功")
    assert case["state"] == "incomplete" and case["flagged"]


async def test_duplicate_event_does_not_change_persisted_history(env):
    raw, _ = await env.speak()
    await env.s.observe(raw, "qq")
    count = await env.store.call("_row", "SELECT count(*) FROM history")
    assert count[0] == 1


async def test_two_accounts_in_same_group_do_not_share_history(env):
    await env.speak("第一个账号收到的背景")
    _, other = await env.speak("第二个账号收到的当前消息", self_id="100003")
    assert other["data"]["context"] == []
    _, original = await env.speak("第一个账号继续发言")
    assert [row["text"] for row in original["data"]["context"]] == ["第一个账号收到的背景"]


@pytest.mark.parametrize("text", ["法\u200b沦功", "ＦＡ　ＬＵＮ　ＧＯＮＧ", "你 牛 子 是否异于常人"])
def test_alias_normalization(text):
    assert matches(text)


def test_custom_meanings_validation_and_targeted_checks():
    settings = parse_settings(
        {
            "terms": [{"name": "自定义缩写", "aliases": ["XYZ"], "meaning": "测试含义"}],
            "checks": [{"name": "敏感话题"}, {"name": "粗俗性玩笑"}],
        }
    )
    assert matches("x y z", settings.terms)[0]["释义"] == "测试含义"
    with pytest.raises(GuardError):
        parse_settings({"terms": [{"name": "无效", "aliases": ["!!!"], "meaning": "符号"}]})
    assert not matches("天安门旅游要预约吗？CS专业怎么报名？")
