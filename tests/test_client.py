import io
import json
from dataclasses import replace

import httpx
import pytest
from PIL import Image

from qq_message_guard.client import Media, OneJev, compress_image, image_url
from qq_message_guard.config import Advanced, Check, GuardError, Settings


def response():
    return {
        "model": "OneJev-4B.Q4_K_M",
        "answers": {
            "rules": {"type": "noul", "noul": 0.8},
            "intent": {
                "type": "choice",
                "choice": "participating",
                "probabilities": {"participating": 0.9, "report_or_stop": 0.05, "normal": 0.05},
                "confidence": 0.85,
            },
        },
        "usage": {"input_tokens": 100, "output_tokens": 1},
        "qev": {"backend": "llama.cpp"},
    }


async def test_native_api_single_request_and_auth():
    calls = []

    def handler(request):
        calls.append(request)
        body = json.loads(request.content)
        assert request.url.path == "/v1/systemone" and request.url.params["debias"] == "1"
        assert request.headers["authorization"] == "Bearer secret"
        assert body["model"] == "jev-latest"
        assert body["questions"]["rules"]["type"] == "noul"
        assert "intent" not in body["questions"]
        assert "media" not in body
        return httpx.Response(200, json=response())

    model = OneJev(httpx.MockTransport(handler))
    try:
        result = await model.evaluate({"消息": "test"}, Settings(api_key="secret", checks=(Check(),)))
        assert result.scores == {"rules": 0.8} and result.input_tokens == 100
        assert result.raw_response["qev"]["backend"] == "llama.cpp" and len(calls) == 1
    finally:
        await model.close()


@pytest.mark.parametrize("bad", [None, True, -0.1, 1.1, "0.9", float("nan")])
async def test_bad_scores_fail_open_without_safe_verdict(bad):
    value = response()
    value["answers"]["rules"]["noul"] = bad
    model = OneJev(httpx.MockTransport(lambda _: httpx.Response(200, content=json.dumps(value))))
    try:
        with pytest.raises(GuardError):
            await model.evaluate("test", Settings(checks=(Check(),)))
    finally:
        await model.close()


@pytest.mark.parametrize("mutation", ["missing", "type", "model", "tokens", "usage"])
async def test_incomplete_response(mutation):
    value = response()
    if mutation == "missing":
        value["answers"] = {}
    elif mutation == "type":
        value["answers"]["rules"]["type"] = "choice"
    elif mutation == "model":
        value["model"] = ""
    elif mutation == "tokens":
        value["usage"]["input_tokens"] = True
    else:
        value.pop("usage")
    model = OneJev(httpx.MockTransport(lambda _: httpx.Response(200, json=value)))
    try:
        with pytest.raises(GuardError):
            await model.evaluate("test", Settings(checks=(Check(),)))
    finally:
        await model.close()


async def test_all_questions_required():
    model = OneJev(httpx.MockTransport(lambda _: httpx.Response(200, json=response())))
    try:
        with pytest.raises(GuardError):
            await model.evaluate("test", Settings(checks=(Check(), Check("色情"))))
    finally:
        await model.close()


async def test_legacy_intent_answers_do_not_gate_category_only_requests():
    value = response()
    value["answers"]["intent"] = {"choice": "report_or_stop", "confidence": -1}
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=value)

    model = OneJev(httpx.MockTransport(handler))
    try:
        result = await model.evaluate("test", Settings(checks=(Check(),)))
        assert result.scores == {"rules": 0.8} and "intent" not in requests[0]["questions"]
    finally:
        await model.close()


@pytest.mark.parametrize("status", [301, 422, 500])
async def test_http_failure_not_retried(status):
    count = []

    def handler(request):
        count.append(1)
        return httpx.Response(status, text="secret must not reach error")

    model = OneJev(httpx.MockTransport(handler))
    try:
        with pytest.raises(GuardError) as error:
            await model.evaluate("test", Settings(checks=(Check(),)))
        assert "secret" not in str(error.value) and count == [1]
    finally:
        await model.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://gchat.qpic.cn/a",
        "https://qpic.cn.evil.com/a",
        "https://127.0.0.1/a",
        "https://gchat.qpic.cn:8000/a",
        "file:///c:/secret",
        "https://user:p@gchat.qpic.cn/a",
    ],
)
def test_image_url_boundary(url):
    with pytest.raises(GuardError):
        image_url(url)


def test_jpeg_compression_and_bad_images():
    buf = io.BytesIO()
    Image.new("RGBA", (400, 200), (0, 0, 0, 0)).save(buf, format="PNG")
    result = compress_image(buf.getvalue(), 256)
    assert result.startswith("data:image/jpeg;base64,")
    with pytest.raises(GuardError):
        compress_image(b"not an image", 256)


async def test_media_limit_and_no_redirect():
    image = Media(
        httpx.MockTransport(lambda _: httpx.Response(302, headers={"location": "http://127.0.0.1"}))
    )
    try:
        with pytest.raises(GuardError):
            await image.load("https://gchat.qpic.cn/a", Advanced())
    finally:
        await image.close()
    image = Media(httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * (1024 * 1024 + 1))))
    try:
        with pytest.raises(GuardError, match="超过"):
            await image.load("https://gchat.qpic.cn/a", replace(Advanced(), image_mb=1))
    finally:
        await image.close()
