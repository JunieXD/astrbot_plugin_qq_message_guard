"""Escalating member mutes, driven only by confirmed automatic recalls."""

from __future__ import annotations

import asyncio
import math

from .config import Deferred, GuardError, fingerprint, qq


def member_key(scope, user):
    return f"mute-member:{scope}:{user}"


def observation_key(scope, user):
    return f"mute-observed:{scope}:{user}"


def plan_mute(message, rules, strikes, history, now):
    previous = history or {}
    until = previous.get("until", 0)
    if until > now:
        return None
    repeat = (
        previous.get("duration", 0) > 0
        and rules.repeat_hours > 0
        and until - 1 <= message["at"] <= until + rules.repeat_hours * 3600
    )
    if repeat:
        seconds = min(previous["duration"] * rules.multiplier, rules.max_minutes * 60)
        sources = [*previous.get("source_ids", []), message["case_id"]]
        reason = f"解禁后{rules.repeat_hours}小时内复犯，按上次{rules.multiplier}倍禁言"
    elif len(strikes) >= rules.trigger_recalls:
        seconds = rules.initial_minutes * 60
        sources = [row["case_id"] for row in strikes]
        reason = f"{rules.window_minutes}分钟内自动撤回达到{rules.trigger_recalls}次"
    else:
        return None
    return {"duration": seconds, "source_ids": list(dict.fromkeys(sources)), "reason": reason}


def muted_until(member):
    value = member.get("shut_up_timestamp")
    try:
        if isinstance(value, bool) or value is None:
            raise ValueError()
        value = float(value)
        if not math.isfinite(value) or not 0 <= value < 2**40:
            raise ValueError()
    except (TypeError, ValueError, OverflowError) as exc:
        raise Deferred("成员当前禁言状态无法确认，不自动禁言。") from exc
    return value


class EscalatingMute:
    def __init__(self, service):
        self.s = service
        self.locks = {}
        self.active = set()
        self.notice_versions = {}

    async def observe_notice(self, raw, scope):
        try:
            user = qq(raw.get("user_id", ""))
        except GuardError:
            return
        duration, stamp = raw.get("duration"), raw.get("time")
        if (
            type(duration) is not int
            or not 0 <= duration <= 2592000
            or type(stamp) is not int
            or not 0 < stamp <= self.s.clock() + 30
        ):
            return
        if raw.get("sub_type") not in ("ban", "lift_ban"):
            return
        key = member_key(scope, user)
        history = await self.s.store.call("get", key)
        if key not in self.active and history is None:
            return
        self.notice_versions[key] = self.notice_versions.get(key, 0) + 1
        await self.s.store.call(
            "mute_notice", scope, user, stamp, duration, self.s.clock(), str(raw.get("operator_id", ""))
        )

    async def strikes(self, case, rules, history):
        since = max(self.s.clock() - rules.window_minutes * 60, (history or {}).get("muted_at", 0))
        message = case["data"]["message"]
        return await self.s.store.call(
            "mute_strikes",
            case["scope"],
            message["user"],
            since,
            self.s.clock(),
            int(history["muted_at"]) + 1 if history else 0,
        )

    async def after_recall(self, case):
        settings, policy = self.s.policy_for(case)
        if not policy.mute.enabled:
            return
        message, cid = case["data"]["message"], case["id"]
        key = member_key(case["scope"], message["user"])
        async with self.locks.setdefault(key, asyncio.Lock()):
            history = await self.s.store.call("get", key)
            strikes = await self.strikes(case, policy.mute, history)
            if cid not in {r["case_id"] for r in strikes}:
                return
            maybe_repeat = bool(
                history
                and history.get("duration")
                and policy.mute.repeat_hours
                and message["at"] <= history["until"] + policy.mute.repeat_hours * 3600
            )
            if len(strikes) < policy.mute.trigger_recalls and not maybe_repeat:
                return
            params = {"group_id": message["group"], "user_id": message["user"], "duration": 0}
            plan, version = None, self.notice_versions.get(key, 0)

            def gate():
                _, latest = self.s.policy_for(case)
                self.s.journal.check()
                if not latest.mute.enabled or message["scope"] in self.s.paused_scopes:
                    raise Deferred("这个群的自动禁言已关闭或暂停。")
                if (
                    cid in self.s.cancelled_cases
                    or case["human"]
                    or case["data"].get("cache_invalidated")
                    or any(i in self.s.cancelled_cases for i in (plan or {}).get("source_ids", []))
                ):
                    raise Deferred("禁言依据已被人工处理，取消本次禁言。")
                if not -30 <= self.s.clock() - message["at"] <= settings.advanced.max_action_seconds:
                    raise Deferred("消息已超出自动操作时限，不补发禁言。")
                stamp = self.s.platform.bot(message["platform"], message["account"])[1]
                if fingerprint(stamp) != fingerprint(case["data"]["binding"]):
                    raise Deferred("接入连接变化，不对旧连接消息禁言。")
                if self.notice_versions.get(key, 0) != version:
                    raise Deferred("成员禁言状态刚发生变化，本次不覆盖。")

            async def prepare():
                nonlocal plan
                current = await self.s.store.call("case", cid)
                if not current or current["human"] or current["data"].get("cache_invalidated"):
                    raise Deferred("当前撤回依据已失效，不自动禁言。")
                operations = await self.s.store.call("operations", cid)
                if not any(op["kind"] == "recall" and op["state"] == "confirmed" for op in operations):
                    raise Deferred("没有确认成功的自动撤回，不自动禁言。")
                await self.s.check_cache_origin(current)
                manager = await self.s.platform.member(
                    message["platform"], message["account"], message["group"], message["account"]
                )
                if manager["role"] not in ("admin", "owner"):
                    raise Deferred("机器人没有管理员权限，不自动禁言。")
                target = await self.s.platform.member(
                    message["platform"], message["account"], message["group"], message["user"]
                )
                if self.notice_versions.get(key, 0) != version:
                    raise Deferred("读取成员资料期间禁言状态发生变化，不覆盖现有禁言。")
                if target["role"] != "member":
                    raise Deferred("不自动禁言群主或管理员。")
                until = muted_until(target)
                previous = await self.s.store.call(
                    "mute_observe_member",
                    message["scope"],
                    message["user"],
                    until,
                    self.s.clock(),
                    message["at"],
                )
                if until > self.s.clock():
                    raise Deferred("成员当前已在禁言中，不覆盖或叠加现有禁言。")
                candidates = await self.strikes(current, policy.mute, previous)
                if cid not in {r["case_id"] for r in candidates}:
                    raise Deferred("当前撤回已不满足计数条件，不自动禁言。")
                plan = plan_mute(
                    {**message, "case_id": cid}, policy.mute, candidates, previous, self.s.clock()
                )
                if plan is None:
                    raise Deferred("当前未达到禁言触发条件。")
                params["duration"] = plan["duration"]
                await self.s.store.call("patch", cid, {"mute_plan": plan, "mute_status": "planned"})

            self.active.add(key)
            try:
                await self.s.platform.write(message, cid, "mute", "set_group_ban", params, gate, prepare)
                self.s.journal.record("重复违规禁言完成", case=cid, duration=params["duration"], harmful=True)
            except GuardError as exc:
                operations = await self.s.store.call("operations", cid)
                operation = next((op for op in operations if op["kind"] == "mute"), {})
                status = operation.get("state", "skipped")
                if status != "confirmed":
                    await self.s.store.call("patch", cid, {"mute_status": status, "mute_reason": str(exc)})
                self.s.journal.record("本次禁言未完成", case=cid, reason=str(exc), harmful=True)
            finally:
                self.active.discard(key)
