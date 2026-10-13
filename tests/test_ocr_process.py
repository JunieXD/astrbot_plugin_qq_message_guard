import asyncio
import json
import os
import sys
from dataclasses import replace

import pytest

from qq_message_guard.config import GuardError, Settings
from qq_message_guard.ocr import Ocr


async def test_local_ocr_process_records_text_and_truncation(monkeypatch, tmp_path):
    text = "正常中文图片" * 100
    output = json.dumps({"text": text}, ensure_ascii=False) if os.name == "nt" else text
    command = [sys.executable, "-X", "utf8", "-c", f"print({output!r})"]
    monkeypatch.setattr(Ocr, "command", staticmethod(lambda _: (command, "test-local")))
    settings = replace(Settings(), advanced=replace(Settings().advanced, max_text_chars=200))
    result = await Ocr().recognize(tmp_path / "image with spaces.jpg", settings)
    assert result.text == text[:200] and result.truncated and result.backend == "test-local"


async def test_local_ocr_business_failure_is_not_empty_success(monkeypatch, tmp_path):
    command = [sys.executable, "-c", "raise SystemExit(1)"]
    monkeypatch.setattr(Ocr, "command", staticmethod(lambda _: (command, "test-local")))
    with pytest.raises(GuardError, match="未完成"):
        await Ocr().recognize(tmp_path / "image.jpg", Settings())


@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_or_reload_kills_and_reaps_only_our_ocr_process(monkeypatch, tmp_path, cancel):
    command = [sys.executable, "-c", "import time; time.sleep(60)"]
    monkeypatch.setattr(Ocr, "command", staticmethod(lambda _: (command, "test-local")))
    original = asyncio.create_subprocess_exec
    children = []
    started = asyncio.Event()

    async def create(*args, **kwargs):
        process = await original(*args, **kwargs)
        children.append(process)
        started.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    settings = replace(Settings(), ocr_timeout=0.05)
    task = asyncio.create_task(Ocr().recognize(tmp_path / "image.jpg", settings))
    await asyncio.wait_for(started.wait(), 2)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(GuardError, match="超时"):
            await task
    assert len(children) == 1 and children[0].returncode is not None
