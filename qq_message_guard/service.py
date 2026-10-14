from __future__ import annotations

import asyncio
import base64
import hashlib
import random
import time
from collections import defaultdict
from dataclasses import asdict

from .client import OneJev
from .config import QUESTION_VERSION, Deferred, GuardError, InputRejected, Uncertain
from .decisions import decide
from .lexicon import hints, matches, screen_output
from .messages import Message, bounded_context, message_id, model_state
from .notifications import is_notifiable, notification_text
from .ocr import Ocr
from .platform import ApiFailure


class Service:
    def __init__(
        self,
        settings,
        store,
        model,
        media,
        platform,
        journal,
        *,
        clock=time.time,
        sleep=asyncio.sleep,
        ocr=None,
    ):
        self.settings, self.store, self.model, self.media = settings, store, model, media
        self.platform, self.journal = platform, journal
        self.clock, self.sleep = clock, sleep
        self.ocr = ocr if ocr is not None else Ocr()
        self.connections = {}
        self.invalidated = {}
        self.cancelled_cases = set()
        self.paused_scopes = set()
        self.served = {}
        self.wake = asyncio.Event()
        self.action_wake = asyncio.Event()
        self.notify_wake = asyncio.Event()
        self.input_lock = asyncio.Lock()
        self.tasks, self.jobs = set(), set()
        self.stopping = False
        self.failure = ""
        self.model_cooldown = 0
        self.last_maintenance = 0

    async def start(self):
        # Observe local websocket identities only; never query or backfill QQ history.
        context = getattr(self.platform, "context", None)
        if context is not None:
            for instance in context.platform_manager.get_insts():
                if instance.meta().name != "aiocqhttp":
                    continue
                clients = getattr(getattr(instance, "bot", None), "_wsr_api_clients", {})
                if isinstance(clients, dict):
                    for account in clients:
                        try:
                            _, stamp = self.platform.bot(str(instance.meta().id), str(account))
                        except Deferred:
                            continue
                        self.connections[(str(instance.meta().id), str(account))] = (stamp, self.clock())
        self.model_cooldown = await self.store.call("get", "model-cooldown", 0)
        self.tasks = {
            asyncio.create_task(self.worker()),
            asyncio.create_task(self.notifier()),
            asyncio.create_task(self.action_worker()),
        }

    async def stop(self):
        self.stopping = True
        pending = self.tasks | (self.jobs - {asyncio.current_task()})
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        async with self.input_lock:
            pass

    async def observe(self, raw, pid):
        if self.stopping or self.failure:
            return
        async with self.input_lock:
            if raw.get("post_type") == "notice":
                await self.notice(raw, pid)
                return
            settings = self.settings()
            if not settings.enabled:
                return
            self.journal.check()
            message = Message.parse(raw, pid, settings.advanced.max_text_chars)
            if message is None:
                return
            policy = settings.group(message.group)
            if policy is None or not policy.enabled or (policy.bot_qq and policy.bot_qq != message.account):
                return
            context, quoted = await self.store.call(
                "history_context",
                message,
                settings.advanced.context_messages,
                settings.advanced.sender_context_messages,
            )
            if message.user in policy.exempt_users:
                await self.store.call("remember", message, settings.advanced.context_cache_messages)
                return
            missing = list(message.unsupported)
            if message.reply and not quoted:
                missing.append("引用消息不在本地条数缓存中")
            item = message.data()
            item["scope"] = message.scope
            if message.images and not settings.images_enabled:
                missing.append("图片审核已关闭")
            if len(message.images) > settings.advanced.max_images:
                missing.append("图片数量超过审核上限")
            stamp = None
            online_since = self.clock()
            try:
                _, stamp = self.platform.bot(message.platform, message.account)
                key = (message.platform, message.account)
                previous = self.connections.get(key)
                if previous is None or previous[0] != stamp:
                    self.connections[key] = (stamp, self.clock())
                online_since = self.connections[key][1]
            except Deferred:
                missing.append("接入连接无法确认")
            payload = {
                "message": item,
                "context": context,
                "missing": missing,
                "settings_revision": settings.revision,
                "policy_revision": policy.revision,
                "notification_enabled": policy.notify_private or policy.notify_group,
                "rules": policy.rules,
                "checks": [asdict(check) for check in settings.checks],
                "question_version": QUESTION_VERSION,
                "binding": stamp,
                "keyword_matches": matches(message.text, settings.terms),
                "keyword_review": policy.keyword_review,
            }
            age = self.clock() - message.at
            initial, reason = "queued", ""
            if message.at < int(online_since):
                initial, reason = "incomplete", "连接前的旧消息已跳过，只审核在线期间的新发言"
            elif age < -30 or age > settings.advanced.max_queue_seconds:
                initial, reason = "incomplete", "消息时间过旧或异常，未进行实时审核"
            elif not message.text and (not message.images or not settings.images_enabled):
                initial, reason = "incomplete", "当前消息没有首版可审核的文字或图片"
            case = await self.store.call(
                "add", message, payload, self.clock(), settings.advanced.queue_size, initial, reason
            )
            if case is None:
                return
            if message.at >= int(online_since):
                await self.store.call("remember", message, settings.advanced.context_cache_messages)
            self.journal.record(
                "收到审核消息",
                case=case["id"],
                group=message.group,
                state=case["state"],
                missing=missing,
                harmful=bool(case["flagged"]),
            )
            self.wake.set()

    async def notice(self, raw, pid):
        account, group = str(raw.get("self_id", "")), str(raw.get("group_id", ""))
        policy = self.settings().group(group)
        if policy is None or (policy.bot_qq and policy.bot_qq != account):
            return
        scope = f"{pid}:{account}:{group}"
        kind = raw.get("notice_type")
        if kind == "group_recall":
            try:
                mid = message_id(raw.get("message_id"))
            except GuardError:
                return
            self.invalidated[(scope, mid)] = self.clock() + 600
            await self.store.call("recalled", scope, mid, self.clock())
        elif str(raw.get("user_id", "")) == account and (
            kind == "group_decrease" or (kind == "group_admin" and raw.get("sub_type") == "unset")
        ):
            self.paused_scopes.add(scope)
            await self.store.call("put", "group-pause:" + scope, "机器人离群或管理员权限被取消")
            self.journal.record("群操作暂停", group=group, reason="机器人离群或管理员权限被取消")

    async def worker(self):
        while not self.stopping:
            try:
                if self.failure:
                    await self.sleep(1)
                    continue
                self.wake.clear()
                case = await self.store.call("next_case", self.served)
                if case is None:
                    try:
                        await asyncio.wait_for(self.wake.wait(), 1)
                    except asyncio.TimeoutError:
                        pass
                else:
                    self.served[case["scope"]] = self.clock()
                    await self.process(case)
                if self.clock() - self.last_maintenance >= 60:
                    self.journal.check()
                    more = self.settings().advanced
                    files = await self.store.call(
                        "maintain", self.clock(), more.retention_days, more.harmful_retention_days
                    )
                    root = self.store.path.parent
                    removed = []
                    for file in files:
                        # Only remove application-created relative evidence files.
                        if file.startswith("media/") and len(file.split("/")) == 2 and ".." not in file:
                            try:
                                (root / file).unlink(missing_ok=True)
                                removed.append(file)
                            except OSError as exc:
                                self.journal.record("过期图片暂未清理", exception=exc)
                    await self.store.call("purge_files", removed)
                    self.journal.maintain(more.retention_days, more.harmful_retention_days)
                    # Recall tombstones only need to live through bounded in-flight processing.
                    self.invalidated = {
                        key: until for key, until in self.invalidated.items() if until > self.clock()
                    }
                    self.last_maintenance = self.clock()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.failure = "审核后台异常，自动操作已停止，请检查日志并重载。"
                self.journal.record("后台异常", exception=exc)

    def policy_for(self, case):
        settings = self.settings()
        policy = settings.group(case["data"]["message"]["group"])
        if (
            self.stopping
            or self.failure
            or not settings.enabled
            or policy is None
            or not policy.enabled
            or settings.revision != case["data"]["settings_revision"]
            or policy.revision != case["data"]["policy_revision"]
        ):
            raise Deferred("插件停止、群已关闭或配置已变化，本次不继续操作。")
        return settings, policy

    async def process(self, case):
        cid, data = case["id"], case["data"]
        try:
            settings, policy = self.policy_for(case)
            message = data["message"]
            if (message["scope"], message["mid"]) in self.invalidated:
                return
            if self.clock() - case["created"] > settings.advanced.max_queue_seconds:
                raise Deferred("队列等待过长，未完成实时审核。")
            if self.clock() < self.model_cooldown:
                raise Deferred("模型服务处于故障冷却期，本次未完成审核。")
            missing = list(data["missing"])
            media = []
            media_records = []
            ocr_records = []
            if settings.images_enabled:
                for url in message["images"][: settings.advanced.max_images]:
                    try:
                        image = await self.media.load(url, settings.advanced)
                        content = base64.b64decode(image.split(",", 1)[1], validate=True)
                        digest = hashlib.sha256(content).hexdigest()
                        file = f"media/{cid}-{len(media) + 1}-{digest[:16]}.jpg"
                        path = self.store.path.parent / file
                        path.parent.mkdir(parents=True, exist_ok=True)
                        media_records.append({"file": file, "sha256": digest, "bytes": len(content)})
                        await self.store.call("patch", cid, {"media_records": list(media_records)})
                        await asyncio.to_thread(path.write_bytes, content)
                        media.append(image)
                        if settings.ocr_enabled:
                            try:
                                recognized = await self.ocr.recognize(path, settings)
                                ocr_records.append({"file": file, **asdict(recognized)})
                                if recognized.truncated:
                                    missing.append("图片文字超过审核上限，仅保留开头")
                            except GuardError as exc:
                                missing.append(str(exc))
                    except GuardError as exc:
                        missing.append(str(exc))
            if not message["text"] and not media:
                await self.store.call("patch", cid, {"missing": list(dict.fromkeys(missing))})
                raise Deferred("；".join(missing) or "图片未读取成功，没有可审核内容。")
            self.policy_for(case)
            context = [
                row for row in data["context"] if (message["scope"], row["mid"]) not in self.invalidated
            ]
            original_count = len(context)
            context = bounded_context(context, settings.advanced.context_chars)
            if message["reply"] and not any(row.get("quoted") for row in context):
                missing.append("引用原文已撤回或未能提供给模型")
            ocr_texts = [row["text"] for row in ocr_records]
            current_matches, definitions = hints(message, context, settings.terms, ocr_texts)
            state = model_state(message, policy, context, len(media), definitions, ocr_texts)
            request = OneJev.request(state, settings)
            # Media bytes live in bounded JPEG files; the stored request retains placeholders.
            await self.store.call(
                "patch",
                cid,
                {
                    "audit_request": request,
                    "media_records": media_records,
                    "ocr_records": ocr_records,
                    "keyword_matches": current_matches,
                    "context_used": len(context),
                    "context_trimmed": original_count - len(context),
                },
            )
            started = time.monotonic()
            try:
                result = await self.model.evaluate(state, settings, media)
            except asyncio.CancelledError:
                await asyncio.shield(self.store.call("put", "model-cooldown", self.clock() + 60))
                await asyncio.shield(
                    self.store.call(
                        "record_call",
                        cid,
                        self.clock(),
                        None,
                        (time.monotonic() - started) * 1000,
                        "插件停止中断了请求，服务端可能仍在计算",
                    )
                )
                raise
            except GuardError as exc:
                if not isinstance(exc, InputRejected):
                    self.model_cooldown = self.clock() + 60
                    await self.store.call("put", "model-cooldown", self.model_cooldown)
                await self.store.call(
                    "record_call", cid, self.clock(), None, (time.monotonic() - started) * 1000, str(exc)
                )
                raise
            await self.store.call("record_call", cid, self.clock(), result)
            self.model_cooldown = 0
            await self.store.call("put", "model-cooldown", 0)
            self.policy_for(case)
            decision = decide(result, settings, policy, current_matches)
            if message["reply"] and (message["scope"], message["reply"]) in self.invalidated:
                missing.append("引用原文在审核期间被撤回")
            flagged, auto, warn = decision.flagged, decision.recall, decision.warn
            state = "incomplete" if missing else "review" if flagged else "allowed"
            case = await self.store.call(
                "finish",
                cid,
                state,
                {
                    "scores": result.scores,
                    "notification_ready": decision.notify,
                    "actual_model": result.model,
                    "missing": missing,
                    "reason": "；".join([*missing, *([decision.reason] if decision.reason else [])]),
                    "intent": asdict(result.intent) if result.intent else None,
                    "action_intent_ok": bool(
                        not settings.intent_enabled
                        or (result.intent and result.intent.permits_action(settings.intent_confidence))
                    ),
                    "audit_response": result.raw_response,
                    "usage": {
                        "input_tokens": result.input_tokens,
                        "output_tokens": result.output_tokens,
                        "elapsed_ms": result.elapsed_ms,
                    },
                    "recall_ready": bool(auto and not missing and policy.auto_recall),
                    "warn_ready": bool(warn and not missing and policy.warn_member),
                    "auto_ready": bool(
                        not missing and (auto and policy.auto_recall or warn and policy.warn_member)
                    ),
                    "actions_pending": bool(
                        not missing and (auto and policy.auto_recall or warn and policy.warn_member)
                    ),
                },
                self.clock(),
                flagged,
            )
            self.journal.record(
                "模型审核完成",
                case=cid,
                scores=result.scores,
                model=result.model,
                state=case["state"],
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                elapsed_ms=result.elapsed_ms,
                harmful=flagged,
            )
            self.action_wake.set()
            self.notify_wake.set()
        except asyncio.CancelledError:
            await asyncio.shield(
                self.store.call(
                    "finish", cid, "incomplete", {"reason": "插件停止，审核或后续处理未完成"}, self.clock()
                )
            )
            raise
        except GuardError as exc:
            current = await self.store.call("case", cid)
            if current and current["state"] == "auditing":
                await self.store.call("finish", cid, "incomplete", {"reason": str(exc)}, self.clock())
            self.journal.record("本次审核未完成", case=cid, reason=str(exc))

    def gate(self, case, manual=False):
        settings, policy = self.policy_for(case)
        self.journal.check()
        message = case["data"]["message"]
        if (case["id"] in self.cancelled_cases and not manual) or message["scope"] in self.paused_scopes:
            raise Deferred("记录标记为误判或这个群已暂停，取消操作。")
        if (message["scope"], message["mid"]) in self.invalidated:
            raise Deferred("这条消息已撤回，取消操作。")
        if not -30 <= self.clock() - message["at"] <= settings.advanced.max_action_seconds:
            raise Deferred("消息超出本插件允许的撤回时间，请在QQ中人工处理。")
        if not manual and (not policy.auto_recall or case["data"].get("missing") or case["human"]):
            raise Deferred("本条记录不满足自动撤回条件。")
        if not manual and settings.intent_enabled and not case["data"].get("action_intent_ok"):
            raise Deferred("发言意图不明确或并非参与违规，不自动撤回。")
        if not manual and message.get("reply") and (message["scope"], message["reply"]) in self.invalidated:
            raise Deferred("引用原文已经撤回，请人工复核当前发言。")
        if case["state"] in ("recalled", "unknown") or case["human"] == "误判":
            raise Deferred("记录已撤回、结果不明或标记为误判，不再操作。")
        stamp = self.platform.bot(message["platform"], message["account"])[1]
        # JSON stores tuples as lists. Compare a canonical representation.
        from .config import fingerprint

        if fingerprint(stamp) != fingerprint(case["data"]["binding"]):
            raise Deferred("接入连接已经改变，不处理旧连接上的消息。")

    async def recall(self, case, manual=False):
        cid = case["id"]
        settings = self.settings()
        message = case["data"]["message"]

        async def prepare():
            current = await self.store.call("case", cid)
            if current is None or current["human"] == "误判" or current["state"] in ("recalled", "unknown"):
                raise Deferred("记录状态已经变化，取消操作。")
            manager = await self.platform.member(
                message["platform"], message["account"], message["group"], message["account"]
            )
            if manager["role"] not in ("admin", "owner"):
                self.paused_scopes.add(message["scope"])
                await self.store.call("put", "group-pause:" + message["scope"], "机器人没有管理员权限")
                raise Deferred("机器人没有管理员权限，已暂停这个群的操作。")
            target = await self.platform.member(
                message["platform"], message["account"], message["group"], message["user"]
            )
            if target["role"] in ("admin", "owner"):
                raise Deferred("群主或管理员消息只供复核，请在QQ中人工处理。")
            await self.platform.locate(message)

        try:
            self.gate(case, manual)
            await self.sleep(
                random.uniform(settings.advanced.action_min_seconds, settings.advanced.action_max_seconds)
            )
            await self.platform.write(
                message,
                cid,
                "recall",
                "delete_msg",
                {"message_id": message["mid"]},
                lambda: self.gate(case, manual),
                prepare,
            )
            await self.store.call(
                "finish", cid, "recalled", {"reason": "撤回接口确认成功"}, self.clock(), True
            )
            return "撤回接口确认成功。"
        except (Uncertain, ApiFailure) as exc:
            await self.store.call(
                "finish",
                cid,
                "unknown" if isinstance(exc, Uncertain) else "failed",
                {"reason": str(exc)},
                self.clock(),
                True,
            )
            raise
        except Deferred as exc:
            await self.store.call("finish", cid, "review", {"reason": str(exc)}, self.clock(), True)
            raise
        except asyncio.CancelledError:
            ops = await asyncio.shield(self.store.call("operations", cid))
            unknown = any(op["kind"] == "recall" and op["state"] == "unknown" for op in ops)
            await asyncio.shield(
                self.store.call(
                    "finish",
                    cid,
                    "unknown" if unknown else "review",
                    {"reason": "插件停止，撤回结果不明" if unknown else "插件停止，未提交撤回"},
                    self.clock(),
                    True,
                )
            )
            raise

    async def action_worker(self):
        while not self.stopping:
            try:
                self.action_wake.clear()
                case = None if self.failure else await self.store.call("next_recall")
                if case is None:
                    try:
                        await asyncio.wait_for(self.action_wake.wait(), 1)
                    except asyncio.TimeoutError:
                        pass
                    continue
                try:
                    if case["data"].get("recall_ready"):
                        try:
                            await self.recall(case)
                        except GuardError as exc:
                            self.journal.record(
                                "自动撤回未完成", case=case["id"], reason=str(exc), harmful=True
                            )
                    if case["data"].get("warn_ready"):
                        current = await self.store.call("case", case["id"])
                        if current:
                            await self.warn(current)
                except GuardError as exc:
                    await self.store.call("patch", case["id"], {"warning_reason": str(exc)})
                    self.journal.record("群内警告未完成", case=case["id"], reason=str(exc), harmful=True)
                finally:
                    await asyncio.shield(self.store.call("patch", case["id"], {"actions_pending": False}))
                    self.notify_wake.set()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.failure = "撤回后台异常，自动操作已停止，请检查日志并重载。"
                self.journal.record("撤回后台异常", exception=exc)

    async def notifier(self):
        while not self.stopping:
            try:
                try:
                    await asyncio.wait_for(self.notify_wake.wait(), 5)
                except asyncio.TimeoutError:
                    pass
                self.notify_wake.clear()
                if not self.failure:
                    await self.send_alerts()
            except asyncio.CancelledError:
                raise
            except GuardError as exc:
                self.journal.record("通知暂缓", reason=str(exc))
            except Exception as exc:
                self.failure = "通知后台异常，自动操作已停止，请检查日志并重载。"
                self.journal.record("通知后台异常", exception=exc)

    async def send_alerts(self):
        settings = self.settings()
        if not settings.enabled or not any(
            p.enabled and (p.notify_private or p.notify_group) for p in settings.groups
        ):
            return
        scopes = defaultdict(list)
        for case in await self.store.call("alerts", settings.revision):
            policy = settings.group(case["data"]["message"]["group"])
            if (
                policy
                and policy.enabled
                and (policy.notify_private or policy.notify_group)
                and case["data"]["settings_revision"] == settings.revision
                and is_notifiable(case)
            ):
                scopes[case["scope"]].append(case)
        for scope, cases in scopes.items():
            if self.clock() < await self.store.call("get", "notify:" + scope, 0):
                continue
            await self.store.call(
                "put", "notify:" + scope, self.clock() + settings.advanced.notify_minutes * 60
            )
            batch_key = "alert-batch:" + scope
            batch = await self.store.call("get", batch_key)
            if not batch or batch["revision"] != settings.revision:
                users = set()
                for item in cases:
                    if len(users) < 8:
                        users.add(item["data"]["message"]["user"])
                batch = {
                    "ids": [c["id"] for c in cases if c["data"]["message"]["user"] in users],
                    "revision": settings.revision,
                    "anchor": cases[0]["id"],
                }
                await self.store.call("put", batch_key, batch)
            cases = [c for c in await self.store.call("batch_cases", batch["ids"]) if is_notifiable(c)]
            if not cases:
                await self.store.call("notified", batch["ids"])
                await self.store.call("put", batch_key, None)
                continue
            first, message = cases[0], cases[0]["data"]["message"]
            policy = settings.group(message["group"])

            def gate():
                latest, policy = self.policy_for(first)
                if (
                    not policy.notify_private and not policy.notify_group
                ) or latest.notify_users != settings.notify_users:
                    raise Deferred("通知配置已经变化。")

            targets = (
                [("private:" + user, "send_private_msg", {"user_id": user}) for user in settings.notify_users]
                if policy.notify_private
                else []
            )
            if policy.notify_group:
                targets.append(
                    ("group:" + policy.admin_group, "send_group_msg", {"group_id": policy.admin_group})
                )
            pending = False
            for target, action, params in targets:
                kind = "notice:" + target
                previous = next(
                    (op for op in await self.store.call("operations", batch["anchor"]) if op["kind"] == kind),
                    None,
                )
                if previous is not None and previous["state"] != "not_sent":
                    continue  # Submitted delivery results, including unknown ones, are never resent.
                selected, seen, cooldowns = [], set(), []
                for item in cases:
                    user = item["data"]["message"]["user"]
                    key = f"notice-member:{scope}:{target}:{user}"
                    if user not in seen and self.clock() >= await self.store.call("get", key, 0):
                        selected.append(item)
                        cooldowns.append((key, settings.advanced.notify_cooldown_minutes * 60))
                        seen.add(user)
                if not selected:
                    continue
                text = notification_text(selected, policy, settings)

                async def prepare(params=params):
                    if "group_id" in params:
                        await self.platform.member(
                            message["platform"], message["account"], params["group_id"], message["account"]
                        )

                try:
                    await self.platform.write(
                        message,
                        batch["anchor"],
                        kind,
                        action,
                        {**params, "message": [{"type": "text", "data": {"text": text}}]},
                        gate,
                        prepare,
                        cooldowns=cooldowns,
                    )
                except GuardError as exc:
                    self.journal.record("管理员通知未完成", case=first["id"], reason=str(exc))
                    submitted = any(
                        op["kind"] == kind and op["state"] != "not_sent"
                        for op in await self.store.call("operations", batch["anchor"])
                    )
                    pending = pending or not submitted
            if not pending:
                await self.store.call("notified", batch["ids"])
                await self.store.call("put", batch_key, None)

    async def resume(self, group, pid, account):
        settings = self.settings()
        policy = settings.group(group)
        if policy is None:
            raise GuardError("这个群尚未配置。")
        if policy.bot_qq and policy.bot_qq != account:
            raise GuardError("这个群由另一个QQ账号负责。")
        if not await self.platform.online(pid, account):
            raise GuardError("QQ尚未在线。")
        manager = await self.platform.member(pid, account, group, account)
        if manager["role"] not in ("admin", "owner"):
            raise GuardError("机器人不是该群管理员，无法恢复撤回功能。")
        await self.store.call("put", f"group-pause:{pid}:{account}:{group}", "")
        await self.store.call("put", f"pause:{pid}:{account}", "")
        self.journal.record("管理员恢复操作", group=group)
        self.paused_scopes.discard(f"{pid}:{account}:{group}")
        return "已解除本插件的操作暂停；旧的已提交请求不会重发。共享协调器的暂停仍需在其所属插件中处理。"

    async def mark(self, cid, result, actor):
        self.cancelled_cases.add(cid)
        await self.store.call("mark", cid, result, actor, self.clock())
        self.journal.record("人工判定", case=cid, result=result, actor=actor, harmful=result == "违规")

    async def warn(self, case):
        cid, message = case["id"], case["data"]["message"]
        settings, policy = self.policy_for(case)
        key = f"warn:{message['platform']}:{message['account']}:{message['group']}:{message['user']}"
        if self.clock() < await self.store.call("get", key, 0):
            raise Deferred("这个成员的群内警告仍在冷却期，不重复@。")
        operations = await self.store.call("operations", cid)
        own_recall = any(op["kind"] == "recall" and op["state"] == "confirmed" for op in operations)

        def gate():
            latest, rule = self.policy_for(case)
            self.journal.check()
            if (
                not rule.warn_member
                or case["data"].get("missing")
                or case["human"]
                or cid in self.cancelled_cases
                or (latest.intent_enabled and not case["data"].get("action_intent_ok"))
            ):
                raise Deferred("本条记录不满足群内警告条件。")
            if message["scope"] in self.paused_scopes or case["state"] == "unknown":
                raise Deferred("这个群已暂停或操作结果不明，不发送警告。")
            if message.get("reply") and (message["scope"], message["reply"]) in self.invalidated:
                raise Deferred("引用原文已经撤回，不自动警告当前成员。")
            if (
                (message["scope"], message["mid"]) in self.invalidated or case["state"] == "recalled"
            ) and not own_recall:
                raise Deferred("成员或管理员已经撤回，不再发送群内警告。")
            if not -30 <= self.clock() - message["at"] <= latest.advanced.max_action_seconds:
                raise Deferred("消息过旧，不再发送群内警告。")
            from .config import fingerprint

            if fingerprint(self.platform.bot(message["platform"], message["account"])[1]) != fingerprint(
                case["data"]["binding"]
            ):
                raise Deferred("接入连接已改变，不发送旧消息警告。")

        async def prepare():
            current = await self.store.call("case", cid)
            if current is None or current["human"] or cid in self.cancelled_cases:
                raise Deferred("人工判定已变化，取消群内警告。")
            manager = await self.platform.member(
                message["platform"], message["account"], message["group"], message["account"]
            )
            if manager["role"] not in ("admin", "owner"):
                self.paused_scopes.add(message["scope"])
                await self.store.call("put", "group-pause:" + message["scope"], "机器人没有管理员权限")
                raise Deferred("群内自动警告需要机器人具有管理员权限。")
            target = await self.platform.member(
                message["platform"], message["account"], message["group"], message["user"]
            )
            if target["role"] in ("admin", "owner"):
                raise Deferred("群主或管理员消息只供人工复核，不自动@警告。")
            if not own_recall:
                await self.platform.locate(message)

        hits = "、".join(
            c.name for c in settings.checks if case["data"].get("scores", {}).get(c.key, 0) >= c.warn_at
        )
        text = policy.warning_text.format_map(
            {"群号": message["group"], "QQ号": message["user"], "审核项": hits}
        )
        if len(text) > 2000:
            raise GuardError("警告模板展开后超过2000字，请缩短模板。")
        screen_output(text, settings.terms)
        gate()
        if not own_recall:
            await self.sleep(
                random.uniform(settings.advanced.action_min_seconds, settings.advanced.action_max_seconds)
            )
        await self.platform.write(
            message,
            cid,
            "warn",
            "send_group_msg",
            {
                "group_id": message["group"],
                "message": [
                    {"type": "at", "data": {"qq": message["user"]}},
                    {"type": "text", "data": {"text": " " + text}},
                ],
            },
            gate,
            prepare,
        )
        await self.store.call("patch", cid, {"warned": True})
        self.journal.record("群内警告完成", case=cid, harmful=True)
