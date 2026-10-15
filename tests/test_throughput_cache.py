import base64
import io
from dataclasses import replace

import httpx
import pytest
from conftest import ADMIN
from PIL import Image

from qq_message_guard.client import Media, compress_image
from qq_message_guard.config import Advanced, Batch, Cache, Check, Settings, SkippedImage


def image_segments(text="", reply=None):
    return (
        ([{"type": "reply", "data": {"id": reply}}] if reply else [])
        + ([{"type": "text", "data": {"text": text}}] if text else [])
        + [{"type": "image", "data": {"file": "same.jpg", "url": "https://gchat.qpic.cn/same"}}]
    )


@pytest.fixture
async def cached_env(env):
    env.box.settings = replace(env.box.settings, checks=Settings().checks, cache=Cache())
    env.model.score = 0.1
    return env


async def test_old_text_batch_is_not_starved_by_new_images(cached_env):
    env = cached_env
    await env.speak("Waiting text one.")
    await env.speak("Waiting text two.")
    env.clock.now += 10
    await env.speak(segments=image_segments())
    first, _ = await env.store.call("next_work", {}, env.box.settings, env.clock())
    assert len(first) == 1 and first[0]["data"]["message"]["images"]
    env.clock.now += 45  # A real observed image request can take this long.
    await env.speak(segments=image_segments())
    second, _ = await env.store.call("next_work", {}, env.box.settings, env.clock())
    assert len(second) == 2 and all(c["data"]["message"]["text"] for c in second)
    assert all(env.clock() - c["created"] < 90 for c in second)


async def test_new_batch_defaults_fill_sixteen_or_wait_eight_seconds(cached_env):
    env = cached_env
    assert Batch().max_messages == 16 and Batch().wait_seconds == 8
    for i in range(2):
        await env.speak(f"Message {i}")
    assert await env.store.call("next_work", {}, env.box.settings, env.clock()) == ([], 8)
    for i in range(14):
        await env.speak(f"More {i}")
    cases, wait = await env.store.call("next_work", {}, env.box.settings, env.clock())
    assert len(cases) == 16 and wait == 0


async def test_larger_batches_still_obey_text_budget(cached_env):
    env = cached_env
    for _ in range(10):
        await env.speak("a" * 600)
    cases, _ = await env.store.call("next_work", {}, env.box.settings, env.clock())
    assert len(cases) == 6 and sum(len(c["data"]["message"]["text"]) for c in cases) <= 4000


@pytest.mark.parametrize("role", ["admin", "owner"])
async def test_group_managers_skip_queue_model_and_notices_but_keep_context(env, role):
    env.configure(notify_group=True, admin_group="123456")
    await env.speak("Manager's background", sender={"role": role})
    assert not await env.store.call("recent") and not env.model.requests
    await env.s.send_alerts()
    assert not env.bot.writes
    await env.speak("A member's publication", user_id="200002")
    case = await env.process()
    assert case and any(row["text"] == "Manager's background" for row in case["data"]["context"])
    assert len(env.model.requests) == 1


async def test_admin_exemption_can_be_disabled_and_unknown_role_is_not_trusted(env):
    env.configure(skip_admins=False)
    await env.speak("Explicitly included manager", sender={"role": "admin"})
    assert await env.process()
    env.configure(skip_admins=True)
    await env.speak("Unknown role", sender={})
    assert await env.process()
    assert len(env.model.requests) == 2


async def test_gif_only_is_explicitly_skipped_but_mixed_text_is_audited(env, monkeypatch):
    async def skip(*args):
        raise SkippedImage("GIF")

    monkeypatch.setattr(env.media, "load", skip)
    await env.speak(segments=image_segments())
    first = await env.process()
    assert first["state"] == "skipped" and first["data"]["audit_path"] == "skipped_media"
    assert not env.model.requests and not first["flagged"]
    env.configure(auto_recall=True)
    await env.speak(segments=image_segments("Publication accompanying a GIF."))
    second = await env.process()
    assert len(env.model.requests) == 1 and second["data"]["scores"]
    assert second["data"]["ignored_media"] and not second["data"]["missing"]
    assert second["data"]["recall_ready"]


async def test_standalone_picture_reuses_scores_and_does_not_forge_http_usage(cached_env):
    env = cached_env
    await env.speak(segments=image_segments())
    first = await env.process()
    env.clock.now += 5
    await env.speak(segments=image_segments(), user_id="200002")
    second = await env.process()
    assert len(env.model.requests) == 1
    assert first["data"]["audit_mode"] == "image_content" and first["data"]["context_used"] == 0
    assert second["data"]["audit_path"] == "image_cache" and second["data"]["cache_source"] == first["id"]
    assert second["data"]["usage"]["input_tokens"] == 0 and second["data"]["usage"]["elapsed_ms"] == 0
    stats = await env.store.call("stats", env.clock())
    assert stats["calls"]["n"] == 1 and stats["calls"]["input"] == 123
    assert stats["image_cache_hits"] == 1 and stats["ocr_cache_hits"] == 1
    assert (env.path / second["data"]["media_records"][0]["file"]).exists()


async def test_cached_risk_still_targets_each_new_message_not_the_origin(cached_env):
    env = cached_env
    env.model.score = 0.97
    env.configure(auto_recall=True)
    for _ in range(2):
        await env.speak(segments=image_segments())
        case = await env.process()
        assert case["data"]["recall_ready"]
        await env.s.recall(case)
    assert len(env.model.requests) == 1
    assert [params["message_id"] for action, params in env.bot.writes if action == "delete_msg"] == ["1", "2"]


async def test_new_text_quote_and_author_continuation_bypass_verdict_cache(cached_env):
    env = cached_env
    await env.speak(segments=image_segments())
    origin = await env.process()
    await env.speak(segments=image_segments("New publication."))
    attached = await env.process()
    assert attached["data"]["audit_mode"] == "contextual"
    await env.speak(segments=image_segments(reply=origin["data"]["message"]["mid"]))
    quoted = await env.process()
    assert quoted["data"]["audit_mode"] == "contextual"
    await env.speak("法沦功")
    await env.process()
    await env.speak(segments=image_segments())
    continuation = await env.process()
    assert continuation["data"]["audit_mode"] == "contextual"
    assert len(env.model.requests) == 5


async def test_cache_expires_and_human_judgment_invalidates_source(cached_env):
    env = cached_env
    await env.speak(segments=image_segments())
    first = await env.process()
    await env.s.mark(first["id"], "误判", ADMIN)
    await env.speak(segments=image_segments())
    second = await env.process()
    assert len(env.model.requests) == 2 and not second["data"]["cache_source"]
    env.clock.now += 24 * 3600 + 1
    await env.speak(segments=image_segments())
    third = await env.process()
    assert len(env.model.requests) == 3 and not third["data"]["cache_source"]


async def test_marking_origin_cancels_cached_punishments_and_pending_notices(cached_env):
    from qq_message_guard.config import Deferred

    env = cached_env
    env.model.score = 0.97
    env.configure(auto_recall=True, notify_group=True, admin_group="123456")
    await env.speak(segments=image_segments())
    first = await env.process()
    await env.speak(segments=image_segments())
    second = await env.process()
    assert second["data"]["recall_ready"] and second["data"]["notification_ready"]
    await env.s.mark(first["id"], "误判", ADMIN)
    with pytest.raises(Deferred):
        await env.s.recall(second)
    await env.s.send_alerts()
    assert not env.bot.writes
    invalidated = await env.store.call("case", second["id"])
    assert invalidated["data"]["cache_invalidated"] and not invalidated["data"]["notification_ready"]


async def test_cache_scope_and_category_changes_require_fresh_classification(cached_env):
    env = cached_env
    await env.speak(segments=image_segments())
    await env.process()
    env.box.settings = replace(env.box.settings, checks=(Check("色情"),))
    await env.speak(segments=image_segments())
    assert not (await env.process())["data"]["cache_source"]
    env.box.settings = replace(
        env.box.settings,
        groups=env.box.settings.groups + (replace(env.box.settings.groups[0], group_id="100003"),),
    )
    await env.speak(segments=image_segments(), group_id="100003")
    assert not (await env.process())["data"]["cache_source"]
    assert len(env.model.requests) == 3
    env.box.settings = replace(env.box.settings, cache=replace(env.box.settings.cache, hours=1))
    await env.speak(segments=image_segments(), group_id="100003")
    assert not (await env.process())["data"]["cache_source"]
    assert len(env.model.requests) == 4


async def test_partial_ocr_results_are_not_cached_as_complete_verdicts(cached_env):
    env = cached_env
    env.ocr.failure = True
    for _ in range(2):
        await env.speak(segments=image_segments())
        case = await env.process()
        assert case["state"] == "incomplete" and not case["data"]["cache_source"]
    assert len(env.model.requests) == 2


async def test_streaming_gif_header_skips_without_reading_body_or_decoding():
    consumed = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for value in b"GIF89a" + b"body that should not be downloaded":
                consumed.append(value)
                yield bytes([value])

    media = Media(httpx.MockTransport(lambda _: httpx.Response(200, stream=Stream())))
    try:
        with pytest.raises(SkippedImage):
            await media.load("https://gchat.qpic.cn/a", Advanced())
        assert bytes(consumed) == b"GIF89a"
    finally:
        await media.close()


async def test_oversized_gif_is_skipped_rather_than_reported_as_download_failure():
    content = b"GIF89a" + b"x" * (9 * 1024 * 1024)
    media = Media(httpx.MockTransport(lambda _: httpx.Response(200, content=content)))
    try:
        with pytest.raises(SkippedImage):
            await media.load("https://gchat.qpic.cn/a", Advanced())
    finally:
        await media.close()


async def test_same_image_bytes_reuse_decode_but_different_settings_do_not():
    buffer = io.BytesIO()
    Image.new("RGB", (600, 300), "white").save(buffer, format="JPEG")
    media = Media(httpx.MockTransport(lambda _: httpx.Response(200, content=buffer.getvalue())))
    try:
        first = await media.load("https://gchat.qpic.cn/a", Advanced())
        assert not media.last_cache_hit
        second = await media.load("https://gchat.qpic.cn/b", Advanced())
        assert media.last_cache_hit and second == first
        await media.load("https://gchat.qpic.cn/a", replace(Advanced(), image_side=256))
        assert not media.last_cache_hit
    finally:
        await media.close()


def test_large_phone_jpeg_is_downsampled_before_exif_copy():
    buffer = io.BytesIO()
    with Image.new("RGB", (4800, 4000), "white") as image:
        image.save(buffer, format="JPEG")
    encoded = compress_image(buffer.getvalue(), 256)
    with Image.open(io.BytesIO(base64.b64decode(encoded.split(",", 1)[1]))) as result:
        assert max(result.size) == 256
