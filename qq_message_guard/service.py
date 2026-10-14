from __future__ import annotations

import asyncio
import base64
import hashlib
import random
import time
import uuid
from collections import defaultdict
from dataclasses import asdict

from .batching import build_state, location_request, queued_at, screen_request, spot_check
from .categories import CATEGORIES
from .client import OneJev
from .config import QUESTION_VERSION, Deferred, GuardError, InputRejected, Uncertain
from .decisions import decide
from .evidence import EvidenceArchive, atomic_write, evidence_path, original_text
from .lexicon import hints, screen_output
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
        self.evidence = EvidenceArchive(store)
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
                "keyword_matches": hints(item, context, settings.terms)[0],
                "keyword_review": policy.keyword_review,
                "original_text": original_text(raw),
                "original_image_count": sum(segment["type"] == "image" for segment in raw["message"]),
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
                cases, wait = await self.store.call("next_work", self.served, self.settings(), self.clock())
                if not cases:
                    try:
                        await asyncio.wait_for(self.wake.wait(), min(1, wait))
                    except asyncio.TimeoutError:
                        pass
                else:
                    self.served[cases[0]["scope"]] = self.clock()
                    await self.dispatch(cases)
                if self.clock() - self.last_maintenance >= 60:
                    self.journal.check()
                    await self.maintain()
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

    async def invoke_model(self, case, settings, call, stage="detail", sid=None):
        """Record one physical HTTP call once, including failures and interrupted calls."""
        started = time.monotonic()
        result, error = None, ""
        try:
            result = await call()
            self.model_cooldown = 0
            await self.store.call("put", "model-cooldown", 0)
            return result
        except asyncio.CancelledError:
            error = "插件停止中断了请求，服务端可能仍在计算"
            await asyncio.shield(self.store.call("put", "model-cooldown", self.clock() + 60))
            raise
        except GuardError as exc:
            error = str(exc)
            if not isinstance(exc, InputRejected):
                self.model_cooldown = self.clock() + 60
                await self.store.call("put", "model-cooldown", self.model_cooldown)
            raise
        finally:
            await asyncio.shield(
                self.store.call(
                    "record_call",
                    case["id"],
                    self.clock(),
                    result,
                    (time.monotonic() - started) * 1000,
                    error,
                    stage,
                    sid,
                )
            )
            if sid is not None:
                await asyncio.shield(
                    self.store.call(
                        "finish_screening",
                        sid,
                        self.clock(),
                        result,
                        error,
                        stage == "locate"
                        or bool(result and max(result.scores.values()) >= settings.screen_threshold),
                    )
                )

    async def batch_live(self, cases, check_age=True):
        live = []
        for old in cases:
            case = await self.store.call("case", old["id"])
            if case is None or case["state"] != "auditing":
                continue
            try:
                settings, _ = self.policy_for(case)
                if check_age and self.clock() - queued_at(case) > settings.advanced.max_queue_seconds:
                    raise Deferred("队列等待过长，未完成实时审核。")
                if self.clock() < self.model_cooldown:
                    raise Deferred("模型服务处于故障冷却期，本次未完成审核。")
                if case["human"]:
                    raise Deferred("已人工标记，不继续自动审核。")
                message = case["data"]["message"]
                if (case["scope"], message["mid"]) in self.invalidated:
                    raise Deferred("消息已撤回，不继续审核。")
                if message["reply"] and (case["scope"], message["reply"]) in self.invalidated:
                    raise Deferred("引用原文已经撤回，本条未完整审核。")
                live.append(case)
            except GuardError as exc:
                await self.store.call("finish", case["id"], "incomplete", {"reason": str(exc)}, self.clock())
        return live

    async def dispatch(self, cases):
        if cases[0]["data"].get("batch", {}).get("phase") == "locate":
            await self.process_location(cases)
        elif len(cases) == 1:
            await self.process(cases[0])
        else:
            await self.process_batch(cases)

    async def after_screen(self, cases, result, refs, labels, candidates, priorities=None):
        """Finish clear cases now; queue individual detail, yielding model fairness."""
        priorities = priorities or {}
        for case in await self.batch_live(cases, check_age=False):
            settings, _ = self.policy_for(case)
            sampled = case["id"] not in candidates and spot_check(case, settings)
            metadata = {
                "screening_ids": refs,
                "label": labels[case["id"]],
                "threshold": settings.screen_threshold,
                "spot_check": sampled,
                "priority": priorities.get(case["id"], 0.5) if case["id"] in candidates else 0,
            }
            if case["id"] in candidates or sampled:
                await self.store.call(
                    "queue_stage", case["id"], {**metadata, "phase": "detail"}, self.clock()
                )
            else:
                await self.store.call("patch", case["id"], {"batch": metadata})
                await self.store.call(
                    "finish",
                    case["id"],
                    "allowed",
                    {
                        "audit_path": "batch_clear",
                        "actual_model": result.model,
                        "reason": "批量初筛未命中，未进行逐条细审；不代表保证无风险。",
                        "notification_ready": False,
                        "auto_ready": False,
                        "actions_pending": False,
                    },
                    self.clock(),
                )
        self.wake.set()

    async def process_batch(self, cases):
        try:
            live = await self.batch_live(cases)
            if len(live) < 2:
                for case in live:
                    await self.process(case)
                return
            settings, _ = self.policy_for(live[0])
            state = build_state(live, settings, self.invalidated)
            labels = {c["id"]: row[0] for c, row in zip(live, state["待初筛消息"])}
            request = screen_request(state, settings)
            sid = await self.store.call("begin_screening", live, "screen", request, self.clock())
            result = await self.invoke_model(
                live[0], settings, lambda: self.model.evaluate_request(request, settings), "screen", sid
            )
            keys = [key for key, score in result.scores.items() if score >= settings.screen_threshold]
            live = await self.batch_live(live, check_age=False)
            if keys and len(live) > 2 and location_request(state, settings, keys) is not None:
                for case in live:
                    await self.store.call(
                        "queue_stage",
                        case["id"],
                        {
                            "screening_ids": [sid],
                            "label": labels[case["id"]],
                            "threshold": settings.screen_threshold,
                            "phase": "locate",
                            "keys": keys,
                            "priority": 0.5,
                        },
                        self.clock(),
                    )
                self.wake.set()
            else:
                await self.after_screen(
                    live, result, [sid], labels, {c["id"] for c in live} if keys else set()
                )
            self.journal.record(
                "批量初筛完成", screening=sid, messages=len(cases), scores=result.scores, harmful=bool(keys)
            )
        except InputRejected as exc:
            self.journal.record("批量输入被拒绝，回退逐条", reason=str(exc))
            await self.queue_fallback(cases)
        except asyncio.CancelledError:
            await asyncio.shield(self.finish_batch(cases, "插件停止中断了批量审核，未补审。"))
            raise
        except GuardError as exc:
            await self.finish_batch(cases, str(exc))

    async def process_location(self, cases):
        try:
            live = await self.batch_live(cases)
            if not live:
                return
            settings, _ = self.policy_for(live[0])
            batch = live[0]["data"]["batch"]
            sid = batch["screening_ids"][0]
            shared = await self.store.call("screening", sid)
            if shared is None:
                raise Deferred("关联初筛记录不可用，未完成候选定位。")
            labels = {c["id"]: c["data"]["batch"]["label"] for c in live}
            active = set(labels.values())
            state = shared["data"]["request"]["state"]
            state = {**state, "待初筛消息": [row for row in state["待初筛消息"] if row[0] in active]}
            request = location_request(state, settings, batch["keys"])
            if request is None or len(live) <= 2:
                await self.queue_fallback(live)
                return
            lid = await self.store.call("begin_screening", live, "locate", request, self.clock())
            result = await self.invoke_model(
                live[0], settings, lambda: self.model.evaluate_request(request, settings), "locate", lid
            )
            priorities = {
                c["id"]: max(result.scores[labels[c["id"]] + "__" + key] for key in batch["keys"])
                for c in live
            }
            candidates = {cid for cid, score in priorities.items() if score >= settings.screen_threshold}
            await self.after_screen(live, result, [sid, lid], labels, candidates, priorities)
            self.journal.record(
                "候选定位完成", screening=lid, candidates=len(candidates), harmful=bool(candidates)
            )
        except InputRejected:
            await self.queue_fallback(cases)
        except asyncio.CancelledError:
            await asyncio.shield(self.finish_batch(cases, "插件停止中断了候选定位，未补审。"))
            raise
        except GuardError as exc:
            await self.finish_batch(cases, str(exc))

    async def queue_fallback(self, cases):
        for case in await self.batch_live(cases, check_age=False):
            old = case["data"].get("batch", {})
            await self.store.call(
                "queue_stage",
                case["id"],
                {"screening_ids": [], "label": "", **old, "phase": "detail", "priority": 0.5},
                self.clock(),
            )
        self.wake.set()

    async def finish_batch(self, cases, reason):
        for old in cases:
            current = await self.store.call("case", old["id"])
            if current and current["state"] == "auditing":
                await self.store.call("finish", old["id"], "incomplete", {"reason": reason}, self.clock())
        self.journal.record("批量审核未完成", cases=[c["id"] for c in cases], reason=reason)

    async def process(self, case):
        cid, data = case["id"], case["data"]
        try:
            settings, policy = self.policy_for(case)
            message = data["message"]
            if (message["scope"], message["mid"]) in self.invalidated:
                return
            if self.clock() - queued_at(case) > settings.advanced.max_queue_seconds:
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
                        await self.evidence.thread(atomic_write, path, content)
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
            result = await self.invoke_model(
                case, settings, lambda: self.model.evaluate(state, settings, media)
            )
            self.policy_for(case)
            decision = decide(result, settings)
            if decision.flagged:
                try:
                    await self.evidence.archive(await self.store.call("case", cid))
                except GuardError as exc:
                    missing.append("本地证据归档未完成")
                    self.journal.record("证据归档未完成", case=cid, reason=str(exc), harmful=True)
            if message["reply"] and (message["scope"], message["reply"]) in self.invalidated:
                missing.append("引用原文在审核期间被撤回")
            flagged, auto, warn = decision.flagged, decision.recall, decision.warn
            if flagged and data.get("batch"):
                await self.store.call("flag_screenings", data["batch"]["screening_ids"])
                if data["batch"].get("spot_check"):
                    self.journal.record("初筛抽查发现疑点", case=cid, harmful=True)
            state = "incomplete" if missing else "review" if flagged else "allowed"
            case = await self.store.call(
                "finish",
                cid,
                state,
                {
                    "audit_path": "detail",
                    "scores": result.scores,
                    "notification_ready": decision.notify,
                    "actual_model": result.model,
                    "missing": missing,
                    "reason": "；".join([*missing, *([decision.reason] if decision.reason else [])]),
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
        if not manual and (
            not policy.auto_recall
            or not case["data"].get("recall_ready")
            or case["data"].get("missing")
            or case["human"]
        ):
            raise Deferred("本条记录不满足自动撤回条件。")
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
            # Commit the archive before delete_msg is allowed to leave this process.
            await self.evidence.archive(current)

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
            previews = {}
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
                segments = [{"type": "text", "data": {"text": text}}]
                if settings.evidence.notify_images:
                    for item in selected:
                        if item["id"] not in previews:
                            try:
                                pictures = await self.evidence.render(
                                    item, settings.evidence, policy.display_name, settings.terms
                                )
                                previews[item["id"]] = await self.picture_segment(pictures.preview[0])
                            except GuardError as exc:
                                previews[item["id"]] = None
                                self.journal.record(
                                    "通知证据图片未生成", case=item["id"], reason=str(exc), harmful=True
                                )
                        # One first-page preview per member keeps batched notices bounded.
                        if previews[item["id"]] is not None:
                            segments.append(previews[item["id"]])

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
                        {**params, "message": segments},
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

    async def picture_segment(self, path):
        try:
            content = await self.evidence.thread(path.read_bytes)
        except OSError as exc:
            raise GuardError("证据图片暂不可用；不会回退发送原文。") from exc
        return {"type": "image", "data": {"file": "base64://" + base64.b64encode(content).decode()}}

    async def send_review(self, case, actor, pid, account):
        settings = self.settings()
        policy = settings.group(case["data"]["message"]["group"])
        pictures = await self.evidence.render(
            case, settings.evidence, policy.display_name if policy else "", settings.terms
        )
        segments = [await self.picture_segment(path) for path in pictures.preview]
        message = {**case["data"]["message"], "platform": pid, "account": account}

        def gate():
            self.journal.check()
            if self.stopping or self.settings().revision != settings.revision:
                raise Deferred("插件正在停止或证据配置已变化，取消本次发送。")

        async def prepare():
            if await self.store.call("case", case["id"]) is None:
                raise Deferred("记录已过期，不再发送。")

        # Manual pictures also use account pacing, quotas and uncertain-delivery protection.
        await self.platform.write(
            message,
            case["id"],
            "review:" + uuid.uuid4().hex,
            "send_private_msg",
            {"user_id": actor, "message": segments},
            gate,
            prepare,
        )
        self.journal.record("管理员查看证据", case=case["id"], actor=actor)

    async def maintain(self):
        more = self.settings().advanced
        # Prevent cleanup racing a renderer or a manual review of an old record.
        async with self.evidence.lock:
            files = await self.store.call(
                "maintain", self.clock(), more.retention_days, more.harmful_retention_days
            )
            removed = []
            for file in files:
                try:
                    evidence_path(self.store.path.parent, file).unlink(missing_ok=True)
                    removed.append(file)
                except (OSError, GuardError) as exc:
                    self.journal.record("过期证据暂未清理", exception=exc)
            await self.store.call("purge_files", removed)
        self.journal.maintain(more.retention_days, more.harmful_retention_days)

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
                or not case["data"].get("warn_ready")
                or case["data"].get("missing")
                or case["human"]
                or cid in self.cancelled_cases
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
            c.name
            for c in settings.checks
            if CATEGORIES[c.name].punishable and case["data"].get("scores", {}).get(c.key, 0) >= c.warn_at
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
