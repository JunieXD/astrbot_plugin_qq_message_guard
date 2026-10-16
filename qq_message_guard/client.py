from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import math
import time
import warnings
from collections import OrderedDict
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageOps

from .categories import CATEGORIES, CONTENT_SCOPE
from .config import QUESTIONS, GuardError, InputRejected, SkippedImage


@dataclass(frozen=True)
class Result:
    model: str
    scores: dict[str, float]
    input_tokens: int
    output_tokens: int
    elapsed_ms: float
    raw_response: dict = field(default_factory=dict)
    cache_source: int = 0
    safety: str = ""
    categories: tuple[str, ...] = ()


class OneJev:
    def __init__(self, transport=None):
        self.http = httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False)
        self.lock = asyncio.Lock()

    async def close(self):
        await self.http.aclose()

    @staticmethod
    def request(state, settings, media=()):
        questions = {}
        for check in settings.checks:
            category = CATEGORIES[check.name]
            instructions = (
                QUESTIONS[check.name][1]
                if check.key == "rules"
                else f"只判断当前消息是否属于{check.name}。" + CONTENT_SCOPE
            )
            questions[check.key] = {
                "type": "noul",
                "instructions": instructions,
                "criteria": {"true": category.positive, "false": category.negative},
            }
        if isinstance(state, dict):
            state = {**state, "审核范围": [check.name for check in settings.checks]}
            if all(check.key != "rules" for check in settings.checks):
                # Independent definitions live in their question, not in a vague
                # catch-all rule or a second copy of every definition in the prefix.
                state.pop("群规", None)
        body = {"model": settings.model, "state": state, "questions": questions}
        if media:
            body["media"] = [{"type": "image", "data": item} for item in media]
        return body

    async def evaluate(self, state, settings, media=()):
        return await self.evaluate_request(self.request(state, settings, media), settings)

    async def evaluate_request(self, body, settings):
        """All stages share one transport and strict validation of requested answers."""
        headers = {"Authorization": "Bearer " + settings.api_key} if settings.api_key else {}
        started = time.monotonic()
        async with self.lock:
            try:
                response = await self.http.post(
                    settings.base_url + "/v1/systemone",
                    params={"debias": 1},
                    json=body,
                    headers=headers,
                    timeout=settings.timeout_seconds,
                )
                if response.status_code in (413, 422):
                    raise InputRejected(
                        f"OneJev拒绝当前审核输入（HTTP {response.status_code}），本条未完成审核。"
                    )
                if response.status_code != 200:
                    raise GuardError(f"OneJev返回HTTP {response.status_code}，本次未完成审核。")
                if len(response.content) > 1024 * 1024:
                    raise GuardError("OneJev响应过大，本次未完成审核。")
                value = response.json()
                json.dumps(value, allow_nan=False)  # Keep all persisted diagnostic JSON valid for SQLite.
            except (httpx.HTTPError, ValueError) as exc:
                raise GuardError("OneJev请求失败或响应不是JSON，本次未完成审核。") from exc
        if not isinstance(value, dict) or not isinstance(value.get("answers"), dict):
            raise GuardError("OneJev响应缺少answers，本次未完成审核。")
        scores = {}
        for key, question in body["questions"].items():
            if question["type"] != "noul":
                continue
            answer = value["answers"].get(key)
            if not isinstance(answer, dict) or answer.get("type") != "noul":
                raise GuardError("OneJev缺少审核项或类型不正确，本次未完成审核。")
            score = answer.get("noul")
            if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
                raise GuardError("OneJev审核分数无效，本次未完成审核。")
            scores[key] = float(score)
        model, usage = value.get("model"), value.get("usage")
        if not isinstance(model, str) or not model or len(model) > 200 or not isinstance(usage, dict):
            raise GuardError("OneJev缺少实际模型或token统计，本次未完成审核。")
        counts = [usage.get(k) for k in ("input_tokens", "output_tokens")]
        if any(type(n) is not int or not 0 <= n < 10**9 for n in counts):
            raise GuardError("OneJev的token统计无效，本次未完成审核。")
        return Result(model, scores, *counts, round((time.monotonic() - started) * 1000, 2), value)


def image_url(url):
    """Only QQ media CDN URLs; never fetch arbitrary user-provided links/files."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        allowed = any(
            host == domain or host.endswith("." + domain) for domain in ("qpic.cn", "qq.com", "qq.com.cn")
        )
        if (
            parts.scheme != "https"
            or not allowed
            or parts.username
            or parts.password
            or parts.port not in (None, 443)
            or parts.fragment
        ):
            raise ValueError()
    except (ValueError, TypeError) as exc:
        raise GuardError("图片地址不是受支持的QQ媒体地址。") from exc
    return url


def compress_image(content, side):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                if image.format == "GIF" or getattr(image, "is_animated", False):
                    raise SkippedImage("GIF或其他动态图按规则跳过，不进行图片审核。")
                if image.width * image.height > 64_000_000:
                    raise GuardError("图片超过6400万像素，未进行图片审核。")
                # JPEG draft downsamples during decoding; thumbnail before EXIF
                # avoids copying a full-resolution 48MP phone photo.
                image.draft("RGB", (side, side))
                image.thumbnail((side, side))
                image = ImageOps.exif_transpose(image)
                if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
                    rgba = image.convert("RGBA")
                    rgb = Image.new("RGB", rgba.size, "white")
                    rgb.paste(rgba, mask=rgba.getchannel("A"))
                else:
                    rgb = image.convert("RGB")
                output = io.BytesIO()
                rgb.save(output, format="JPEG", quality=88)
    except (OSError, ValueError, Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise GuardError("图片无法安全解码，本次未完成图片审核。") from exc
    return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")


class Media:
    def __init__(self, transport=None):
        self.http = httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False)
        self.cache = OrderedDict()
        self.cache_bytes = 0
        self.cache_enabled = True
        self.cache_hours = 24
        self.last_cache_hit = False
        self.last_source_digest = ""

    async def close(self):
        self.cache.clear()
        self.cache_bytes = 0
        await self.http.aclose()

    async def load(self, url, settings):
        self.last_cache_hit = False
        self.last_source_digest = ""
        for key, (expires, cached) in list(self.cache.items()):
            if not self.cache_enabled or expires <= time.monotonic():
                self.cache.pop(key)
                self.cache_bytes -= len(cached)
        try:
            return await asyncio.wait_for(self._load(url, settings), settings.image_timeout)
        except asyncio.TimeoutError as exc:
            raise GuardError("图片下载或处理超时，本次未完成图片审核。") from exc

    async def _load(self, url, settings):
        limit = settings.image_mb * 1024 * 1024
        try:
            async with self.http.stream("GET", image_url(url), timeout=settings.image_timeout) as response:
                if response.status_code != 200:
                    raise GuardError(f"图片下载返回HTTP {response.status_code}。")
                pieces, size, prefix = [], 0, bytearray()
                async for block in response.aiter_bytes():
                    prefix.extend(block[: 6 - len(prefix)])
                    if bytes(prefix) in (b"GIF87a", b"GIF89a"):
                        raise SkippedImage("GIF按规则跳过，不进行图片审核。")
                    size += len(block)
                    if size > limit:
                        raise GuardError("图片文件超过配置大小，本次未完成图片审核。")
                    pieces.append(block)
            content = b"".join(pieces)
            self.last_source_digest = hashlib.sha256(content).hexdigest()
            key = (self.last_source_digest, settings.image_side)
            if self.cache_enabled and key in self.cache:
                self.last_cache_hit = True
                self.cache.move_to_end(key)
                return self.cache[key][1]
            image = await asyncio.to_thread(compress_image, content, settings.image_side)
            if self.cache_enabled:
                self.cache[key] = (time.monotonic() + self.cache_hours * 3600, image)
                self.cache_bytes += len(image)
                while len(self.cache) > 128 or self.cache_bytes > 32 * 1024 * 1024:
                    _, (_, old) = self.cache.popitem(last=False)
                    self.cache_bytes -= len(old)
            return image
        except httpx.HTTPError as exc:
            raise GuardError("图片下载失败，本次未完成图片审核。") from exc
