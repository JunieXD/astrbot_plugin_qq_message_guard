from dataclasses import replace

import httpx
import pytest
from conftest import ADMIN

from qq_message_guard.classification import classification_text
from qq_message_guard.client import Result
from qq_message_guard.commands import Commands, lines
from qq_message_guard.config import GuardError, InputRejected, Qwen, Settings, parse_settings
from qq_message_guard.decisions import decide
from qq_message_guard.notifications import is_notifiable, notification_text
from qq_message_guard.qwen import TEMPLATE, QwenGuard, parse_classification


def settings(**changes):
    return replace(Settings(), backend="qwen3guard", model="Qwen3Guard-Gen-4B-Q4_K_M", **changes)


def response(content="Safety: Unsafe\nCategories: Politically Sensitive Topics", **changes):
    return {
        "model": "Qwen3Guard-Gen-4B-Q4_K_M",
        "content": content,
        "tokens_evaluated": 322,
        "tokens_predicted": 15,
        "truncated": False,
        "stopped_limit": False,
        **changes,
    }


@pytest.mark.parametrize(
    "value",
    [
        "",
        "Safety: Safe",
        "Safety: Safe\nCategories: Politically Sensitive Topics",
        "Safety: Unsafe\nCategories: None",
        "Safety: Controversial\nCategories: None",
        "Safety: Unsafe\nCategories: Unknown",
        "Safety: Unsafe\nCategories: Violent, None",
        "Safety: Unsafe\nCategories: Violent, Violent",
        "Safety: Safe\nCategories: None\nOther output",
    ],
)
def test_malformed_classification_is_not_safe(value):
    with pytest.raises(GuardError):
        parse_classification(value)


@pytest.mark.parametrize("label", ["Unsafe", "Controversial"])
def test_categorical_decisions_are_not_fake_probabilities(label):
    r = Result("qwen", {}, 0, 0, 0, safety=label, categories=("Politically Sensitive Topics",))
    decision = decide(r, settings())
    assert decision.flagged and decision.notify
    assert decision.recall == decision.warn == (label == "Unsafe")
    assert r.scores == {}
    text = classification_text({"classification": {"safety": label, "categories": r.categories}})
    assert "%" not in text and "敏感话题" in text


def test_disabled_categories_and_controversial_switch():
    r = Result("qwen", {}, 0, 0, 0, safety="Unsafe", categories=("PII",))
    assert not decide(r, settings()).flagged
    r = replace(r, safety="Controversial", categories=("Violent",))
    assert not decide(r, settings(qwen=Qwen(include_controversial=False))).flagged


def test_controversial_automatic_actions_require_explicit_switch():
    r = Result("qwen", {}, 0, 0, 0, safety="Controversial", categories=("Violent",))
    decision = decide(r, settings(qwen=Qwen(act_on_controversial=True)))
    assert decision.recall and decision.warn and decision.notify
    with pytest.raises(GuardError):
        parse_settings({"qwen": {"include_controversial": False, "act_on_controversial": True}})


@pytest.mark.parametrize("automatic_enabled", [False, True])
def test_local_only_controversial_category_never_notifies_or_acts(automatic_enabled):
    r = Result("qwen", {}, 0, 0, 0, safety="Controversial", categories=("Unethical Acts",))
    decision = decide(r, settings(qwen=Qwen(act_on_controversial=automatic_enabled)))
    assert decision.flagged and "本地记录" in decision.reason
    assert not decision.notify and not decision.recall and not decision.warn


def test_local_only_rule_does_not_suppress_unsafe_or_other_controversial_categories():
    r = Result("qwen", {}, 0, 0, 0, safety="Unsafe", categories=("Unethical Acts",))
    decision = decide(r, settings())
    assert decision.notify and decision.recall and decision.warn
    r = replace(r, safety="Controversial", categories=("Unethical Acts", "Politically Sensitive Topics"))
    decision = decide(r, settings())
    assert decision.notify and not decision.recall and not decision.warn


def test_local_only_categories_are_configurable_and_validated():
    cfg = parse_settings({"backend": "qwen3guard"})
    assert cfg.qwen.controversial_local_only == ("不当行为",)
    r = Result("qwen", {}, 0, 0, 0, safety="Controversial", categories=("Unethical Acts",))
    cfg = replace(cfg, qwen=replace(cfg.qwen, controversial_local_only=()))
    assert decide(r, cfg).notify
    assert cfg.revision != parse_settings({"backend": "qwen3guard"}).revision
    for names in (None, ["Unknown"], ["不当行为", "不当行为"]):
        with pytest.raises(GuardError):
            parse_settings({"qwen": {"controversial_local_only": names}})
    assert parse_settings({"qwen": {"controversial_local_only": []}}).qwen.controversial_local_only == ()


def test_qwen_configuration():
    cfg = parse_settings({"backend": "qwen3guard"})
    assert cfg.model == "Qwen3Guard-Gen-4B-Q4_K_M"
    assert cfg.base_url == "http://127.0.0.1:8001"
    assert not cfg.batching_enabled
    assert cfg.cooldown_key != Settings().cooldown_key
    for qwen in ({"categories": ["仇恨歧视"]}, {"categories": []}, {"include_controversial": 1}):
        with pytest.raises(GuardError):
            parse_settings({"backend": "qwen3guard", "qwen": qwen})


async def test_native_transport_and_official_prompt():
    def reply(request):
        import json

        body = json.loads(request.content)
        assert request.url.path == "/completion"
        assert body["prompt"] == TEMPLATE["prefix"] + "[成员1] 当前内容" + TEMPLATE["suffix"]
        assert body["n_predict"] == 96
        return httpx.Response(200, json=response())

    model = QwenGuard(httpx.MockTransport(reply))
    try:
        result = await model.evaluate({"current_text": "当前内容", "background": "不该进入模型"}, settings())
    finally:
        await model.close()
    assert result.safety == "Unsafe" and result.input_tokens == 322
    assert result.categories == ("Politically Sensitive Topics",)


async def test_current_llama_stop_type():
    value = response(stop_type="eos")
    value.pop("stopped_limit")
    model = QwenGuard(httpx.MockTransport(lambda r: httpx.Response(200, json=value)))
    try:
        result = await model.evaluate({"current_text": "hello"}, settings())
        assert result.safety == "Unsafe"
    finally:
        await model.close()


@pytest.mark.parametrize(
    "value",
    [
        response(truncated=True),
        response(stopped_limit=True),
        response(stop_type="limit"),
        response(tokens_evaluated=True),
        response(model="wrong-model"),
        response(content="failed"),
        {"content": "Safety: Safe\nCategories: None"},
    ],
)
async def test_incomplete_responses_are_not_safe(value):
    model = QwenGuard(httpx.MockTransport(lambda r: httpx.Response(200, json=value)))
    try:
        with pytest.raises(GuardError):
            await model.evaluate({"current_text": "hello"}, settings())
    finally:
        await model.close()


async def test_input_overflow_is_not_global_outage():
    model = QwenGuard(httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "too large"})))
    try:
        with pytest.raises(InputRejected):
            await model.evaluate({"current_text": "long input"}, settings())
    finally:
        await model.close()


def use_qwen(env, content="Safety: Unsafe\nCategories: Politically Sensitive Topics"):
    env.box.settings = replace(env.box.settings, backend="qwen3guard", model="Qwen3Guard-Gen-4B-Q4_K_M")
    requests = []

    def reply(request):
        import json

        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response(content))

    env.s.model = QwenGuard(httpx.MockTransport(reply))
    return requests


async def test_current_only_end_to_end_with_categorical_actions(env):
    env.configure(auto_recall=True, warn_member=True, notify_private=True)
    requests = use_qwen(env)
    try:
        _, old = await env.speak("不该进入模型的前文")
        # Never fetch neighboring or quoted history in this backend, even when
        # legacy context/batch options retain their enabled defaults.
        call = env.store.call

        async def no_history(name, *args):
            assert name != "history_context"
            return await call(name, *args)

        env.store.call = no_history
        _, case = await env.speak(
            segments=[
                {"type": "reply", "data": {"id": str(old["id"])}},
                {"type": "text", "data": {"text": "当前测试内容"}},
            ]
        )
        # Removing somebody else's quoted message must not hide a separate
        # violation in the reply author's own text.
        env.s.invalidated[(case["scope"], "1")] = env.clock() + 600
        work, wait = await env.store.call("next_work", {}, env.box.settings, env.clock(), {})
        assert len(work) == 1 and wait == 0
        await env.s.process(await env.store.call("case", case["id"]))
        case = await env.store.call("case", case["id"])
        assert case["flagged"] and case["data"]["recall_ready"] and case["data"]["warn_ready"]
        assert case["data"]["context_used"] == 0 and case["data"]["audit_mode"] == "current_only"
        assert case["data"]["scores"] == {} and len(requests) == 1
        assert "不该进入模型的前文" not in requests[0]["prompt"]
        assert "当前测试内容" in requests[0]["prompt"]
        assert (await env.store.call("stats", env.clock()))["calls"]["ok"] == 1
        await env.store.call("patch", case["id"], {"actions_pending": False})
        case = await env.store.call("case", case["id"])
        assert is_notifiable(case)
        assert case["id"] in [r["id"] for r in await env.store.call("alerts", env.box.settings.revision)]
        text = notification_text([case], env.box.settings.groups[0], env.box.settings)
        assert "敏感话题 · 不安全" in text and "%" not in text and "当前测试内容" not in text
        assert "不安全" in lines(case)
        status = await Commands(env.s).run("/消息审核 状态", ADMIN, "qq", "100001")
        assert "仅审核当前发言" in status and "无百分比" in status
    finally:
        await env.s.model.close()


async def test_quoted_content_does_not_become_reply_authors_content(env):
    requests = use_qwen(env, "Safety: Safe\nCategories: None")
    try:
        await env.speak("其他成员的原文不属于回复者")
        _, case = await env.speak(
            segments=[
                {"type": "reply", "data": {"id": "1"}},
                {"type": "text", "data": {"text": "我们聊点学习吧"}},
            ]
        )
        await env.s.process(await env.store.call("case", case["id"]))
        result = await env.store.call("case", case["id"])
        assert result["state"] == "allowed" and not result["flagged"]
        assert "其他成员的原文不属于回复者" not in requests[0]["prompt"]
        assert "我们聊点学习吧" in requests[0]["prompt"]
    finally:
        await env.s.model.close()


async def test_controversial_only_notifies_even_when_group_auto_actions_are_enabled(env):
    env.configure(auto_recall=True, warn_member=True, notify_group=True, admin_group="123456")
    use_qwen(env, "Safety: Controversial\nCategories: Politically Sensitive Topics")
    try:
        await env.speak()
        case = await env.process()
        assert case["flagged"] and case["data"]["notification_ready"]
        assert not case["data"]["recall_ready"] and not case["data"]["warn_ready"]
        assert not case["data"]["actions_pending"]
        assert await env.store.call("next_recall") is None
        await env.s.send_alerts()
        assert [a for a, _ in env.bot.writes] == ["send_group_msg"]
        assert env.bot.writes[0][1]["group_id"] == "123456"
        text = env.bot.writes[0][1]["message"][0]["data"]["text"]
        assert "有争议" in text and "未撤回" in text
    finally:
        await env.s.model.close()


async def test_unethical_controversial_is_preserved_locally_without_alerts_or_actions(env):
    env.configure(auto_recall=True, warn_member=True, notify_group=True, admin_group="123456")
    use_qwen(env, "Safety: Controversial\nCategories: Unethical Acts")
    try:
        await env.speak("素质极差")
        case = await env.process()
        assert case["state"] == "review" and case["flagged"]
        assert case["data"]["classification"]["matched"] == ["Unethical Acts"]
        assert case["data"]["classification"]["actionable"] == []
        assert (
            case["data"]["audit_response"]["content"] == "Safety: Controversial\nCategories: Unethical Acts"
        )
        assert "本地记录" in case["data"]["reason"]
        assert not case["data"]["recall_ready"] and not case["data"]["warn_ready"]
        assert not case["data"]["notification_ready"] and not case["data"]["actions_pending"]
        assert (env.path / "evidence" / f"{case['id']}-source.json").exists()
        assert await env.store.call("next_recall") is None
        assert not await env.store.call("alerts", env.box.settings.revision)
        await env.s.send_alerts()
        assert not env.bot.writes
    finally:
        await env.s.model.close()


async def test_local_only_hit_does_not_hide_other_risk_or_appear_as_notification_reason(env):
    env.configure(notify_group=True, admin_group="123456")
    use_qwen(env, "Safety: Controversial\nCategories: Unethical Acts, Politically Sensitive Topics")
    try:
        await env.speak()
        case = await env.process()
        assert case["data"]["notification_ready"]
        assert case["data"]["classification"]["actionable"] == ["Politically Sensitive Topics"]
        assert "不当行为" in classification_text(case["data"])
        await env.s.send_alerts()
        assert len(env.bot.writes) == 1
        text = env.bot.writes[0][1]["message"][0]["data"]["text"]
        assert "敏感话题" in text and "不当行为" not in text
    finally:
        await env.s.model.close()


@pytest.mark.parametrize("ocr_enabled,ocr_text", [(False, ""), (True, "")])
async def test_unreadable_images_are_incomplete_not_safe(env, ocr_enabled, ocr_text):
    requests = use_qwen(env)
    env.box.settings = replace(env.box.settings, ocr_enabled=ocr_enabled)
    env.ocr.text = ocr_text
    try:
        await env.speak(segments=[{"type": "image", "data": {"file": "p.jpg", "url": "https://qpic.cn/x"}}])
        case = await env.process()
        assert case["state"] == "incomplete" and case["data"]["missing"]
        assert not requests and not env.bot.writes
    finally:
        await env.s.model.close()


async def test_image_ocr_uses_text_and_reuses_native_verdict(env):
    requests = use_qwen(env)
    from qq_message_guard.config import Cache

    env.box.settings = replace(env.box.settings, cache=Cache())
    env.ocr.text = "图片上的当前文字"
    try:
        for i in range(2):
            await env.speak(
                segments=[{"type": "image", "data": {"file": "p.jpg", "url": "https://qpic.cn/x"}}]
            )
            case = await env.process()
            assert case["flagged"]
            assert case["data"]["classification"]["safety"] == "Unsafe"
        assert len(requests) == 1
        assert "图片上的当前文字" in requests[0]["prompt"]
        assert case["data"]["audit_path"] == "image_cache"
        assert case["data"]["usage"]["input_tokens"] == 0
    finally:
        await env.s.model.close()


@pytest.mark.parametrize(
    "text,has_url", [("", True), ("", False), ("当前违规文字", True), ("当前违规文字", False)]
)
async def test_image_switch_skips_download_ocr_and_only_audits_text(env, monkeypatch, text, has_url):
    requests = use_qwen(env)
    env.box.settings = replace(env.box.settings, images_enabled=False)

    async def forbidden(*args):
        raise AssertionError("Images disabled: media download and OCR must never run")

    monkeypatch.setattr(env.media, "load", forbidden)
    monkeypatch.setattr(env.ocr, "recognize", forbidden)
    image = {"file": "a.jpg", **({"url": "https://qpic.cn/x"} if has_url else {})}
    try:
        _, initial = await env.speak(
            segments=[{"type": "image", "data": image}, {"type": "text", "data": {"text": text}}]
        )
        if text:
            case = await env.process()
            assert case["flagged"] and case["state"] == "review"
            assert case["data"]["missing"] == []
            assert case["data"]["coverage"] == "current_text_only"
            assert len(requests) == 1
        else:
            assert initial["state"] == "skipped" and not requests
            assert await env.process() is None
    finally:
        await env.s.model.close()
