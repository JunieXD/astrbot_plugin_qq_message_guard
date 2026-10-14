import asyncio
import base64
import copy
import io
import json
import os
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import ADMIN, A
from PIL import Image, ImageChops

from qq_message_guard.commands import Commands
from qq_message_guard.config import Evidence, GuardError, parse_settings
from qq_message_guard.evidence import HEIGHT, MAX_PAGES, evidence_path, local_html, source_data


@pytest.fixture
def picture_config():
    # CI verifies transport/layout with an installed font; local Chinese samples
    # are inspected separately with Microsoft's Chinese font.
    candidates = [
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/segoeui.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
    ]
    font = next((path for path in candidates if path.exists()), None)
    assert font, "CI needs a system TrueType font"
    return Evidence(notify_images=True, review_images=True, font_path=str(font))


async def test_archive_is_saved_before_recall_and_review_works_after_message_disappears(env, picture_config):
    env.box.settings = replace(env.box.settings, evidence=picture_config)
    env.configure(auto_recall=True, notify_group=True, admin_group="123456")
    raw, _ = await env.speak("Original publication for moderator review.")
    case = await env.process()

    async def before(action):
        if action == "delete_msg":
            source = env.path / f"evidence/{case['id']}-source.json"
            assert (
                json.loads(source.read_text(encoding="utf-8"))["message"]["text"]
                == raw["message"][0]["data"]["text"]
            )
            assert (env.path / f"evidence/{case['id']}-local.html").exists()
            # The attachment and text evidence don't depend on a post-recall QQ query.

    async def after(action):
        if action == "delete_msg":
            env.bot.messages.clear()

    env.bot.before, env.bot.after = before, after
    await env.s.recall(case)
    reads_before = sum(action == "get_msg" for action, _ in env.bot.reads)
    result = await Commands(env.s).run(f"/消息审核 查看 {case['id']}", ADMIN, "qq", A)
    assert result is None
    assert sum(action == "get_msg" for action, _ in env.bot.reads) == reads_before
    action, params = env.bot.writes[-1]
    assert action == "send_private_msg" and params["user_id"] == ADMIN
    assert all(segment["type"] == "image" for segment in params["message"])
    for segment in params["message"]:
        assert segment["data"]["file"].startswith("base64://")
        Image.open(io.BytesIO(base64.b64decode(segment["data"]["file"][9:]))).verify()


async def test_notification_contains_safe_metadata_and_preview_not_raw_attachment(env, picture_config):
    env.box.settings = replace(env.box.settings, evidence=picture_config)
    env.configure(notify_group=True, admin_group="123456")
    await env.speak(
        segments=[
            {"type": "text", "data": {"text": "Exact original publication."}},
            {"type": "image", "data": {"file": "attachment.jpg", "url": "https://gchat.qpic.cn/a"}},
        ]
    )
    case = await env.process()
    await env.s.send_alerts()
    segments = env.bot.writes[0][1]["message"]
    assert len(segments) == 2 and segments[0]["type"] == "text" and segments[1]["type"] == "image"
    assert "Exact original publication." not in segments[0]["data"]["text"]
    assert f"#{case['id']}" in segments[0]["data"]["text"]
    media = env.path / case["data"]["media_records"][0]["file"]
    assert media.exists()
    assert base64.b64decode(segments[1]["data"]["file"][9:]) != media.read_bytes()


async def test_no_font_falls_back_to_metadata_without_stopping_audit(env):
    env.box.settings = replace(
        env.box.settings, evidence=Evidence(notify_images=True, font_path="missing.ttf")
    )
    env.configure(notify_group=True, admin_group="123456")
    await env.speak("Never echo this original in fallback.")
    case = await env.process()
    await env.s.send_alerts()
    assert env.s.failure == "" and len(env.bot.writes) == 1
    segments = env.bot.writes[0][1]["message"]
    assert len(segments) == 1 and segments[0]["type"] == "text"
    assert "Never echo" not in segments[0]["data"]["text"]
    reply = await Commands(env.s).run(f"/消息审核 查看 {case['id']}", ADMIN, "qq", A)
    assert "证据图片未确认送达" in reply and "Never echo" not in reply


async def test_archive_failure_blocks_automatic_recall_but_keeps_verdict(env, monkeypatch):
    env.configure(auto_recall=True)

    def fail(*args):
        raise OSError("simulated disk failure")

    monkeypatch.setattr("qq_message_guard.evidence.atomic_write", fail)
    await env.speak()
    case = await env.process()
    assert case["flagged"] and case["data"]["scores"]
    assert not case["data"]["recall_ready"] and "本地证据归档未完成" in case["data"]["missing"]
    assert not env.bot.writes


async def test_long_publication_is_archived_beyond_model_budget_and_images_are_bounded(env, picture_config):
    env.box.settings = replace(
        env.box.settings, advanced=replace(env.box.settings.advanced, max_text_chars=200)
    )
    original = "Publication with a complete local archive. " * 140 + "ORIGINAL END"
    await env.speak(original)
    case = await env.process()
    assert len(case["data"]["message"]["text"]) == 200
    assert source_data(case)["message"]["text"] == original
    pictures = await env.s.evidence.render(case, picture_config)
    assert 1 < len(pictures.preview) <= MAX_PAGES
    for preview, clear in zip(pictures.preview, pictures.clear):
        with Image.open(preview) as decorated, Image.open(clear) as plain:
            assert decorated.height <= HEIGHT
            assert ImageChops.difference(decorated, plain).getbbox()
    source = json.loads((env.path / f"evidence/{case['id']}-source.json").read_text(encoding="utf-8"))
    assert source["message"]["text"].endswith("ORIGINAL END")
    assert "ORIGINAL END" in (env.path / f"evidence/{case['id']}-local.html").read_text(encoding="utf-8")
    shorter = copy.deepcopy(case)
    shorter["data"]["original_text"] = {"text": "short", "truncated": False}
    await env.s.evidence.render(shorter, replace(picture_config, line_width=0))
    assert not (env.path / f"evidence/{case['id']}-notice-2.png").exists()


async def test_evidence_retention_tracks_case_not_render_time(env, picture_config):
    env.model.score = 0.1
    await env.speak("Normal publication.")
    normal = await env.process()
    await env.s.evidence.render(normal, picture_config)
    env.model.score = 0.95
    await env.speak("Risk candidate.")
    risk = await env.process()
    await env.s.evidence.render(risk, picture_config)
    env.clock.now += 15 * 86400
    await env.s.maintain()
    assert not (env.path / f"evidence/{normal['id']}-local.html").exists()
    assert (env.path / f"evidence/{risk['id']}-notice-1.png").exists()
    env.clock.now += 46 * 86400
    await env.s.maintain()
    assert list((env.path / "evidence").iterdir()) == []


async def test_viewing_a_marked_record_does_not_requeue_actions_or_extend_retention(env, picture_config):
    env.configure(auto_recall=True)
    await env.speak()
    case = await env.process()
    await env.s.mark(case["id"], "误判", ADMIN)
    before = await env.store.call("case", case["id"])
    assert before["action_ready"] == 0 and before["data"]["auto_ready"]
    env.clock.now += 60
    await env.s.evidence.render(before, picture_config)
    after = await env.store.call("case", case["id"])
    assert after["action_ready"] == 0 and after["updated"] == before["updated"]
    assert await env.store.call("next_recall") is None


async def test_render_cancellation_waits_for_thread_before_releasing_lock(env, picture_config, monkeypatch):
    await env.speak()
    case = await env.process()
    started, release = threading.Event(), threading.Event()

    def blocking(*args):
        started.set()
        assert release.wait(5)

    monkeypatch.setattr(env.s.evidence, "_render", blocking)
    task = asyncio.create_task(env.s.evidence.render(case, picture_config))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done() and env.s.evidence.lock.locked()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not env.s.evidence.lock.locked()


async def test_uncertain_review_delivery_is_not_retried(env, picture_config):
    env.box.settings = replace(env.box.settings, evidence=picture_config)
    await env.speak()
    case = await env.process()
    env.bot.fail = "send_private_msg"
    reply = await Commands(env.s).run(f"/消息审核 查看 {case['id']}", ADMIN, "qq", A)
    assert "未确认送达" in reply
    assert len(env.bot.writes) == 1
    await Commands(env.s).run(f"/消息审核 查看 {case['id']}", ADMIN, "qq", A)
    assert len(env.bot.writes) == 1  # Account pause stops a second manual attempt.


async def test_local_html_escapes_chat_content_and_cleanup_rejects_external_paths(env):
    await env.speak('<script>alert("test")</script><img src="https://example.com">')
    case = await env.process()
    page = local_html(source_data(case))
    assert "<script>" not in page and "&lt;script&gt;" in page
    assert "Content-Security-Policy" in page
    for relative in ("../outside.txt", "media/../../outside.txt", "evidence/a/record.png", "C:/outside.png"):
        with pytest.raises(GuardError):
            evidence_path(env.path, relative)


def test_picture_settings_validation():
    parsed = parse_settings({"evidence": {"line_width": 6, "notify_images": True, "review_images": True}})
    assert parsed.evidence.line_width == 6 and parsed.evidence.notify_images
    for invalid in (
        {"line_width": 11},
        {"context_messages": 6},
        {"notify_images": "true"},
        {"font_path": "x\n"},
    ):
        with pytest.raises(GuardError):
            parse_settings({"evidence": invalid})
