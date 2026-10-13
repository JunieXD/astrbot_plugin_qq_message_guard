from dataclasses import replace

import pytest

IMAGE = [{"type": "image", "data": {"file": "a.jpg", "url": "https://gchat.qpic.cn/a"}}]


async def test_ocr_evidence_and_original_image_both_reach_model(env):
    env.model.score = 0.1
    env.ocr.text = "你 牛 子 是否异于常人"
    await env.speak(segments=IMAGE)
    case = await env.process()
    assert case["state"] == "review" and case["data"]["keyword_matches"]
    state, _, media = env.model.requests[-1]
    assert state["当前待审核消息"]["图片文字"] == [env.ocr.text]
    assert len(media) == 1 and "<image:1>" in state["当前待审核消息"]["图片"]
    assert case["data"]["ocr_records"][0]["backend"] == "fake-local-ocr"


@pytest.mark.parametrize("issue", ["failure", "truncated"])
async def test_ocr_missing_coverage_never_auto_punishes(env, issue):
    env.configure(auto_recall=True, warn_member=True)
    setattr(env.ocr, issue, True)
    await env.speak(segments=IMAGE)
    case = await env.process()
    assert case["state"] == "incomplete" and case["data"]["missing"]
    assert not case["data"]["auto_ready"] and len(env.model.requests[-1][2]) == 1


async def test_ocr_can_be_disabled_without_disabling_image_model(env):
    env.box.settings = replace(env.box.settings, ocr_enabled=False)
    env.ocr.failure = True
    await env.speak(segments=IMAGE)
    case = await env.process()
    assert case["state"] == "review" and not case["data"]["ocr_records"]
    assert not case["data"]["missing"] and len(env.model.requests[-1][2]) == 1
