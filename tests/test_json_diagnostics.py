import json

import httpx
import pytest

from qq_message_guard.client import OneJev
from qq_message_guard.config import GuardError, Settings


async def test_nonfinite_diagnostics_cannot_poison_sqlite_json(env):
    value = {
        "model": "OneJev-4B",
        "answers": {"rules": {"type": "noul", "noul": 0.98}},
        "usage": {"input_tokens": 100, "output_tokens": 1},
        "qev": {"diagnostic": float("nan")},
    }
    client = OneJev(httpx.MockTransport(lambda _: httpx.Response(200, content=json.dumps(value))))
    try:
        with pytest.raises(GuardError):
            await client.evaluate("test", Settings())
    finally:
        await client.close()
