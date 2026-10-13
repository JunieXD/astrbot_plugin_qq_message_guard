import importlib
import logging
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def plugin_class(monkeypatch, tmp_path):
    # Model the public AstrBot API only; source compatibility is checked against
    # the installed AstrBot adapter and event classes documented in references.md.
    api, events, stars = (
        ModuleType("astrbot.api"),
        ModuleType("astrbot.api.event"),
        ModuleType("astrbot.api.star"),
    )

    def decorator(*args, **kwargs):
        return lambda function: function

    api.logger = logging.getLogger("astrbot-test")
    events.filter = SimpleNamespace(
        platform_adapter_type=decorator,
        event_message_type=decorator,
        command=decorator,
        PlatformAdapterType=SimpleNamespace(AIOCQHTTP=1),
        EventMessageType=SimpleNamespace(ALL=3, PRIVATE_MESSAGE=1),
    )

    class Star:
        def __init__(self, context, config=None):
            self.context = context

    stars.Context, stars.Star, stars.register = object, Star, decorator
    stars.StarTools = SimpleNamespace(get_data_dir=lambda _: tmp_path)
    monkeypatch.setitem(sys.modules, "astrbot", ModuleType("astrbot"))
    monkeypatch.setitem(sys.modules, "astrbot.api", api)
    monkeypatch.setitem(sys.modules, "astrbot.api.event", events)
    monkeypatch.setitem(sys.modules, "astrbot.api.star", stars)
    name = "test_astrbot_message_guard"
    package = ModuleType(name)
    package.__path__ = [str(Path(__file__).resolve().parents[1])]
    monkeypatch.setitem(sys.modules, name, package)
    module = importlib.import_module(name + ".main")
    yield module.QQMessageGuard
    for key in list(sys.modules):
        if key.startswith(name + "."):
            sys.modules.pop(key, None)


class Event:
    def __init__(self, admin, text):
        self.admin, self.text, self.stopped = admin, text, False
        self.message_obj = SimpleNamespace(raw_message={"self_id": "100001"})
        self.platform_meta = SimpleNamespace(id="qq")

    def is_admin(self):
        return self.admin

    def stop_event(self):
        self.stopped = True

    def get_message_str(self):
        return self.text

    def get_sender_id(self):
        return "300001"

    def plain_result(self, text):
        return text


async def test_initialize_command_permissions_and_clean_shutdown(plugin_class, env):
    plugin = plugin_class(env.context, {})
    await plugin.initialize()
    try:
        assert plugin.service is not None and plugin.start_error == ""
        denied = Event(False, "/消息审核 状态")
        assert [r async for r in plugin.command(denied)] == [] and denied.stopped
        allowed = Event(True, "/消息审核 状态")
        result = [r async for r in plugin.command(allowed)]
        assert len(result) == 1 and "总开关：关闭" in result[0]
    finally:
        await plugin.terminate()
    assert plugin.service is None and plugin.store is None


async def test_invalid_config_can_be_reloaded_without_leaked_lock(plugin_class, env):
    plugin = plugin_class(env.context, {"timeout_seconds": 1})
    await plugin.initialize()
    assert plugin.service is None and "超时" in plugin.start_error
    plugin.raw_config = {}
    await plugin.initialize()
    try:
        assert plugin.service is not None
    finally:
        await plugin.terminate()
