from __future__ import annotations

import asyncio
import random
import time

from .config import Deferred, GuardError, Uncertain
from .messages import message_id, signature


class ApiFailure(GuardError):
    def __init__(self, action, result):
        code = result.get("retcode") if isinstance(result, dict) else None
        code = code if type(code) is int else "未知"
        super().__init__(f"{action}返回业务失败（返回码：{code}）。")


class Platform:
    def __init__(self, context, store, settings, journal, *, clock=time.time, sleep=asyncio.sleep):
        self.context, self.store, self.settings, self.journal = context, store, settings, journal
        self.clock, self.sleep = clock, sleep
        self.locks = {}
        self.bindings = {}

    def bot(self, pid, account):
        instances = [p for p in self.context.platform_manager.get_insts() if p.meta().name == "aiocqhttp"]
        matches = [p for p in instances if str(p.meta().id) == pid]
        if len(matches) != 1 or not callable(getattr(getattr(matches[0], "bot", None), "call_action", None)):
            raise Deferred("原QQ接入不存在，暂缓操作。")
        advertised = []
        for p in instances:
            clients = getattr(p.bot, "_wsr_api_clients", None)
            if isinstance(clients, dict):
                advertised.extend(str(key) for key in clients)
        if advertised.count(account) > 1:
            raise Deferred("同一QQ连接多个接入，已停止重复管理。")
        bot = matches[0].bot
        clients = getattr(bot, "_wsr_api_clients", None)
        if isinstance(clients, dict):
            if len(clients) > 1 or (clients and str(next(iter(clients))) != account):
                raise Deferred("接入账号不唯一或已改变，暂缓操作。")
            if not clients and getattr(getattr(bot, "_api", None), "_http_api", None) is None:
                raise Deferred("NapCat未连接，暂缓操作。")
            token = tuple(sorted((str(key), id(ws)) for key, ws in clients.items()))
        else:
            token = None
        return bot, (id(bot), token)

    async def binding(self, pid, account):
        bot, stamp = self.bot(pid, account)
        key = pid + ":" + account
        previous = self.bindings.get(key)
        if previous is not None and previous != stamp:
            await self.store.call(
                "put", "recovery:" + key, self.clock() + self.settings().advanced.recovery_seconds
            )
        self.bindings[key] = stamp
        if self.clock() < await self.store.call("get", "recovery:" + key, 0):
            raise Deferred("连接恢复后的等待尚未结束。")
        return bot, stamp

    async def call(self, pid, account, action, **params):
        bot, stamp = self.bot(pid, account)
        key = pid + ":" + account
        if action not in ("get_login_info", "get_status"):
            await self.store.call("read", key, self.clock(), self.settings().advanced.reads_per_hour)
        try:
            result = await asyncio.wait_for(bot.call_action(action=action, self_id=account, **params), 15)
        except Exception as exc:
            result = getattr(exc, "result", None)
            if isinstance(result, dict) and result.get("status") == "failed":
                raise ApiFailure(action, result) from exc
            raise Deferred(f"{action}读取未完成，暂缓操作。") from exc
        if self.bot(pid, account)[1] != stamp:
            raise Deferred("查询期间连接变化，暂缓操作。")
        if isinstance(result, dict) and ("status" in result or "retcode" in result):
            if result.get("status") != "ok" or result.get("retcode", 0) != 0:
                raise ApiFailure(action, result)
            result = result.get("data")
        return result

    async def online(self, pid, account):
        login = await self.call(pid, account, "get_login_info")
        if not isinstance(login, dict) or str(login.get("user_id")) != account:
            raise Deferred("机器人实际登录账号不一致。")
        status = await self.call(pid, account, "get_status")
        return isinstance(status, dict) and status.get("online") is True and status.get("good", True) is True

    async def member(self, pid, account, group, user):
        member = await self.call(
            pid, account, "get_group_member_info", group_id=group, user_id=user, no_cache=True
        )
        if (
            not isinstance(member, dict)
            or str(member.get("user_id")) != user
            or str(member.get("group_id", group)) != group
            or member.get("role") not in ("member", "admin", "owner")
        ):
            raise Deferred("群成员身份或权限无法确认。")
        return member

    async def locate(self, message):
        found = await self.call(message["platform"], message["account"], "get_msg", message_id=message["mid"])
        if not isinstance(found, dict) or not isinstance(found.get("sender"), dict):
            raise Deferred("原消息无法定位，不进行撤回。")
        valid = (
            str(found.get("message_id")) == message["mid"]
            and str(found.get("group_id")) == message["group"]
            and str(found["sender"].get("user_id")) == message["user"]
            and found.get("time") == message["at"]
            and signature(found.get("message")) == message["content_hash"]
        )
        if not valid:
            raise Deferred("消息身份或内容不一致，不进行撤回。")

    def shared_guard(self):
        guard = getattr(self.context.platform_manager, "_qq_automation_guard_v1", None)
        if guard is not None and (
            not callable(getattr(guard, "run", None))
            or not isinstance(getattr(guard, "deferred_error", None), type)
            or not issubclass(guard.deferred_error, Exception)
        ):
            raise Deferred("共享QQ操作协调器不兼容。")
        return guard

    async def write(self, message, cid, kind, action, params, gate, prepare, *, cooldowns=()):
        pid, account = message["platform"], message["account"]
        key = pid + ":" + account
        lock = self.locks.setdefault(key, asyncio.Lock())
        guard = self.shared_guard()
        more = self.settings().advanced
        gap_range = (
            (more.recall_gap_min, more.recall_gap_max)
            if kind == "recall"
            else (more.account_gap_min, more.account_gap_max)
        )
        limit = more.recalls_per_hour if kind == "recall" else more.writes_per_hour
        async with lock:
            await self.binding(pid, account)
            if guard is None and not await self.online(pid, account):
                await self.store.call(
                    "put", "recovery:" + key, self.clock() + self.settings().advanced.recovery_seconds
                )
                raise Deferred("QQ离线，操作已暂缓。")

            async def perform():
                oid, submitted = None, False
                try:
                    gate()
                    bot, stamp = await self.binding(pid, account)
                    more = self.settings().advanced
                    if await self.store.call("get", "pause:" + key):
                        raise Deferred("账号操作已暂停，请核对后恢复。")
                    if kind in ("recall", "warn") and await self.store.call(
                        "get", "group-pause:" + message["scope"]
                    ):
                        raise Deferred("这个群的操作已暂停，请核对后恢复。")
                    gap_at = await self.store.call("get", "gap:" + key, 0)
                    await self.sleep(max(0, gap_at - self.clock()))
                    gate()
                    await self.store.call(
                        "can_write",
                        cid,
                        kind,
                        key,
                        self.clock(),
                        limit,
                        more.group_warnings_per_hour,
                    )
                    await prepare()
                    gate()
                    gap = random.uniform(*gap_range)
                    oid = await self.store.call(
                        "reserve",
                        cid,
                        kind,
                        key,
                        self.clock(),
                        limit,
                        gap,
                        more.warn_cooldown_minutes,
                        more.group_warnings_per_hour,
                        [(name, self.clock() + seconds) for name, seconds in cooldowns],
                    )
                    await self.store.call("operation", oid, "submitted")
                    gate()
                    if self.bot(pid, account)[1] != stamp:
                        raise Deferred("写入前连接变化，取消操作。")
                    submitted = True
                    try:
                        result = await asyncio.wait_for(
                            bot.call_action(action=action, self_id=account, **params), 15
                        )
                    except Exception as exc:
                        result = getattr(exc, "result", None)
                        if isinstance(result, dict) and result.get("status") == "failed":
                            raise ApiFailure(action, result) from exc
                        raise Uncertain("操作请求未确认完成，不会自动重发。") from exc
                    if isinstance(result, dict) and ("status" in result or "retcode" in result):
                        if result.get("status") != "ok" or result.get("retcode", 0) != 0:
                            if result.get("status") == "failed":
                                raise ApiFailure(action, result)
                            raise Uncertain("QQ接口未返回明确的操作结果，不会重发。")
                        result = result.get("data")
                    if kind == "recall" and result is not None:
                        raise Uncertain("撤回接口返回类型异常，未确认操作结果。")
                    if kind != "recall":
                        try:
                            message_id(result.get("message_id") if isinstance(result, dict) else None)
                        except GuardError as exc:
                            raise Uncertain("发送接口未返回有效消息ID，不会自动重发。") from exc
                    await self.store.call("operation", oid, "confirmed")
                    # Pace from the response, so a slow API call cannot consume
                    # the entire interval before the next operation begins.
                    await self.store.call("put", "gap:" + key, self.clock() + gap)
                    self.journal.record("QQ操作完成", case=cid, operation=oid, kind=kind)
                    return result
                except BaseException as exc:
                    if oid:
                        state = (
                            "failed"
                            if isinstance(exc, ApiFailure)
                            else "unknown"
                            if submitted
                            else "not_sent"
                        )
                        await asyncio.shield(self.store.call("operation", oid, state, str(exc)))
                        if state == "unknown":
                            await asyncio.shield(
                                self.store.call("put", "pause:" + key, "操作结果不明，请核对后恢复")
                            )
                    if not submitted and isinstance(exc, ApiFailure):
                        # A failed permission/message read is a deferred preparation,
                        # not a failed QQ write that should trip the shared account guard.
                        raise Deferred(str(exc)) from exc
                    raise

            if guard is None:
                return await perform()

            invoked = False

            async def guarded():
                nonlocal invoked
                invoked = True
                try:
                    return await perform()
                except Deferred as exc:
                    raise guard.deferred_error(str(exc)) from exc

            try:
                options = (
                    dict(
                        priority=0 if kind == "recall" else 1 if kind == "warn" else 2,
                        group=message["group"],
                        label="消息撤回"
                        if kind == "recall"
                        else "成员警告"
                        if kind == "warn"
                        else "管理提醒",
                    )
                    if getattr(guard, "scheduling_version", 1) >= 3
                    else {}
                )
                result = await guard.run(
                    account=pid,
                    online=lambda: self.online(pid, account),
                    action=guarded,
                    config={
                        "recovery_min_seconds": more.recovery_seconds,
                        "recovery_max_seconds": more.recovery_seconds,
                        "failure_threshold": 1,
                        "failure_cooldown_seconds": 3600,
                    },
                    delay=(0, 0),
                    gap=gap_range,
                    key=None,
                    **options,
                )
                if not invoked:
                    raise Deferred("共享协调器没有执行本次操作，不将其视为成功。")
                return result
            except guard.deferred_error as exc:
                if isinstance(exc.__cause__, Deferred):
                    raise exc.__cause__
                raise Deferred("共享QQ操作协调器暂缓了本次操作，请检查其状态。") from exc
