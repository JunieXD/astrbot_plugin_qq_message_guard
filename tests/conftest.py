from __future__ import annotations

import asyncio
import copy
import io
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PIL import Image

from qq_message_guard.client import Result, compress_image
from qq_message_guard.config import Advanced, Cache, Check, Evidence, Policy, Settings
from qq_message_guard.ocr import OcrResult
from qq_message_guard.platform import Platform
from qq_message_guard.service import Service
from qq_message_guard.store import Store

A, G, U, ADMIN = "100001", "100002", "200001", "300001"


class Clock:
    now = 1_800_000_000.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds
        await asyncio.sleep(0)


class Journal:
    failure = False

    def __init__(self):
        self.records = []

    def record(self, event, **fields):
        self.records.append({"event": event, **fields})

    def check(self):
        if self.failure:
            from qq_message_guard.config import GuardError

            raise GuardError("日志损坏")

    def maintain(self, *args):
        self.check()


class Bot:
    def __init__(self):
        self._wsr_api_clients = {A: object()}
        self.messages = {}
        self.reads, self.writes = [], []
        self.online, self.role, self.target_role = True, "admin", "member"
        self.login, self.fail, self.after, self.before = A, "", None, None

    async def call_action(self, action, self_id, **params):
        assert self_id == A
        if self.before:
            await self.before(action)
        if action in ("delete_msg", "send_group_msg", "send_private_msg"):
            self.writes.append((action, copy.deepcopy(params)))
            if self.after:
                await self.after(action)
            if self.fail == action:
                raise asyncio.TimeoutError()
            if self.fail == "business:" + action:
                return {"status": "failed", "retcode": 123}
            return None if action == "delete_msg" else {"message_id": 90000 + len(self.writes)}
        self.reads.append((action, copy.deepcopy(params)))
        if action == "get_login_info":
            return {"user_id": self.login}
        if action == "get_status":
            return {"online": self.online, "good": True}
        if action == "get_group_member_info":
            return {
                "user_id": params["user_id"],
                "group_id": params["group_id"],
                "role": self.role if params["user_id"] == A else self.target_role,
            }
        if action == "get_msg":
            return self.messages.get(str(params["message_id"]))
        raise AssertionError(action)


class Model:
    def __init__(self):
        self.score, self.failure, self.hook = 0.97, False, None
        self.requests = []

    async def evaluate(self, state, settings, media=()):
        self.requests.append((copy.deepcopy(state), settings, tuple(media)))
        if self.hook:
            await self.hook()
        if self.failure:
            from qq_message_guard.config import GuardError

            raise GuardError("模拟模型超时")
        scores = {check.key: self.score for check in settings.checks}
        raw = {
            "model": "fake-4b",
            "answers": {key: {"type": "noul", "noul": value} for key, value in scores.items()},
            "usage": {"input_tokens": 123, "output_tokens": 1},
            "qev": {"backend": "llama.cpp", "prompt_version": "qev-labels-v2"},
        }
        return Result("fake-4b", scores, 123, 1, 8.5, raw)


class Media:
    failure = False

    async def load(self, url, settings):
        if self.failure:
            from qq_message_guard.config import GuardError

            raise GuardError("图片下载失败")
        buf = io.BytesIO()
        Image.new("RGB", (20, 20), "white").save(buf, format="PNG")
        return compress_image(buf.getvalue(), settings.image_side)


class Ocr:
    text = ""
    failure = False
    truncated = False

    async def recognize(self, path, settings):
        if self.failure:
            from qq_message_guard.config import GuardError

            raise GuardError("模拟OCR失败")
        return OcrResult(self.text, "fake-local-ocr", 3.0, self.truncated)


@pytest.fixture
async def env(tmp_path):
    clock, bot, journal, model, media = Clock(), Bot(), Journal(), Model(), Media()
    ocr = Ocr()
    policy = Policy(G)
    box = SimpleNamespace(
        settings=Settings(
            True,
            (policy,),
            checks=(Check(),),
            evidence=Evidence(review_images=False),
            cache=Cache(enabled=False),
            advanced=Advanced(
                action_min_seconds=0, action_max_seconds=0, account_gap_min=0, account_gap_max=0
            ),
        )
    )
    store = Store(tmp_path / "state.sqlite3")
    await store.open()
    inst = SimpleNamespace(bot=bot, meta=lambda: SimpleNamespace(id="qq", name="aiocqhttp"))
    context = SimpleNamespace(platform_manager=SimpleNamespace(get_insts=lambda: [inst]))
    platform = Platform(context, store, lambda: box.settings, journal, clock=clock, sleep=clock.sleep)
    service = Service(
        lambda: box.settings, store, model, media, platform, journal, clock=clock, sleep=clock.sleep, ocr=ocr
    )
    counter = 0

    async def speak(text="普通群聊消息", segments=None, **raw_fields):
        nonlocal counter
        counter += 1
        raw = dict(
            post_type="message",
            message_type="group",
            self_id=A,
            group_id=G,
            user_id=U,
            message_id=counter,
            time=int(clock()),
            sender={"user_id": U, "role": "member"},
            message=segments if segments is not None else [{"type": "text", "data": {"text": text}}],
        )
        raw.update(raw_fields)
        bot.messages[str(raw["message_id"])] = copy.deepcopy(raw)
        await service.observe(raw, "qq")
        cases = await store.call("recent")
        return raw, cases[0] if cases else None

    async def process():
        case = await store.call("next_case", {})
        if case:
            await service.process(case)
            return await store.call("case", case["id"])

    def configure(**changes):
        box.settings = replace(box.settings, groups=(replace(box.settings.groups[0], **changes),))

    yield SimpleNamespace(
        s=service,
        store=store,
        bot=bot,
        journal=journal,
        model=model,
        media=media,
        ocr=ocr,
        clock=clock,
        box=box,
        context=context,
        platform=platform,
        speak=speak,
        process=process,
        configure=configure,
        path=tmp_path,
    )
    await service.stop()
    await store.close()
