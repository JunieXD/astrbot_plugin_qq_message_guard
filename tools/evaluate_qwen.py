"""Exercise the real model through Service, using an isolated DB and no QQ API.

Run with AstrBot's native Python on Windows. The dataset must contain cases with
id, split, current.text, expected_flag and source, as in the frozen comparison.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace


class Journal:
    def record(self, *args, **kwargs):
        pass

    def check(self):
        pass


async def main(args):
    sys.path.insert(0, str(args.source))
    from qq_message_guard.client import Media
    from qq_message_guard.config import Policy, Settings
    from qq_message_guard.qwen import QwenGuard
    from qq_message_guard.service import Service
    from qq_message_guard.store import Store

    cases = [c for c in json.loads(args.dataset.read_text(encoding="utf-8"))["cases"] if c["split"] == "test"]
    if args.limit:
        cases = cases[: args.limit]
    args.output.mkdir(parents=True, exist_ok=False)
    cfg = Settings(
        enabled=True,
        groups=(
            Policy("100002", auto_recall=True, warn_member=True, notify_group=True, admin_group="100005"),
        ),
        backend="qwen3guard",
        base_url="http://127.0.0.1:8001",
        model="Qwen3Guard-Gen-4B-Q4_K_M",
    )
    store = Store(args.output / "state.sqlite3")
    await store.open()
    model, media = QwenGuard(), Media()
    # No real platform object is constructed, so no QQ read/write is possible.
    platform = SimpleNamespace(bot=lambda *a: (None, ("fake-online", 1)))
    service = Service(lambda: cfg, store, model, media, platform, Journal())
    rows = []
    try:
        for i, item in enumerate(cases, 1):
            text = item["current"]["text"]
            now = int(time.time())
            await service.observe(
                {
                    "post_type": "message",
                    "message_type": "group",
                    "self_id": 100001,
                    "group_id": 100002,
                    "user_id": 100003,
                    "message_id": i,
                    "time": now,
                    "sender": {"role": "member", "card": "测试成员"},
                    "message": [{"type": "text", "data": {"text": text}}],
                },
                "isolated-test",
            )
            case = await store.call("next_case", {})
            assert case is not None
            await service.process(case)
            case = await store.call("case", case["id"])
            data = case["data"]
            # Validate real service decisions, accounting, context selection,
            # and operation readiness. Never start the action/alert workers.
            assert data["context"] == [] and data.get("context_used") == 0
            assert data.get("audit_mode") == "current_only"
            assert case["state"] in ("review", "allowed"), (item["id"], data.get("reason"))
            assert data["scores"] == {}
            assert data["recall_ready"] == bool(case["flagged"])
            assert data["warn_ready"] == bool(case["flagged"])
            row = {
                "id": item["id"],
                "source": item["source"],
                "expected_flag": item["expected_flag"],
                "flagged": bool(case["flagged"]),
                "classification": data["classification"],
                "usage": data["usage"],
                "state": case["state"],
            }
            rows.append(row)
            with (args.output / "results.jsonl").open("a", encoding="utf-8") as out:
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
            if i % 10 == 0 or i == len(cases):
                print(
                    json.dumps({"done": i, "total": len(cases), "elapsed_ms": data["usage"]["elapsed_ms"]}),
                    flush=True,
                )
        stats = await store.call("stats", time.time())
        assert stats["calls"]["ok"] == len(cases) and stats["calls"]["n"] == len(cases)
        tp = sum(r["flagged"] and r["expected_flag"] for r in rows)
        fp = sum(r["flagged"] and not r["expected_flag"] for r in rows)
        fn = sum(not r["flagged"] and r["expected_flag"] for r in rows)
        tn = len(rows) - tp - fp - fn
        summary = {
            "model": cfg.model,
            "mode": "current_only",
            "n": len(rows),
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "tn": tn,
            "mean_seconds": sum(r["usage"]["elapsed_ms"] for r in rows) / max(1, len(rows)) / 1000,
            "calls": stats["calls"],
            "qq_api_constructed": False,
            "wrong_case_ids": [r["id"] for r in rows if r["flagged"] != r["expected_flag"]],
        }
        (args.output / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    finally:
        await service.stop()
        await model.close()
        await media.close()
        await store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    asyncio.run(main(parser.parse_args()))
