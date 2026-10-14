"""AstrBot entry point. Model calls and QQ writes run outside message callbacks."""

from __future__ import annotations

import asyncio

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, StarTools, register

from .qq_message_guard.client import Media, OneJev
from .qq_message_guard.commands import Commands
from .qq_message_guard.config import GuardError, parse_settings
from .qq_message_guard.platform import Platform
from .qq_message_guard.resources import InstanceLock, Journal
from .qq_message_guard.service import Service
from .qq_message_guard.store import Store


@register("astrbot_plugin_qq_message_guard", "JunieXD", "本地OneJev群消息审核与复核撤回", "0.5.2")
class QQMessageGuard(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context=context, config=config)
        self.raw_config = config if config is not None else {}
        self.service = self.store = self.journal = self.lock = self.model = self.media = None
        self.start_error = "插件尚未初始化。"

    def settings(self):
        return parse_settings(dict(self.raw_config))

    async def initialize(self):
        try:
            settings = self.settings()
            root = StarTools.get_data_dir("astrbot_plugin_qq_message_guard")
            root.mkdir(parents=True, exist_ok=True)
            self.lock = InstanceLock(root / "instance.lock")
            self.journal = Journal(root, settings.advanced)
            self.store = Store(root / "state.sqlite3")
            await self.store.open()
            self.model = OneJev()
            self.media = Media()
            platform = Platform(self.context, self.store, self.settings, self.journal)
            self.service = Service(self.settings, self.store, self.model, self.media, platform, self.journal)
            await self.service.start()
            self.start_error = ""
            logger.info(
                "QQ 群消息审核 v0.5.2 已加载；总开关：%s；群数：%s；审核项：%s；批量初筛：%s。",
                settings.enabled,
                len(settings.groups),
                len(settings.checks),
                settings.batch.enabled,
            )
        except BaseException as exc:
            self.start_error = (
                str(exc) if isinstance(exc, GuardError) else "初始化失败，请检查日志或数据目录。"
            )
            logger.error("QQ 群消息审核：%s；异常类型：%s", self.start_error, type(exc).__name__)
            await self.terminate()
            if not isinstance(exc, Exception):
                raise

    async def terminate(self):
        try:
            if self.service:
                await self.service.stop()
        finally:
            try:
                results = await asyncio.gather(
                    *(resource.close() for resource in (self.media, self.model, self.store) if resource),
                    return_exceptions=True,
                )
                for result in results:
                    if isinstance(result, BaseException):
                        logger.error("QQ消息审核资源关闭失败：%s", type(result).__name__)
            finally:
                if self.journal:
                    self.journal.close()
                if self.lock:
                    self.lock.close()
                self.service = self.store = self.journal = self.lock = self.model = self.media = None

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def observe(self, event):
        service = self.service
        if service is None:
            return
        raw = getattr(event.message_obj, "raw_message", None)
        if raw is None:
            return
        task = asyncio.current_task()
        service.jobs.add(task)
        try:
            await service.observe(raw, str(event.platform_meta.id))
        except GuardError as exc:
            service.journal.record("消息接收未完成", reason=str(exc))
        except Exception as exc:
            service.failure = "事件保存异常，自动操作已停止，请检查日志并重载。"
            service.journal.record("消息接收异常", exception=exc)
        finally:
            service.jobs.discard(task)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.command("消息审核", alias={"qmod"})
    async def command(self, event):
        event.stop_event()
        if not event.is_admin():
            return
        service = self.service
        if service is None:
            await asyncio.wait_for(event.send(event.plain_result(self.start_error or "插件正在停止。")), 15)
            return
        task = asyncio.current_task()
        service.jobs.add(task)
        try:
            raw = getattr(event.message_obj, "raw_message", None)
            account = str(raw.get("self_id", "")) if raw is not None else ""
            result = await Commands(service).run(
                event.get_message_str(), str(event.get_sender_id()), str(event.platform_meta.id), account
            )
        except GuardError as exc:
            result = str(exc)
        except Exception as exc:
            service.journal.record("命令异常", exception=exc)
            result = "命令未完成，请检查插件状态和日志。"
        finally:
            service.jobs.discard(task)
        # AstrBot stops its response pipeline after stop_event(), including yielded results.
        await asyncio.wait_for(event.send(event.plain_result(result)), 15)
