"""Qwen3Guard native completion adapter using its frozen official user template."""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

import httpx

from .classification import QWEN_CATEGORIES
from .client import Result
from .config import GuardError, InputRejected

TEMPLATE = json.loads(Path(__file__).with_name("qwen_prompt.json").read_text(encoding="utf-8"))


def parse_classification(content):
    if not isinstance(content, str):
        raise GuardError("Qwen3Guard缺少分类文本，本次未完成审核。")
    match = re.fullmatch(
        r"\s*Safety:\s*(Safe|Unsafe|Controversial)\s*\r?\nCategories:\s*([^\r\n]+)\s*",
        content,
    )
    if not match:
        raise GuardError("Qwen3Guard分类格式无效，本次未完成审核。")
    label, text = match.groups()
    categories = tuple(c.strip().removesuffix(".") for c in text.strip().split(","))
    if categories == ("None",):
        categories = ()
    if (
        any(c not in QWEN_CATEGORIES for c in categories)
        or len(set(categories)) != len(categories)
        or (label == "Safe") != (not categories)
    ):
        raise GuardError("Qwen3Guard分类缺失、未知或互相矛盾，本次未完成审核。")
    return label, categories


class QwenGuard:
    def __init__(self, transport=None):
        self.http = httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False)
        self.lock = asyncio.Lock()

    async def close(self):
        await self.http.aclose()

    @staticmethod
    def request(state, settings, media=()):
        if not isinstance(state, dict) or not isinstance(state.get("current_text"), str):
            raise GuardError("Qwen3Guard只接受当前消息文字。")
        if not state["current_text"].strip():
            raise InputRejected("当前消息没有可审核文字。")
        # No system rules, neighboring messages, or hidden author history. The
        # anonymous prefix matches the tested format; special tokens are quoted.
        text = state["current_text"]
        for token in ("<|im_start|>", "<|im_end|>", "<|endoftext|>"):
            text = text.replace(token, token.replace("<", "〈").replace(">", "〉"))
        prompt = TEMPLATE["prefix"] + "[成员1] " + text + TEMPLATE["suffix"]
        return {
            "prompt": prompt,
            "temperature": 0,
            "seed": 20261016,
            "n_predict": 96,
            "cache_prompt": True,
            "stream": False,
            "stop": ["<|im_end|>", "\n\nUser:", "\n\nUSER:"],
        }

    async def evaluate(self, state, settings, media=()):
        body = self.request(state, settings)
        started = time.monotonic()
        headers = {"Authorization": "Bearer " + settings.api_key} if settings.api_key else {}
        async with self.lock:
            try:
                response = await self.http.post(
                    settings.base_url + "/completion",
                    json=body,
                    headers=headers,
                    timeout=settings.timeout_seconds,
                )
                if response.status_code in (400, 413, 422):
                    raise InputRejected(
                        f"Qwen3Guard拒绝当前输入（HTTP {response.status_code}），本条未完成审核。"
                    )
                if response.status_code != 200:
                    raise GuardError(f"Qwen3Guard返回HTTP {response.status_code}，本次未完成审核。")
                if len(response.content) > 1024 * 1024:
                    raise GuardError("Qwen3Guard响应过大，本次未完成审核。")
                value = response.json()
                json.dumps(value, allow_nan=False)
            except (httpx.HTTPError, ValueError) as exc:
                raise GuardError("Qwen3Guard请求失败或响应无效，本次未完成审核。") from exc
        if not isinstance(value, dict) or value.get("truncated") is not False:
            raise GuardError("Qwen3Guard未确认完整读取输入，本次未完成审核。")
        # llama.cpp b11561 uses stop_type; older builds expose stopped_limit.
        completed = value.get("stop_type") in ("eos", "word") or (
            "stop_type" not in value and value.get("stopped_limit") is False
        )
        if not completed or value.get("stopped_limit") is True:
            raise GuardError("Qwen3Guard分类输出未完整结束，本次未完成审核。")
        label, categories = parse_classification(value.get("content"))
        actual = value.get("model")
        if actual != settings.model:
            raise GuardError("Qwen3Guard实际模型与配置不符，本次未完成审核。")
        counts = [value.get(k) for k in ("tokens_evaluated", "tokens_predicted")]
        if any(type(n) is not int or not 0 <= n < 10**9 for n in counts):
            raise GuardError("Qwen3Guard缺少有效token统计，本次未完成审核。")
        return Result(
            actual,
            {},
            *counts,
            round((time.monotonic() - started) * 1000, 2),
            value,
            safety=label,
            categories=categories,
        )
