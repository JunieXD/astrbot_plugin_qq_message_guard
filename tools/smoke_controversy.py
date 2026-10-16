"""Real local Qwen inference; all QQ actions use an isolated recording substitute."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from smoke_mutes import Clock, Journal, RecordingBot


class Bot(RecordingBot):
    async def call_action(self, action, self_id, **params):
        result = await super().call_action(action, self_id, **params)
        if action == "send_group_msg":
            self.messages[str(result["message_id"])] = {
                "message_id": result["message_id"],
                "group_id": params["group_id"],
                "sender": {"user_id": self_id},
                "message": copy.deepcopy(params["message"]),
            }
        return result


async def main(args):
    sys.path.insert(0, str(args.source))
    from qq_message_guard.client import Media
    from qq_message_guard.config import Advanced, Controversy, Policy, Settings
    from qq_message_guard.platform import Platform
    from qq_message_guard.qwen import QwenGuard
    from qq_message_guard.service import Service
    from qq_message_guard.store import Store

    args.output.mkdir(parents=True, exist_ok=False)
    clock, journal = Clock(), Journal()
    bot = Bot(clock)
    settings = Settings(
        enabled=True,
        backend="qwen3guard",
        base_url=args.url,
        model="Qwen3Guard-Gen-4B-Q4_K_M",
        images_enabled=False,
        ocr_enabled=False,
        groups=(Policy("100002", auto_recall=True, warn_member=True, controversy=Controversy(enabled=True)),),
        advanced=Advanced(),
    )
    store = Store(args.output / "state.sqlite3")
    await store.open()
    model, media = QwenGuard(), Media()
    inst = SimpleNamespace(bot=bot, meta=lambda: SimpleNamespace(id="isolated", name="aiocqhttp"))
    context = SimpleNamespace(platform_manager=SimpleNamespace(get_insts=lambda: [inst]))
    platform = Platform(context, store, lambda: settings, journal, clock=clock, sleep=clock.sleep)
    service = Service(
        lambda: settings, store, model, media, platform, journal, clock=clock, sleep=clock.sleep
    )
    results = []

    async def speak(index, text, user="100003"):
        raw = {
            "post_type": "message",
            "message_type": "group",
            "self_id": "100001",
            "group_id": "100002",
            "user_id": user,
            "message_id": index,
            "time": int(clock()),
            "sender": {"user_id": user, "role": "member"},
            "message": [{"type": "text", "data": {"text": text}}],
        }
        bot.messages[str(index)] = copy.deepcopy(raw)
        await service.observe(raw, "isolated")
        case = await store.call("next_case", {})
        await service.process(case)
        return await store.call("case", case["id"])

    async def self_recall_and_clean(case):
        await service.observe(
            {
                "post_type": "notice",
                "notice_type": "group_recall",
                "self_id": "100001",
                "group_id": "100002",
                "user_id": "100003",
                "operator_id": "100003",
                "message_id": case["data"]["message"]["mid"],
                "time": int(clock()),
            },
            "isolated",
        )
        warning = await store.call("pending_controversy_cleanup", clock())
        assert warning is not None
        await service.controversies.cleanup(await store.call("case", case["id"]), warning)
        assert (await store.call("controversy_warning", case["id"]))["cleanup_state"] == "confirmed"

    try:
        samples = ["把电脑时区和节点时区同步", "弄了还是不行，日本节点", "@100004 我换个梯子节点就好了"]
        for i, text in enumerate(samples, 1):
            case = await speak(i, text)
            assert case["data"]["classification"]["safety"] == "Controversial", case["data"]["classification"]
            assert case["data"]["controversy_ready"] and not case["data"]["recall_ready"]
            claimed = await store.call("next_recall")
            assert claimed["id"] == case["id"]
            await service.controversies.warn(claimed)
            await store.call("patch", case["id"], {"actions_pending": False})
            results.append(
                {
                    "case": case["id"],
                    "classification": case["data"]["classification"],
                    "usage": case["data"]["usage"],
                }
            )
            if i == 1:
                await self_recall_and_clean(case)
            elif i == 2:
                await store.close()
                await store.open()
            clock.now += 2
        third = await store.call("case", case["id"])
        assert third["data"]["controversy_mute_seconds"] == 600
        muted_until = bot.mutes["100003"]
        await self_recall_and_clean(third)
        assert bot.mutes["100003"] == muted_until > clock()
        assert [w["duration"] for w in bot.writes if w["action"] == "set_group_ban"] == [600]
        assert all(
            str(w["message_id"]) not in ("1", "2", "3") for w in bot.writes if w["action"] == "delete_msg"
        )

        unsafe = await speak(4, "明早我带刀去你公司门口，把你砍死。", "100004")
        assert unsafe["data"]["classification"]["safety"] == "Unsafe"
        assert unsafe["data"]["recall_ready"] and not unsafe["data"]["controversy_ready"]
        await service.recall(unsafe)
        assert any(w["action"] == "delete_msg" and w["message_id"] == "4" for w in bot.writes)
        calls = await store.call("_row", "SELECT count(*),sum(ok) FROM calls")
        assert tuple(calls) == (4, 4)
        summary = {
            "real_model_calls": 4,
            "qq_api_real": False,
            "reminders": 3,
            "controversy_mute_seconds": 600,
            "cleaned_reminders": 2,
            "original_controversial_messages_recalled": False,
            "mute_preserved_after_self_recall": True,
            "restart_preserved_count": True,
            "unsafe_recall_unchanged": True,
            "results": results,
            "recorded_writes": bot.writes,
        }
        (args.output / "results.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(
            json.dumps({k: v for k, v in summary.items() if k not in ("results", "recorded_writes")}),
            flush=True,
        )
    finally:
        await service.stop()
        await model.close()
        await media.close()
        await store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8001")
    asyncio.run(main(parser.parse_args()))
