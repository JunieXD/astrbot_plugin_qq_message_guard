"""Gentle, category-specific reminders and their independent short-mute policy."""

from __future__ import annotations

import asyncio

from .classification import QWEN_CATEGORIES, matched_categories
from .config import Deferred, GuardError, fingerprint, qq
from .lexicon import screen_output
from .muting import member_key, muted_until
from .platform import ApiFailure


def selected_categories(result, settings, policy):
    if not policy.controversy.enabled or result.safety != "Controversial":
        return ()
    order = {name: i for i, name in enumerate(policy.controversy.categories)}
    return tuple(
        sorted(
            (c for c in matched_categories(result, settings) if QWEN_CATEGORIES[c] in order),
            key=lambda c: order[QWEN_CATEGORIES[c]],
        )
    )


class ControversyReminders:
    def __init__(self, service):
        self.s = service
        self.wake = asyncio.Event()

    def gate(self, case, *, mute=False):
        if case is None:
            raise Deferred("审核记录已过期，不继续操作。")
        settings, policy = self.s.policy_for(case)
        data, message = case["data"], case["data"]["message"]
        self.s.journal.check()
        if not policy.controversy.enabled or (mute and not policy.controversy.mute_enabled):
            raise Deferred("本群争议提醒或累计禁言已关闭。")
        if (
            case["human"]
            or case["id"] in self.s.cancelled_cases
            or data.get("cache_invalidated")
            or not data.get("controversy_ready")
            or message["scope"] in self.s.paused_scopes
        ):
            raise Deferred("争议提醒依据已变化或本群已暂停。")
        if not -30 <= self.s.clock() - message["at"] <= settings.advanced.max_action_seconds:
            raise Deferred("争议消息已过自动操作时限，不补发提醒或禁言。")
        if fingerprint(self.s.platform.bot(message["platform"], message["account"])[1]) != fingerprint(
            data["binding"]
        ):
            raise Deferred("接入连接已变化，不处理旧连接上的争议消息。")
        if not mute and (
            case["state"] == "recalled" or (case["scope"], message["mid"]) in self.s.invalidated
        ):
            raise Deferred("原消息已经撤回，不再发送争议提醒。")
        return policy.controversy

    async def member(self, case):
        message = case["data"]["message"]
        manager = await self.s.platform.member(
            message["platform"], message["account"], message["group"], message["account"]
        )
        if manager["role"] not in ("admin", "owner"):
            raise Deferred("机器人没有管理员权限，不执行争议提醒或禁言。")
        target = await self.s.platform.member(
            message["platform"], message["account"], message["group"], message["user"]
        )
        if target["role"] != "member":
            raise Deferred("不对群主或管理员执行争议提醒或禁言。")
        if muted_until(target) > self.s.clock():
            raise Deferred("成员已经处于禁言中，不追加提醒或覆盖禁言。")

    async def warn(self, case):
        rules = self.gate(case)
        data, message = case["data"], case["data"]["message"]
        categories = data["controversy_categories"]
        texts = dict(rules.messages)
        text = texts.get(QWEN_CATEGORIES[categories[0]], "这段话可能引起争议，请留意措辞。")
        screen_output(text, self.s.settings().terms)

        async def prepare():
            current = await self.s.store.call("case", case["id"])
            self.gate(current)
            await self.s.check_cache_origin(current)
            await self.member(current)
            history = await self.s.store.call(
                "get", f"controversy-round:{case['scope']}:{message['user']}", {}
            )
            if history and message["at"] <= int(history["at"]):
                raise Deferred("禁言前积压的旧消息不补发争议提醒。")
            await self.s.platform.locate(message)

        await self.s.platform.write(
            message,
            case["id"],
            "controversy_warn",
            "send_group_msg",
            {
                "group_id": message["group"],
                "message": [
                    {"type": "at", "data": {"qq": message["user"]}},
                    {"type": "text", "data": {"text": " " + text}},
                ],
            },
            lambda: self.gate(case),
            prepare,
        )
        self.wake.set()
        self.s.journal.record("争议提醒完成", case=case["id"], categories=categories, harmful=True)
        await self.mute(case)

    async def mute(self, case):
        _, policy = self.s.policy_for(case)
        if not policy.controversy.mute_enabled:
            return
        rules = self.gate(case, mute=True)
        message = case["data"]["message"]
        key = member_key(case["scope"], message["user"])
        async with self.s.mutes.locks.setdefault(key, asyncio.Lock()):
            strikes = await self.s.store.call(
                "controversy_strikes",
                case["scope"],
                message["user"],
                self.s.clock() - rules.window_minutes * 60,
                self.s.clock(),
            )
            if len(strikes) < rules.trigger_warnings or case["id"] not in {r["case_id"] for r in strikes}:
                return
            version = self.s.mutes.notice_versions.get(key, 0)
            sources = []

            def gate():
                self.gate(case, mute=True)
                if self.s.mutes.notice_versions.get(key, 0) != version:
                    raise Deferred("成员禁言状态刚发生变化，不覆盖现有操作。")
                if any(cid in self.s.cancelled_cases for cid in sources):
                    raise Deferred("争议累计依据已被人工处理。")

            async def prepare():
                current = await self.s.store.call("case", case["id"])
                self.gate(current, mute=True)
                await self.member(current)
                candidates = await self.s.store.call(
                    "controversy_strikes",
                    case["scope"],
                    message["user"],
                    self.s.clock() - rules.window_minutes * 60,
                    self.s.clock(),
                )
                if len(candidates) < rules.trigger_warnings or case["id"] not in {
                    r["case_id"] for r in candidates
                }:
                    raise Deferred("当前未达到争议提醒累计禁言条件。")
                sources[:] = [r["case_id"] for r in candidates]
                await self.s.store.call(
                    "patch",
                    case["id"],
                    {"controversy_mute_plan": {"duration": rules.mute_minutes * 60, "source_ids": sources}},
                )

            self.s.mutes.active.add(key)
            try:
                await self.s.platform.write(
                    message,
                    case["id"],
                    "controversy_mute",
                    "set_group_ban",
                    {
                        "group_id": message["group"],
                        "user_id": message["user"],
                        "duration": rules.mute_minutes * 60,
                    },
                    gate,
                    prepare,
                )
                self.s.journal.record(
                    "争议累计禁言完成", case=case["id"], minutes=rules.mute_minutes, harmful=True
                )
            except GuardError as exc:
                self.s.journal.record("争议累计禁言未完成", case=case["id"], reason=str(exc), harmful=True)
            finally:
                self.s.mutes.active.discard(key)

    async def recalled(self, raw, scope, mid):
        try:
            user, operator = qq(raw.get("user_id", "")), qq(raw.get("operator_id", ""))
        except GuardError:
            return
        stamp = raw.get("time")
        if type(stamp) is not int or not 0 < stamp <= self.s.clock() + 30:
            return
        await self.s.store.call("controversy_recalled", scope, mid, user, operator, stamp, self.s.clock())
        self.wake.set()

    async def cleanup(self, case, warning):
        message = case["data"]["message"]

        def gate():
            settings = self.s.settings()
            policy = settings.group(message["group"])
            self.s.journal.check()
            if (
                self.s.stopping
                or self.s.failure
                or not settings.enabled
                or not policy
                or not policy.enabled
                or not policy.controversy.enabled
                or (policy.bot_qq and policy.bot_qq != message["account"])
            ):
                raise Deferred("插件或本群争议提醒已停止。")
            if self.s.clock() > warning["deadline"] + 300:
                raise Deferred("提醒清理任务已过期。")
            if (case["scope"], warning["warning_mid"]) in self.s.invalidated:
                raise Deferred("该提醒已经撤回。")

        async def prepare():
            current = await self.s.store.call("controversy_warning", case["id"])
            if not current or current["cleanup_state"] != "pending":
                raise Deferred("该提醒不再需要撤回。")
            await self.s.platform.member(
                message["platform"], message["account"], message["group"], message["account"]
            )
            try:
                found = await self.s.platform.call(
                    message["platform"], message["account"], "get_msg", message_id=warning["warning_mid"]
                )
            except ApiFailure as exc:
                await self.s.store.call("close_controversy_cleanup", case["id"], "failed")
                raise Deferred("提醒消息无法定位，不再自动尝试撤回。") from exc
            if (
                not isinstance(found, dict)
                or str(found.get("message_id")) != warning["warning_mid"]
                or str(found.get("group_id")) != message["group"]
                or str((found.get("sender") or {}).get("user_id")) != message["account"]
            ):
                await self.s.store.call("close_controversy_cleanup", case["id"], "failed")
                raise Deferred("无法确认是本账号发出的对应提醒，不撤回。")

        await self.s.platform.write(
            message,
            case["id"],
            "controversy_cleanup",
            "delete_msg",
            {"message_id": warning["warning_mid"]},
            gate,
            prepare,
        )
        self.s.journal.record("本人撤回后清理争议提醒", case=case["id"])

    async def worker(self):
        while not self.s.stopping:
            try:
                self.wake.clear()
                warning = (
                    None
                    if self.s.failure
                    else await self.s.store.call("pending_controversy_cleanup", self.s.clock())
                )
                if warning is None:
                    try:
                        await asyncio.wait_for(self.wake.wait(), 5)
                    except asyncio.TimeoutError:
                        pass
                    continue
                case = await self.s.store.call("case", warning["case_id"])
                try:
                    await self.cleanup(case, warning)
                except GuardError as exc:
                    if (case["scope"], warning["warning_mid"]) in self.s.invalidated:
                        await self.s.store.call("close_controversy_cleanup", case["id"], "gone")
                    else:
                        await self.s.store.call("defer_controversy_cleanup", case["id"], self.s.clock())
                    self.s.journal.record("争议提醒清理暂缓", case=case["id"], reason=str(exc))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.s.failure = "争议提醒清理后台异常，自动操作已停止，请检查日志并重载。"
                self.s.journal.record("争议提醒清理异常", exception=exc)
                await self.s.sleep(1)
