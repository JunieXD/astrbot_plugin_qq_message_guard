"""Real local Qwen inference with isolated state and a recording QQ substitute."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds
        await asyncio.sleep(0)


class RecordingBot:
    def __init__(self, clock):
        self.clock = clock
        self._wsr_api_clients = {"100001": object()}
        self.messages, self.mutes, self.writes = {}, {}, []

    async def call_action(self, action, self_id, **params):
        assert self_id == "100001"
        if action == "get_login_info":
            return {"user_id": self_id}
        if action == "get_status":
            return {"online": True, "good": True}
        if action == "get_group_member_info":
            return {
                "group_id": params["group_id"],
                "user_id": params["user_id"],
                "role": "admin" if params["user_id"] == self_id else "member",
                "shut_up_timestamp": self.mutes.get(params["user_id"], 0),
            }
        if action == "get_msg":
            return self.messages[str(params["message_id"])]
        assert action in ("delete_msg", "set_group_ban", "send_group_msg")
        self.writes.append({"action": action, **copy.deepcopy(params)})
        if action == "set_group_ban":
            self.mutes[params["user_id"]] = self.clock() + params["duration"]
        return {"message_id": 90000 + len(self.writes)} if action == "send_group_msg" else None


class Journal:
    def check(self):
        pass

    def record(self, *args, **kwargs):
        pass


async def main(args):
    sys.path.insert(0, str(args.source))
    from qq_message_guard.client import Media
    from qq_message_guard.config import Advanced, Deferred, MutePolicy, Policy, Settings
    from qq_message_guard.muting import EscalatingMute, member_key
    from qq_message_guard.platform import Platform
    from qq_message_guard.qwen import QwenGuard
    from qq_message_guard.service import Service
    from qq_message_guard.store import Store

    args.output.mkdir(parents=True, exist_ok=False)
    clock = Clock()
    bot = RecordingBot(clock)
    settings = Settings(
        enabled=True,
        backend="qwen3guard",
        base_url=args.url,
        model="Qwen3Guard-Gen-4B-Q4_K_M",
        images_enabled=False,
        ocr_enabled=False,
        groups=(
            Policy(
                "100002",
                auto_recall=True,
                warn_member=True,
                notify_group=True,
                admin_group="100005",
                mute=MutePolicy(enabled=True),
            ),
        ),
        advanced=Advanced(notify_cooldown_minutes=0),
    )
    journal, store = Journal(), Store(args.output / "state.sqlite3")
    await store.open()
    model, media = QwenGuard(), Media()
    inst = SimpleNamespace(bot=bot, meta=lambda: SimpleNamespace(id="isolated", name="aiocqhttp"))
    context = SimpleNamespace(platform_manager=SimpleNamespace(get_insts=lambda: [inst]))
    platform = Platform(context, store, lambda: settings, journal, clock=clock, sleep=clock.sleep)
    service = Service(
        lambda: settings, store, model, media, platform, journal, clock=clock, sleep=clock.sleep
    )
    results = []
    try:
        for i in range(6):
            if i >= 4:
                history = await store.call("get", member_key("isolated:100001:100002", "100003"))
                clock.now = history["until"] + 1
                # Prove persisted progression is enough after a plugin restart.
                await store.close()
                await store.open()
                service.mutes = EscalatingMute(service)
            raw = {
                "post_type": "message",
                "message_type": "group",
                "self_id": "100001",
                "group_id": "100002",
                "user_id": "100003",
                "message_id": i + 1,
                "time": int(clock()),
                "sender": {"user_id": "100003", "role": "member"},
                "message": [{"type": "text", "data": {"text": "明早我带刀去你公司门口，把你砍死。"}}],
            }
            bot.messages[str(i + 1)] = copy.deepcopy(raw)
            await service.observe(raw, "isolated")
            case = await store.call("next_case", {})
            assert case is not None
            await service.process(case)
            case = await store.call("case", case["id"])
            assert case["data"]["classification"]["safety"] == "Unsafe"
            await service.recall(case)
            case = await store.call("case", case["id"])
            try:
                await service.warn(case)
            except Deferred:
                pass
            await store.call("patch", case["id"], {"actions_pending": False})
            await service.send_alerts()
            case = await store.call("case", case["id"])
            results.append(
                {
                    "case": case["id"],
                    "state": case["state"],
                    "mute_status": case["data"].get("mute_status"),
                    "mute_seconds": case["data"].get("mute_seconds"),
                    "usage": case["data"]["usage"],
                }
            )
            clock.now += 61
        durations = [w["duration"] for w in bot.writes if w["action"] == "set_group_ban"]
        assert durations == [3600, 7200, 14400], durations
        assert sum(w["action"] == "delete_msg" for w in bot.writes) == 6
        assert (
            len([w for w in bot.writes if w["action"] == "send_group_msg" and w["group_id"] == "100002"]) == 3
        )
        stats = await store.call("stats", clock())
        # The clock jumps through hours; count all six physical calls directly.
        calls = await store.call("_row", "SELECT count(*),sum(ok) FROM calls")
        assert tuple(calls) == (6, 6)
        summary = {
            "real_model_calls": 6,
            "mute_durations_seconds": durations,
            "qq_api_real": False,
            "results": results,
            "recorded_writes": bot.writes,
            "last_day_stats": stats["calls"],
        }
        (args.output / "results.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "real_model_calls": 6,
                    "mute_durations_seconds": durations,
                    "qq_api_real": False,
                    "persisted_escalation_verified": True,
                }
            ),
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
