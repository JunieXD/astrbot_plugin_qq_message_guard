import json
from dataclasses import replace

import httpx
import pytest

from qq_message_guard.categories import CATEGORIES, DEFAULT_NAMES
from qq_message_guard.client import OneJev
from qq_message_guard.config import Check, Deferred, Settings, parse_settings


def test_sub2api_thirteen_categories_and_chat_defaults():
    assert {c.key for c in CATEGORIES.values() if c.standard} == {
        "harassment",
        "harassment/threatening",
        "hate",
        "hate/threatening",
        "illicit",
        "illicit/violent",
        "self-harm",
        "self-harm/intent",
        "self-harm/instructions",
        "sexual",
        "sexual/minors",
        "violence",
        "violence/graphic",
    }
    assert tuple(c.name for c in Settings().checks) == DEFAULT_NAMES
    assert parse_settings({}).checks == Settings().checks
    assert "群规" not in DEFAULT_NAMES
    assert len({c.key for c in CATEGORIES.values()}) == len(CATEGORIES)


async def test_all_categories_use_one_request_and_one_score_each():
    settings = Settings(checks=tuple(Check(name) for name in CATEGORIES))
    seen = []

    def respond(request):
        body = json.loads(request.content)
        seen.append(body)
        for check in settings.checks:
            category = CATEGORIES[check.name]
            question = body["questions"][check.key]
            assert question["criteria"] == {"true": category.positive, "false": category.negative}
        return httpx.Response(
            200,
            json={
                "model": "test-categories",
                "answers": {
                    **{check.key: {"type": "noul", "noul": 0.1} for check in settings.checks},
                    "intent": {
                        "type": "choice",
                        "choice": "normal",
                        "probabilities": {"normal": 0.9, "participating": 0.05, "report_or_stop": 0.05},
                        "confidence": 0.85,
                    },
                },
                "usage": {"input_tokens": 1000, "output_tokens": len(settings.checks) + 1},
            },
        )

    client = OneJev(httpx.MockTransport(respond))
    try:
        result = await client.evaluate({"群规": "自定义群规", "当前待审核消息": {"内容": "test"}}, settings)
        assert len(seen) == 1 and len(result.scores) == len(CATEGORIES)
        assert "群规" in seen[0]["state"]  # Explicit legacy composite check still works.
    finally:
        await client.close()


def test_categorical_prefix_does_not_duplicate_definitions_or_inject_composite_rules():
    settings = Settings()
    body = OneJev.request({"群规": "商业、政治等一切都算违规", "背景": [], "当前待审核消息": {}}, settings)
    assert "群规" not in body["state"]
    assert body["state"]["审核范围"] == list(DEFAULT_NAMES)
    assert "rules" not in body["questions"] and len(body["questions"]) == len(DEFAULT_NAMES) + 1
    assert all(c.positive not in str(body["state"]) for c in CATEGORIES.values())


async def test_only_actual_notification_categories_are_displayed(env):
    env.configure(notify_group=True, admin_group="123456")
    env.box.settings = replace(env.box.settings, checks=(Check("商业推广"), Check("色情"), Check("敏感话题")))
    original = env.model.evaluate

    async def scores(*args):
        result = await original(*args)
        return replace(result, scores={"advertising": 0.98, "sexual": 0.6, "sensitive_topics": 0.1})

    env.model.evaluate = scores
    await env.speak("法沦功")  # A lexical candidate must not invent a high political score.
    case = await env.process()
    await env.s.send_alerts()
    text = str(env.bot.writes[0])
    assert "疑点：商业推广" in text and "敏感话题" not in text and "色情" not in text
    assert case["data"]["scores"]["sexual"] == 0.6  # Retained for local review.
    assert "群规" not in case["data"]["audit_request"]["state"]


async def test_all_default_categories_are_persisted_and_keep_action_gates(env):
    env.box.settings = replace(env.box.settings, checks=Settings().checks)
    env.configure(auto_recall=True, warn_member=True, notify_group=True, admin_group="123456")
    await env.speak()
    case = await env.process()
    assert set(case["data"]["scores"]) == {c.key for c in Settings().checks}
    assert case["data"]["question_version"] == "qq-message-guard-categories-v4"
    assert case["data"]["actions_pending"] and case["data"]["notification_ready"]
    await env.s.send_alerts()
    assert not env.bot.writes  # Admin delivery continues to wait for actual actions.


async def test_self_harm_intent_alone_is_never_automatically_punished(env):
    env.box.settings = replace(env.box.settings, checks=(Check("自伤意图"),))
    env.configure(auto_recall=True, warn_member=True, notify_group=True, admin_group="123456")
    await env.speak()
    case = await env.process()
    assert case["data"]["notification_ready"] and not case["data"]["auto_ready"]
    await env.s.send_alerts()
    assert [a for a, _ in env.bot.writes] == ["send_group_msg"]
    assert env.bot.writes[0][1]["group_id"] == "123456"


async def test_definition_change_cannot_reuse_previous_automatic_decision(env, monkeypatch):
    env.configure(auto_recall=True)
    await env.speak()
    case = await env.process()
    monkeypatch.setitem(CATEGORIES, "群规", replace(CATEGORIES["群规"], positive="新的行为定义"))
    with pytest.raises(Deferred, match="配置已变化"):
        await env.s.recall(case)
    assert not env.bot.reads and not env.bot.writes
